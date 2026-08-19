"""
Обрезка/перепроекция композитов по AOI, сборка мозаики, построение
пирамид -- прямой перенос Блока 8 исходного ноутбука,
работает только с локальными путями.
"""
import logging
import os
import shutil
import subprocess
import time

import geopandas as gpd
import rasterio
import rasterio.features
from rasterio.mask import mask
from rasterio.merge import merge
from rasterio.warp import Resampling, calculate_default_transform, reproject

logger = logging.getLogger("s2monitor.pipeline.mosaic")


def reproject_and_clip_composite(composite_path: str, aoi_shape, target_crs: str, temp_dir: str) -> str:
    filename = os.path.basename(composite_path)
    clipped_path = os.path.join(temp_dir, filename.replace(".tif", "_clipped.tif"))
    os.makedirs(temp_dir, exist_ok=True)

    with rasterio.open(composite_path) as src:
        aoi_gdf = gpd.GeoDataFrame(geometry=[aoi_shape], crs="EPSG:4326")
        geom = aoi_gdf.to_crs(src.crs).geometry.iloc[0]

        clipped, trans = mask(src, [geom.__geo_interface__], crop=True, all_touched=True)

        if src.crs.to_string() != target_crs:
            left, bottom, right, top = trans * (0, 0) + (trans * (clipped.shape[2], clipped.shape[1]))
            transform, width, height = calculate_default_transform(
                src.crs, target_crs, clipped.shape[2], clipped.shape[1],
                left=left, bottom=bottom, right=right, top=top,
            )
            meta = src.meta.copy()
            meta.update({
                "crs": target_crs, "transform": transform, "width": width, "height": height,
                "compress": "ZSTD", "predictor": 2, "tiled": True,
                "blockxsize": 256, "blockysize": 256, "nodata": 0,
            })
            with rasterio.open(clipped_path, "w", **meta) as dst:
                for i in range(clipped.shape[0]):
                    reproject(
                        rasterio.band(src, i + 1), rasterio.band(dst, i + 1),
                        src_transform=trans, src_crs=src.crs,
                        dst_transform=transform, dst_crs=target_crs,
                        resampling=Resampling.bilinear,
                    )
        else:
            meta = src.meta.copy()
            meta.update(height=clipped.shape[1], width=clipped.shape[2], transform=trans,
                        compress="ZSTD", predictor=2, tiled=True,
                        blockxsize=256, blockysize=256, nodata=0)
            with rasterio.open(clipped_path, "w", **meta) as dst:
                dst.write(clipped)

    return clipped_path


def create_mosaic(composite_paths: list, aoi_shape, target_crs: str, mosaic_output_path: str, temp_dir: str) -> str:
    """composite_paths -- список путей к уже собранным композитам одного
    заказа/спутника/даты. Обрезает каждый по AOI, перепроецирует, мержит,
    затем обрезает итоговую мозаику по AOI ещё раз (merge() отдаёт
    прямоугольную рамку по объединению тайлов -- этого недостаточно, если
    AOI не прямоугольный)."""
    clipped_files = [reproject_and_clip_composite(p, aoi_shape, target_crs, temp_dir) for p in composite_paths]
    if not clipped_files:
        raise RuntimeError("Нет файлов для мозаики")

    srcs = [rasterio.open(f) for f in clipped_files]
    mosaic_array, out_trans = merge(srcs)

    meta = srcs[0].meta.copy()
    meta.update({
        "height": mosaic_array.shape[1], "width": mosaic_array.shape[2], "transform": out_trans,
        "compress": "ZSTD", "predictor": 2, "tiled": True,
        "blockxsize": 256, "blockysize": 256, "nodata": 0,
    })

    os.makedirs(os.path.dirname(mosaic_output_path), exist_ok=True)
    with rasterio.open(mosaic_output_path, "w", **meta) as dst:
        dst.write(mosaic_array)
    for src in srcs:
        src.close()

    # Финальная обрезка мозаики по точному AOI
    with rasterio.open(mosaic_output_path) as src:
        aoi_gdf = gpd.GeoDataFrame(geometry=[aoi_shape], crs="EPSG:4326")
        geom = aoi_gdf.to_crs(src.crs).geometry.iloc[0]
        clipped, trans = mask(src, [geom.__geo_interface__], crop=True, all_touched=True)

        meta = src.meta.copy()
        meta.update(height=clipped.shape[1], width=clipped.shape[2], transform=trans,
                    compress="ZSTD", predictor=2, tiled=True,
                    blockxsize=256, blockysize=256, nodata=0, dtype="uint16")

        with rasterio.open(mosaic_output_path, "w", **meta) as dst:
            dst.write(clipped)

    for f in clipped_files:
        if os.path.exists(f):
            os.remove(f)

    logger.info("Мозаика собрана: %s", os.path.basename(mosaic_output_path))
    return mosaic_output_path


def has_overviews(raster_path: str) -> bool:
    """Есть ли у растра уже построенные пирамиды (внешние .ovr или внутренние)."""
    try:
        with rasterio.open(raster_path) as src:
            return bool(src.overviews(1))
    except Exception:  # noqa: BLE001
        return False


def build_pyramids(raster_path: str) -> bool:
    """Строит пирамиды ОТДЕЛЬНЫМ файлом .ovr рядом с растром, средствами
    rasterio -- без вызова внешней утилиты gdaladdo.

    Почему без gdaladdo: он живёт в пакете gdal-bin, который приходилось
    ставить через apt-get на каждом прогоне. Этот шаг регулярно зависал
    на раннере (наблюдались прогоны по 1-6 часов, висящие именно на
    apt-get). rasterio уже содержит GDAL внутри себя, поэтому внешняя
    утилита не нужна вовсе.

    Ключевой момент -- TIFF_USE_OVR=YES: без него GDAL в режиме
    обновления ('r+') записал бы пирамиды ВНУТРЬ самого .tif, а нужен
    именно отдельный файл .ovr.
    """
    if not os.path.exists(raster_path):
        return False

    ovr_path = raster_path + ".ovr"
    if os.path.exists(ovr_path):
        logger.info("Пирамиды уже есть: %s", os.path.basename(ovr_path))
        return True

    logger.info("Строим внешние пирамиды (.ovr): %s", os.path.basename(raster_path))
    start = time.time()

    try:
        with rasterio.open(raster_path) as src:
            min_side = min(src.width, src.height)
        # Уровни, на которых картинка ещё крупнее ~128 px -- мельче
        # строить пирамиду бессмысленно.
        factors = [f for f in (2, 4, 8, 16, 32, 64) if min_side // f >= 128] or [2]

        with rasterio.Env(TIFF_USE_OVR=True, GDAL_TIFF_OVR_BLOCKSIZE="512", COMPRESS_OVERVIEW="ZSTD"):
            with rasterio.open(raster_path, "r+") as dst:
                dst.build_overviews(factors, Resampling.average)
                dst.update_tags(ns="rio_overview", resampling="average")

        if os.path.exists(ovr_path):
            size_mb = os.path.getsize(ovr_path) / (1024 * 1024)
            logger.info(
                "Пирамиды построены за %.1f сек -> %s (%.1f МБ, уровни: %s)",
                time.time() - start, os.path.basename(ovr_path), size_mb, factors,
            )
            return True

        logger.warning(
            "Файл .ovr не появился -- пирамиды, похоже, записались внутрь .tif. "
            "Пробую запасной путь через gdaladdo, если он есть в системе."
        )
        return _build_pyramids_gdaladdo(raster_path, factors)

    except Exception as e:  # noqa: BLE001
        logger.error("Ошибка создания пирамид через rasterio: %s", e)
        return _build_pyramids_gdaladdo(raster_path)


def _build_pyramids_gdaladdo(raster_path: str, factors=None) -> bool:
    """Запасной путь: внешняя утилита gdaladdo, ЕСЛИ она есть в системе.
    На раннерах GitHub её обычно нет (пакет gdal-bin не ставится), поэтому
    это просто подстраховка для локальных запусков -- сама по себе она
    ничего не устанавливает и не может подвиснуть на apt-get."""
    if shutil.which("gdaladdo") is None:
        logger.error("gdaladdo не найден в системе -- пирамиды не построены")
        return False

    levels = " ".join(str(f) for f in (factors or (2, 4, 8, 16, 32, 64)))
    ovr_path = raster_path + ".ovr"
    try:
        subprocess.run(
            ["gdaladdo", "-ro", "--config", "GDAL_TIFF_OVR_BLOCKSIZE", "512", raster_path, *levels.split()],
            check=True, timeout=1800,
        )
        return os.path.exists(ovr_path)
    except Exception as e:  # noqa: BLE001
        logger.error("gdaladdo тоже не справился: %s", e)
        return False

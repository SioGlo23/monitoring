"""
Обрезка/перепроекция композитов по AOI, сборка мозаики, построение
пирамид (gdaladdo) -- прямой перенос Блока 8 исходного ноутбука,
работает только с локальными путями.
"""
import logging
import os
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


def build_pyramids(raster_path: str) -> bool:
    if not os.path.exists(raster_path):
        return False
    ovr_path = raster_path + ".ovr"
    if os.path.exists(ovr_path):
        return True

    logger.info("Строим пирамиды (gdaladdo): %s", os.path.basename(raster_path))
    start = time.time()
    try:
        subprocess.run(
            f'gdaladdo -ro --config GDAL_TIFF_OVR_BLOCKSIZE 512 "{raster_path}" 2 4 8 16 32 64',
            shell=True, check=True,
        )
        ok = os.path.exists(ovr_path)
        logger.info("Пирамиды %s за %.1f сек", "созданы" if ok else "НЕ созданы", time.time() - start)
        return ok
    except Exception as e:  # noqa: BLE001
        logger.error("Ошибка создания пирамид: %s", e)
        return False

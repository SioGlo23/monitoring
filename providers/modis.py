"""
MODIS Terra (7-2-1) через NASA GIBS WMS. Скачивание, отбраковка пустых
сцен, ZSTD-сжатие -- как в исходном ноутбуке. Плюс: расчёт процента
облачных пикселей ВНУТРИ точного полигона AOI (не буферизованного bbox,
который используется только для запроса самого снимка).

Условие "облачный пиксель" (задано пользователем, каналы в порядке
скачанного 3-канального файла 7-2-1): канал 3 > 160 И канал 2 > 180
одновременно.
"""
import logging
import os

import numpy as np
import pyproj
import rasterio
import rasterio.features
import requests
from rasterio.enums import Compression
from shapely.ops import transform

import config
import storage
import utils

logger = logging.getLogger("s2monitor.modis")

_transform_to_utm = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform
_transform_back = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform

# Условие облачности: канал3 > CH3_THRESHOLD И канал2 > CH2_THRESHOLD
_CLOUD_CH3_THRESHOLD = 160
_CLOUD_CH2_THRESHOLD = 180


def _is_empty(filepath: str, left_columns: int = 4) -> bool:
    try:
        with rasterio.open(filepath) as src:
            data = src.read()
            left_strip = data[:, :, :left_columns]
            return bool(np.all(left_strip == 0))
    except Exception:  # noqa: BLE001
        return True


def _compute_cloud_percent(filepath: str, aoi_shape) -> float:
    """Процент пикселей, одновременно удовлетворяющих условию облачности,
    среди пикселей, которые лежат ВНУТРИ точного полигона aoi_shape
    (в исходной геосистеме файла -- EPSG:4326, т.к. запрос к WMS шёл
    в EPSG:4326). Возвращает None, если в файле нет данных внутри AOI."""
    with rasterio.open(filepath) as src:
        data = src.read()
        if data.shape[0] < 3:
            return None

        aoi_mask = rasterio.features.geometry_mask(
            [aoi_shape.__geo_interface__],
            out_shape=(src.height, src.width),
            transform=src.transform,
            invert=True,  # True -- пиксель ВНУТРИ полигона
        )
        total_aoi_px = int(aoi_mask.sum())
        if total_aoi_px == 0:
            return None

        channel2 = data[1]  # канал 2 (индекс 1)
        channel3 = data[2]  # канал 3 (индекс 2)
        cloud_mask = (channel3 > _CLOUD_CH3_THRESHOLD) & (channel2 > _CLOUD_CH2_THRESHOLD) & aoi_mask
        return round(float(cloud_mask.sum()) / total_aoi_px * 100, 2)


def download_for_aoi(zakaz, aoi_shape, date_str: str):
    """Скачивает MODIS для AOI (буфер 5000 м), загружает на Google Drive,
    считает облачность внутри точного AOI. Возвращает (ссылка_или_None,
    процент_облачности_или_None)."""
    blob_path = f"{config.MODIS_PREFIX}/zakaz_{zakaz}_{date_str}_MODIS_721.tif"
    storage.ensure_local_dir(config.LOCAL_TMP_DIR)
    tmp_final = os.path.join(config.LOCAL_TMP_DIR, f"zakaz_{zakaz}_{date_str}_final.tif")

    if storage.blob_exists(blob_path):
        logger.info("MODIS для zakaz_%s уже есть на Диске -- скачиваю для расчёта облачности", zakaz)
        try:
            data = storage.download_bytes(blob_path)
            with open(tmp_final, "wb") as f:
                f.write(data)
            cloud_percent = _compute_cloud_percent(tmp_final, aoi_shape)
            return f"drive:{blob_path}", cloud_percent
        except Exception as exc:  # noqa: BLE001
            logger.error("Не удалось скачать существующий MODIS для zakaz_%s: %s", zakaz, exc)
            return f"drive:{blob_path}", None
        finally:
            if os.path.exists(tmp_final):
                os.remove(tmp_final)

    utm_shape = transform(_transform_to_utm, aoi_shape)
    buffered = transform(_transform_back, utm_shape.buffer(5000))
    minx, miny, maxx, maxy = buffered.bounds
    bbox_str = f"{miny},{minx},{maxy},{maxx}"

    url = (
        "https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi"
        "?SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0"
        "&LAYERS=MODIS_Terra_CorrectedReflectance_Bands721"
        "&STYLES=default&FORMAT=image/tiff"
        f"&CRS=EPSG:4326&BBOX={bbox_str}"
        f"&WIDTH=2048&HEIGHT=2048&TIME={date_str}"
    )

    tmp_raw = os.path.join(config.LOCAL_TMP_DIR, f"zakaz_{zakaz}_{date_str}_raw.tif")

    try:
        def _do_request():
            resp = requests.get(url, timeout=90)
            resp.raise_for_status()
            return resp.content

        content = utils.retry(
            _do_request, attempts=3, delay_seconds=3, logger=logger, what=f"MODIS WMS (zakaz_{zakaz})"
        )
        with open(tmp_raw, "wb") as f:
            f.write(content)

        if _is_empty(tmp_raw):
            logger.info("MODIS для zakaz_%s пуст (нет данных на эту дату) -- пропускаем", zakaz)
            return None, None

        with rasterio.open(tmp_raw) as src:
            profile = src.profile.copy()
            profile.update(compress=Compression.zstd, zstd_level=9, tiled=True, blockxsize=256, blockysize=256)
            with rasterio.open(tmp_final, "w", **profile) as dst:
                dst.write(src.read())

        cloud_percent = _compute_cloud_percent(tmp_final, aoi_shape)

        gs_path = storage.upload_file(tmp_final, blob_path, content_type="image/tiff")
        logger.info(
            "MODIS для zakaz_%s сохранён -> %s (облачность в AOI: %s%%)",
            zakaz, gs_path, cloud_percent,
        )
        return gs_path, cloud_percent

    except Exception as exc:  # noqa: BLE001
        logger.error("Ошибка MODIS для zakaz_%s: %s", zakaz, exc)
        return None, None
    finally:
        for p in (tmp_raw, tmp_final):
            if os.path.exists(p):
                os.remove(p)

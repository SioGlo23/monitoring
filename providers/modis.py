"""
MODIS Terra (7-2-1) через NASA GIBS WMS. Логика скачивания, отбраковки
пустых сцен и ZSTD-сжатия — как в исходном ноутбуке. Итоговый файл
загружается в GCS вместо Google Drive.
"""
import logging
import os

import numpy as np
import pyproj
import rasterio
import requests
from rasterio.enums import Compression
from shapely.ops import transform

import config
import storage

logger = logging.getLogger("s2monitor.modis")

_transform_to_utm = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform
_transform_back = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform


def _is_empty(filepath: str, left_columns: int = 4) -> bool:
    try:
        with rasterio.open(filepath) as src:
            data = src.read()
            left_strip = data[:, :, :left_columns]
            return bool(np.all(left_strip == 0))
    except Exception:  # noqa: BLE001
        return True


def download_for_aoi(zakaz, aoi_shape, date_str: str) -> str | None:
    """Скачивает MODIS для AOI (буфер 5000 м), загружает в GCS. Возвращает gs:// путь или None."""
    blob_path = f"{config.MODIS_PREFIX}/zakaz_{zakaz}_{date_str}_MODIS_721.tif"

    if storage.blob_exists(blob_path):
        logger.info("MODIS для zakaz_%s уже есть в бакете, пропускаем скачивание", zakaz)
        return f"gs://{config.GCS_BUCKET}/{blob_path}"

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

    storage.ensure_local_dir(config.LOCAL_TMP_DIR)
    tmp_raw = os.path.join(config.LOCAL_TMP_DIR, f"zakaz_{zakaz}_{date_str}_raw.tif")
    tmp_final = os.path.join(config.LOCAL_TMP_DIR, f"zakaz_{zakaz}_{date_str}_final.tif")

    try:
        r = requests.get(url, timeout=90)
        r.raise_for_status()
        with open(tmp_raw, "wb") as f:
            f.write(r.content)

        if _is_empty(tmp_raw):
            logger.info("MODIS для zakaz_%s пуст (нет данных на эту дату) — пропускаем", zakaz)
            os.remove(tmp_raw)
            return None

        with rasterio.open(tmp_raw) as src:
            profile = src.profile.copy()
            profile.update(compress=Compression.zstd, zstd_level=9, tiled=True, blockxsize=256, blockysize=256)
            with rasterio.open(tmp_final, "w", **profile) as dst:
                dst.write(src.read())

        gs_path = storage.upload_file(tmp_final, blob_path, content_type="image/tiff")
        logger.info("MODIS для zakaz_%s сохранён → %s", zakaz, gs_path)
        return gs_path

    except Exception as exc:  # noqa: BLE001
        logger.error("Ошибка MODIS для zakaz_%s: %s", zakaz, exc)
        return None
    finally:
        for p in (tmp_raw, tmp_final):
            if os.path.exists(p):
                os.remove(p)

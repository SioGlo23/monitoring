"""
Геопривязка квиклуков для отображения слоем на карте.

Зачем это нужно. folium.ImageOverlay умеет класть картинку только в
прямоугольник, выровненный по параллелям и меридианам. Реальные
footprint'ы снимков в широтно-долготной сетке повёрнуты (у Landsat --
заметно, порядка 10-13 градусов), поэтому "натянуть картинку на bbox"
даёт видимое смещение.

Решение: квиклук изначально north-up в СВОЕЙ проекции (у Sentinel-2 PVI
это UTM-квадрат тайла, у Landsat browse -- north-up рамка сцены в UTM).
Значит, можно честно назначить ему геопривязку в этой проекции, а затем
перепроецировать в EPSG:4326. Результат -- north-up картинка в
широтно-долготной сетке, которую ImageOverlay кладёт на карту уже без
смещения, с прозрачностью за пределами реального контура снимка.

На выходе: PNG с альфа-каналом (RGBA) в EPSG:4326 + bounds в формате,
который ждёт folium: [[south, west], [north, east]].
"""
import logging

import geopandas as gpd
import numpy as np
import rasterio.features
from PIL import Image
from rasterio.transform import array_bounds, from_bounds
from rasterio.warp import Resampling, calculate_default_transform, reproject
from shapely.geometry import mapping, shape

logger = logging.getLogger("s2monitor.pipeline.quicklook_geo")

# Пиксели темнее этого значения во ВСЕХ каналах считаются фоном рамки
# (у Landsat browse углы залиты чёрным вокруг повёрнутой сцены) и
# делаются прозрачными. Порог намеренно очень низкий, чтобы не съесть
# тёмную воду на самом снимке.
_BLACK_THRESHOLD = 4


def georeference_quicklook(src_png: str, footprint_geojson: dict, src_crs: str,
                            dst_png: str, max_px: int = 512):
    """Привязывает квиклук и перепроецирует его в EPSG:4326.

    src_png       -- исходный квиклук (обычный PNG/JPEG без геопривязки)
    footprint_geojson -- контур сцены (GeoJSON geometry, EPSG:4326)
    src_crs       -- проекция, в которой квиклук north-up (обычно UTM сцены)
    dst_png       -- куда сохранить результат (RGBA PNG в EPSG:4326)
    max_px        -- ограничение размера по длинной стороне: карта
                     встраивает картинки в HTML как base64, поэтому
                     большие изображения раздувают файл карты

    Возвращает bounds для folium ([[south, west], [north, east]]) либо
    None, если привязать не удалось.
    """
    try:
        img = Image.open(src_png).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Геопривязка: не удалось открыть %s: %s", src_png, exc)
        return None

    # Ограничиваем размер ДО перепроецирования -- и быстрее, и итоговый
    # PNG заметно легче (важно, т.к. он инлайнится в HTML карты).
    if max(img.size) > max_px:
        scale = max_px / max(img.size)
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)

    rgb = np.moveaxis(np.array(img), -1, 0)  # (3, H, W)
    height, width = rgb.shape[1], rgb.shape[2]

    # --- контур сцены в проекции квиклука ---
    try:
        fp_4326 = shape(footprint_geojson)
        fp_proj = gpd.GeoSeries([fp_4326], crs="EPSG:4326").to_crs(src_crs).iloc[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Геопривязка: не удалось перепроецировать контур в %s: %s", src_crs, exc)
        return None

    minx, miny, maxx, maxy = fp_proj.bounds
    if not all(np.isfinite([minx, miny, maxx, maxy])) or maxx <= minx or maxy <= miny:
        logger.warning("Геопривязка: некорректный bbox контура %s", (minx, miny, maxx, maxy))
        return None

    # Квиклук north-up и покрывает ровно bbox сцены в её проекции
    src_transform = from_bounds(minx, miny, maxx, maxy, width, height)

    # --- альфа: прозрачно вне контура и на чёрной рамке ---
    inside = rasterio.features.geometry_mask(
        [mapping(fp_proj)], out_shape=(height, width), transform=src_transform, invert=True
    )
    not_black = rgb.max(axis=0) > _BLACK_THRESHOLD
    alpha = (inside & not_black).astype("uint8") * 255

    # --- перепроецирование в EPSG:4326 ---
    dst_crs = "EPSG:4326"
    try:
        dst_transform, dst_width, dst_height = calculate_default_transform(
            src_crs, dst_crs, width, height, left=minx, bottom=miny, right=maxx, top=maxy
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Геопривязка: calculate_default_transform не сработал: %s", exc)
        return None

    out = np.zeros((4, dst_height, dst_width), dtype="uint8")
    try:
        for i in range(3):
            reproject(
                source=rgb[i], destination=out[i],
                src_transform=src_transform, src_crs=src_crs,
                dst_transform=dst_transform, dst_crs=dst_crs,
                resampling=Resampling.bilinear,
            )
        reproject(
            source=alpha, destination=out[3],
            src_transform=src_transform, src_crs=src_crs,
            dst_transform=dst_transform, dst_crs=dst_crs,
            resampling=Resampling.nearest,  # альфу нельзя размывать
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Геопривязка: перепроецирование не удалось: %s", exc)
        return None

    try:
        Image.fromarray(np.moveaxis(out, 0, -1), mode="RGBA").save(dst_png, format="PNG", optimize=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Геопривязка: не удалось сохранить %s: %s", dst_png, exc)
        return None

    west, south, east, north = array_bounds(dst_height, dst_width, dst_transform)
    logger.info(
        "Геопривязка готова: %sx%s px, bounds lat %.4f..%.4f, lon %.4f..%.4f",
        dst_width, dst_height, south, north, west, east,
    )
    return [[float(south), float(west)], [float(north), float(east)]]

"""Интерактивная карта (MODIS + AOI + Sentinel-2 + Landsat) -- как в исходном ноутбуке.
Без изменений в рамках этого обновления."""
import logging
import os

import folium

import config
import storage
from shapely.geometry import shape

logger = logging.getLogger("s2monitor.mapping")


def build_map(current_s2: dict, current_landsat: dict, aoi_dict: dict):
    geoms = [shape(feat["geometry"]) for feat in aoi_dict.values()]
    for prods in current_s2.values():
        geoms += [shape(p["GeoFootprint"]) for p in prods if p.get("GeoFootprint")]
    for prods in current_landsat.values():
        geoms += [shape(p["GeoFootprint"]) for p in prods if p.get("GeoFootprint")]

    if not geoms:
        return None

    import geopandas as gpd

    gdf = gpd.GeoDataFrame(geometry=geoms, crs="EPSG:4326")
    minx, miny, maxx, maxy = gdf.total_bounds
    center = [(miny + maxy) / 2, (minx + maxx) / 2]

    m = folium.Map(location=center, zoom_start=5)

    folium.WmsTileLayer(
        url="https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi",
        layers="MODIS_Terra_CorrectedReflectance_Bands721",
        styles="default", fmt="image/png", transparent=True,
        version="1.3.0", name="MODIS Terra 7-2-1",
        overlay=True, control=True, show=True,
    ).add_to(m)

    folium.GeoJson(
        {"type": "FeatureCollection", "features": list(aoi_dict.values())},
        name="Все области интереса (AOI)",
        style_function=lambda x: {"color": "red", "weight": 4, "fillOpacity": 0.18},
        show=True,
    ).add_to(m)

    for zakaz, feat in aoi_dict.items():
        cx, cy = shape(feat["geometry"]).centroid.xy
        folium.Marker(
            [cy[0], cx[0]],
            icon=folium.DivIcon(html=f'<div style="font-size:13pt;color:blue;font-weight:bold;">{zakaz}</div>'),
        ).add_to(m)

    def _cloud_label(p: dict) -> str:
        cc = p.get("cloud_cover")
        return f"{cc}%" if isinstance(cc, (int, float)) else "нет данных"

    def _quicklook_html(p: dict) -> str:
        link = p.get("quicklook_link")
        if not link:
            return "нет квиклука"
        return f'<img src="{link}" style="max-width:220px;max-height:220px;">'

    s2_features = [
        {
            "type": "Feature",
            "geometry": p["GeoFootprint"],
            "properties": {
                "scene_name": p.get("Name", "Unknown"),
                "scene_id": p.get("Id", "No ID"),
                "cloud_cover_label": _cloud_label(p),
                "start_time_msk": p.get("start_time_msk", "—"),
                "published_msk": p.get("published_msk", "—"),
                "discovered_msk": p.get("discovered_msk", "—"),
                "quicklook_html": _quicklook_html(p),
            },
        }
        for prods in current_s2.values() for p in prods if p.get("GeoFootprint")
    ]
    if s2_features:
        folium.GeoJson(
            {"type": "FeatureCollection", "features": s2_features},
            name="Sentinel-2 L1C",
            style_function=lambda x: {"color": "#8B00FF", "weight": 2.5, "fillOpacity": 0.25},
            # При наведении -- лёгкая текстовая подсказка (без картинки, чтобы
            # не тормозить hover). При КЛИКЕ -- окно с той же информацией плюс
            # квиклук, которое остаётся открытым, пока не кликнуть в другое
            # место (в отличие от tooltip, popup не закрывается при уходе курсора).
            tooltip=folium.GeoJsonTooltip(
                fields=["scene_name", "cloud_cover_label", "start_time_msk"],
                aliases=["Сцена:", "Облачность:", "Съёмка (МСК):"],
            ),
            popup=folium.GeoJsonPopup(
                fields=["scene_name", "scene_id", "cloud_cover_label", "start_time_msk", "published_msk", "discovered_msk", "quicklook_html"],
                aliases=["Сцена:", "ID:", "Облачность:", "Съёмка (МСК):", "Публикация (МСК):", "Обнаружено (МСК):", "Квиклук:"],
                max_width=320,
            ),
            show=True,
        ).add_to(m)

    landsat_features = [
        {
            "type": "Feature",
            "geometry": p["GeoFootprint"],
            "properties": {
                "scene_name": p.get("Name", "Unknown"),
                "scene_id": p.get("Id", "No ID"),
                "PR": p.get("PR", "—"),
                "cloud_cover_label": _cloud_label(p),
                "start_time_msk": p.get("start_time_msk", "—"),
                "discovered_msk": p.get("discovered_msk", "—"),
                "quicklook_html": _quicklook_html(p),
            },
        }
        for prods in current_landsat.values() for p in prods if p.get("GeoFootprint")
    ]
    if landsat_features:
        folium.GeoJson(
            {"type": "FeatureCollection", "features": landsat_features},
            name="Landsat 8/9 Level-1",
            style_function=lambda x: {"color": "#FF8C00", "weight": 2.5, "fillOpacity": 0.25},
            tooltip=folium.GeoJsonTooltip(
                fields=["scene_name", "PR", "cloud_cover_label", "start_time_msk"],
                aliases=["Сцена:", "PR:", "Облачность:", "Съёмка (МСК):"],
            ),
            popup=folium.GeoJsonPopup(
                fields=["scene_name", "scene_id", "PR", "cloud_cover_label", "start_time_msk", "discovered_msk", "quicklook_html"],
                aliases=["Сцена:", "ID:", "PR:", "Облачность:", "Съёмка (МСК):", "Обнаружено (МСК):", "Квиклук:"],
                max_width=320,
            ),
            show=True,
        ).add_to(m)

    folium.LayerControl().add_to(m)
    return m


def save_map(m, cycle_ts: str, cycle_number: int):
    if m is None:
        return None
    storage.ensure_local_dir(config.LOCAL_TMP_DIR)
    filename = f"{cycle_ts}_{cycle_number:03d}_cover.html"
    local_path = os.path.join(config.LOCAL_TMP_DIR, filename)
    m.save(local_path)
    blob_path = f"{config.LOGS_PREFIX}/{filename}"
    gs_path = storage.upload_file(local_path, blob_path, content_type="text/html")
    os.remove(local_path)

    latest_blob = f"{config.LOGS_PREFIX}/latest_map.html"
    tmp_latest = os.path.join(config.LOCAL_TMP_DIR, "latest_map.html")
    m.save(tmp_latest)
    storage.upload_file(tmp_latest, latest_blob, content_type="text/html")
    os.remove(tmp_latest)

    return gs_path

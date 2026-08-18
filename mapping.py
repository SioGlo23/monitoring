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

    # Карточки Leaflet по умолчанию расширяются под содержимое: длинное имя
    # сцены -- это один "неразрывный" токен без пробелов, а таблица без
    # table-layout:fixed растягивается под него и вылезает за рамку. Плюс
    # у тултипов Leaflet по умолчанию white-space:nowrap. Этот блок стилей
    # чинит и то, и другое, и заодно вписывает картинку квиклука в ширину
    # ячейки.
    m.get_root().header.add_child(folium.Element("""
<style>
  .leaflet-popup-content, .leaflet-tooltip {
      max-width: 360px !important;
      white-space: normal !important;
  }
  .leaflet-popup-content table, .leaflet-tooltip table {
      width: 100%;
      table-layout: fixed;   /* ключевое: таблица не шире контейнера */
      border-collapse: collapse;
  }
  .leaflet-popup-content table th, .leaflet-tooltip table th {
      width: 34%;
      text-align: left;
      vertical-align: top;
      padding: 2px 6px 2px 0;
      white-space: normal;
      overflow-wrap: break-word;
      word-break: normal;
  }
  .leaflet-popup-content table td, .leaflet-tooltip table td {
      width: 66%;
      vertical-align: top;
      padding: 2px 0;
      white-space: normal;
      overflow-wrap: anywhere;   /* рвёт длинное имя сцены по любому месту */
      word-break: break-all;
  }
  .leaflet-popup-content img, .leaflet-tooltip img {
      max-width: 100%;
      height: auto;
      display: block;
  }
</style>
"""))

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
        return f"{round(cc, 2)}%" if isinstance(cc, (int, float)) else "нет данных"

    def _quicklook_html(p: dict) -> str:
        link = p.get("quicklook_link")
        if not link:
            return "нет квиклука"
        # Ширину задаёт CSS выше (max-width:100% от ячейки) -- жёсткий
        # размер в px здесь как раз и заставлял картинку вылезать за рамку.
        # Текстовая ссылка рядом -- на случай, если картинка не отрисуется.
        return (
            f'<img src="{link}" alt="квиклук">'
            f'<a href="{link}" target="_blank">открыть отдельно</a>'
        )

    # Один и тот же набор полей и при наведении, и при клике (по просьбе),
    # ID убран. Перенос длинных значений и вписывание картинки в рамку
    # задаются глобальным блоком стилей выше (по классам Leaflet) --
    # здесь только минимальная ширина, чтобы карточка не схлопывалась.
    _DETAIL_STYLE = "min-width: 240px;"

    s2_features = [
        {
            "type": "Feature",
            "geometry": p["GeoFootprint"],
            "properties": {
                "scene_name": p.get("Name", "Unknown"),
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
        s2_fields = ["scene_name", "cloud_cover_label", "start_time_msk", "published_msk", "discovered_msk", "quicklook_html"]
        s2_aliases = ["Сцена:", "Облачность:", "Съёмка (МСК):", "Публикация (МСК):", "Обнаружено (МСК):", "Квиклук:"]
        folium.GeoJson(
            {"type": "FeatureCollection", "features": s2_features},
            name="Sentinel-2 L1C",
            style_function=lambda x: {"color": "#8B00FF", "weight": 2.5, "fillOpacity": 0.25},
            # Тултип (наведение) и попап (клик) показывают ОДНИ И ТЕ ЖЕ поля.
            # Попап, в отличие от тултипа, не закрывается при уходе курсора --
            # закрывается только по клику в другое место карты.
            tooltip=folium.GeoJsonTooltip(fields=s2_fields, aliases=s2_aliases, style=_DETAIL_STYLE),
            popup=folium.GeoJsonPopup(fields=s2_fields, aliases=s2_aliases, style=_DETAIL_STYLE, max_width=360),
            show=True,
        ).add_to(m)

    landsat_features = [
        {
            "type": "Feature",
            "geometry": p["GeoFootprint"],
            "properties": {
                "scene_name": p.get("Name", "Unknown"),
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
        l_fields = ["scene_name", "PR", "cloud_cover_label", "start_time_msk", "discovered_msk", "quicklook_html"]
        l_aliases = ["Сцена:", "PR:", "Облачность:", "Съёмка (МСК):", "Обнаружено (МСК):", "Квиклук:"]
        folium.GeoJson(
            {"type": "FeatureCollection", "features": landsat_features},
            name="Landsat 8/9 Level-1",
            style_function=lambda x: {"color": "#FF8C00", "weight": 2.5, "fillOpacity": 0.25},
            tooltip=folium.GeoJsonTooltip(fields=l_fields, aliases=l_aliases, style=_DETAIL_STYLE),
            popup=folium.GeoJsonPopup(fields=l_fields, aliases=l_aliases, style=_DETAIL_STYLE, max_width=360),
            show=True,
        ).add_to(m)

    _add_quicklook_overlays(m, current_s2, current_landsat)

    folium.LayerControl().add_to(m)
    return m


def _add_quicklook_overlays(m, current_s2: dict, current_landsat: dict) -> None:
    """Кладёт геопривязанные квиклуки слоями на карту: все Sentinel-2 в
    один слой, все Landsat -- в другой (галочки в LayerControl).

    Картинки берутся с Google Drive и ВСТРАИВАЮТСЯ в HTML карты как
    base64 (folium делает это сам, когда ImageOverlay получает путь к
    локальному файлу). Это надёжнее ссылок на Drive: карта не зависит от
    доступности файлов и работает даже открытая локально. Размер картинок
    ограничен config.QUICKLOOK_MAX_PX как раз чтобы HTML не распухал."""
    tmp_dir = os.path.join(config.LOCAL_TMP_DIR, "quicklooks_map")
    os.makedirs(tmp_dir, exist_ok=True)

    groups = (
        ("Квиклуки Sentinel-2", current_s2),
        ("Квиклуки Landsat 8/9", current_landsat),
    )

    for group_name, prods_dict in groups:
        group = folium.FeatureGroup(name=group_name, show=config.QUICKLOOK_LAYERS_SHOW_BY_DEFAULT)
        added = 0

        for prods in prods_dict.values():
            for p in prods:
                blob = p.get("quicklook_geo_blob")
                bounds = p.get("quicklook_geo_bounds")
                if not blob or not bounds:
                    continue
                try:
                    local_path = os.path.join(tmp_dir, os.path.basename(blob))
                    if not os.path.exists(local_path):
                        storage.download_to_file(blob, local_path)
                    folium.raster_layers.ImageOverlay(
                        image=local_path,
                        bounds=bounds,
                        opacity=config.QUICKLOOK_OVERLAY_OPACITY,
                        interactive=False,   # чтобы не перехватывать клики у контуров сцен
                        cross_origin=False,
                        zindex=1,
                    ).add_to(group)
                    added += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Квиклук-слой: не удалось добавить %s: %s", blob, exc)

        if added:
            group.add_to(m)
            logger.info("Слой '%s': добавлено %s квиклук(ов)", group_name, added)


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

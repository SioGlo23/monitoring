"""
Landsat 8/9 через USGS M2M API (только поиск сцен). Логика та же, что
в исходном ноутбуке (поиск + сопоставление с GRID_Landsat), плюс
опциональная фильтрация по pr_tile.
"""
import json
import logging

import geopandas as gpd
import requests
from shapely.geometry import shape

import config
import utils

logger = logging.getLogger("s2monitor.usgs_m2m")

_BASE = "https://m2m.cr.usgs.gov/api/api/json/stable/"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) s2-monitor-service/1.0",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
}
DATASET_NAME = "landsat_ot_c2_l1"


def get_session():
    """Логин в USGS M2M. Возвращает session_id (X-Auth-Token) или None,
    если Landsat в этом прогоне недоступен."""
    if config.M2M_USERNAME and config.M2M_PASSWORD:
        try:
            r = requests.post(
                _BASE + "login",
                json={"username": config.M2M_USERNAME, "password": config.M2M_PASSWORD},
                headers=_HEADERS,
                timeout=30,
            )
            if r.status_code == 200 and r.json().get("errorCode") is None:
                logger.info("USGS M2M: авторизация по паролю успешна")
                return r.json().get("data")
        except Exception as exc:  # noqa: BLE001
            logger.warning("USGS M2M: ошибка логина по паролю: %s", exc)

    if config.M2M_USERNAME and config.M2M_TOKEN:
        try:
            r = requests.post(
                _BASE + "login-token",
                json={"username": config.M2M_USERNAME, "token": config.M2M_TOKEN},
                headers=_HEADERS,
                timeout=30,
            )
            if r.status_code == 200 and r.json().get("errorCode") is None:
                logger.info("USGS M2M: авторизация по токену успешна")
                return r.json().get("data")
        except Exception as exc:  # noqa: BLE001
            logger.warning("USGS M2M: ошибка логина по токену: %s", exc)

    logger.warning("USGS M2M: не удалось авторизоваться -- Landsat в этом прогоне пропускается")
    return None


def logout(session_id):
    if not session_id:
        return
    try:
        requests.post(_BASE + "logout", headers={"X-Auth-Token": session_id, **_HEADERS}, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def query_for_aoi(zakaz, feat, session_id: str, target_date_str: str, grid_gdf: "gpd.GeoDataFrame") -> list:
    if not session_id:
        return []

    aoi_shape = shape(feat["geometry"])
    minx, miny, maxx, maxy = aoi_shape.bounds
    auth_headers = {"X-Auth-Token": session_id, **_HEADERS}

    payload = {
        "datasetName": DATASET_NAME,
        "maxResults": 20,
        "sceneFilter": {
            "acquisitionFilter": {"start": target_date_str, "end": target_date_str},
            "cloudCoverFilter": {"min": 0, "max": 100, "includeUnknown": True},
            "spatialFilter": {
                "filterType": "mbr",
                "lowerLeft": {"latitude": float(miny), "longitude": float(minx)},
                "upperRight": {"latitude": float(maxy), "longitude": float(maxx)},
            },
        },
    }

    def _do_request():
        resp = requests.post(_BASE + "scene-search", json=payload, headers=auth_headers, timeout=60)
        resp.raise_for_status()
        return resp.json().get("data", {}).get("results", [])

    results = utils.retry(
        _do_request, attempts=3, delay_seconds=3, logger=logger, what=f"M2M scene-search (заказ {zakaz})"
    )

    pr_tiles = utils.parse_tile_list(feat.get("properties", {}).get("pr_tile"))

    filtered = []
    seen_pr = set()
    for scene in results:
        display_id = scene.get("displayId", "")
        parts = display_id.split("_")
        if len(parts) < 3:
            continue
        scene_pr = parts[2]
        if scene_pr in seen_pr or scene_pr not in grid_gdf["PR"].values:
            continue

        # Если pr_tile задан в свойствах AOI -- берём только эти тайлы.
        # Если не задан/пуст -- берём все, что пересекаются с AOI (как раньше).
        if pr_tiles and scene_pr not in pr_tiles:
            continue

        matched = grid_gdf[grid_gdf["PR"] == scene_pr]
        if matched.empty:
            continue
        grid_geom = matched.iloc[0].geometry
        if not aoi_shape.intersects(grid_geom):
            continue

        seen_pr.add(scene_pr)
        geo_fp = json.loads(gpd.GeoSeries([grid_geom]).to_json())["features"][0]["geometry"]

        temporal = scene.get("temporalCoverage") or {}
        start_iso = temporal.get("startDate")
        if start_iso:
            start_time_msk = utils.to_local_readable(start_iso)
        else:
            date_part = parts[3] if len(parts) > 3 else ""
            start_time_msk = (
                f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}" if len(date_part) >= 8 else "—"
            )

        filtered.append(
            {
                "Name": display_id,
                "Id": scene.get("entityId") or display_id,
                "GeoFootprint": geo_fp,
                "PR": scene_pr,
                "cloud_cover": scene.get("cloudCover", "N/A"),
                "scene_date": parts[3] if len(parts) > 3 else "Unknown",
                "start_time_msk": start_time_msk,
            }
        )

    logger.info("Заказ %s: найдено %s снимков Landsat после фильтрации", zakaz, len(filtered))
    return filtered

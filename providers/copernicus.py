"""
Sentinel-2 через Copernicus Dataspace OData API.
Логика запроса и фильтрации — как в исходном ноутбуке, вынесена в
отдельный модуль и дополнена ретраями.
"""
import logging

import requests
from shapely.geometry import shape

import config
import utils

logger = logging.getLogger("s2monitor.copernicus")

_TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
_PRODUCTS_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"


def get_access_token() -> str:
    def _do():
        resp = requests.post(
            _TOKEN_URL,
            data={
                "client_id": "cdse-public",
                "username": config.COPERNICUS_USERNAME,
                "password": config.COPERNICUS_PASSWORD,
                "grant_type": "password",
            },
            timeout=30,
        )
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if not token:
            raise RuntimeError("Copernicus не вернул access_token")
        return token

    return utils.retry(_do, logger=logger, what="Copernicus auth")


def query_for_aoi(zakaz, feat, access_token: str, today_start: str, tomorrow: str) -> list:
    """Ищет сцены Sentinel-2 L1C, пересекающие AOI, за указанные сутки."""
    aoi_shape = shape(feat["geometry"])
    minx, miny, maxx, maxy = aoi_shape.bounds
    bbox_wkt = f"POLYGON(({minx} {miny},{maxx} {miny},{maxx} {maxy},{minx} {maxy},{minx} {miny}))"

    params = {
        "$filter": (
            "Collection/Name eq 'SENTINEL-2' and "
            "Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType' "
            "and att/OData.CSC.StringAttribute/Value eq 'S2MSI1C') and "
            f"OData.CSC.Intersects(area=geography'SRID=4326;{bbox_wkt}') and "
            f"ContentDate/Start gt {today_start} and ContentDate/Start lt {tomorrow}"
        ),
        "$orderby": "ContentDate/Start desc",
        "$top": 15,
    }
    headers = {"Authorization": f"Bearer {access_token}"}

    def _do_request():
        resp = requests.get(_PRODUCTS_URL, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json().get("value", [])

    # Ретраи на случай временного сбоя API. Если после всех попыток
    # запрос так и не удался — исключение пробрасывается наверх, а не
    # тихо превращается в "снимков нет" (иначе временный сбой сети
    # выглядел бы как реальное исчезновение уже известных снимков и
    # мог бы затереть память о них — см. обработку в monitor.py).
    products = utils.retry(_do_request, attempts=3, delay_seconds=3, logger=logger, what=f"S2 query (заказ {zakaz})")

    filtered = []
    for p in products:
        if p.get("GeoFootprint"):
            footprint = shape(p["GeoFootprint"])
            if aoi_shape.intersects(footprint):
                pub_date = p.get("PublicationDate") or p.get("IngestionDate") or ""
                p["published_msk"] = utils.to_local_readable(pub_date)
                # Start Time из метаданных — фактическое время съёмки (ContentDate/Start),
                # а не дата публикации/обнаружения.
                content_date = p.get("ContentDate") or {}
                p["start_time_msk"] = utils.to_local_readable(content_date.get("Start"))
                filtered.append(p)

    logger.info("Заказ %s: найдено %s снимков S2 после фильтрации", zakaz, len(filtered))
    return filtered

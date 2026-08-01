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

    try:
        resp = requests.get(_PRODUCTS_URL, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.error("Ошибка запроса S2 для заказа %s: %s", zakaz, exc)
        return []

    products = resp.json().get("value", [])
    filtered = []
    for p in products:
        if p.get("GeoFootprint"):
            footprint = shape(p["GeoFootprint"])
            if aoi_shape.intersects(footprint):
                pub_date = p.get("PublicationDate") or p.get("IngestionDate") or ""
                p["published_msk"] = utils.to_local_timestamp(pub_date)
                filtered.append(p)

    logger.info("Заказ %s: найдено %s снимков S2 после фильтрации", zakaz, len(filtered))
    return filtered

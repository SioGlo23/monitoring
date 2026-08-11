"""
Sentinel-2 через Copernicus Dataspace OData API. Логика запроса и
геофильтрации -- как в исходном ноутбуке (включая защитный фолбэк на
случай, если у продукта нет GeoFootprint -- продукт всё равно
сохраняется, а не выбрасывается: именно выбрасывание таких продуктов
было причиной регресса в предыдущей версии). Плюс: фильтрация по
mrgs_tiles и извлечение облачности из метаданных (Attributes/cloudCover).
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


def _extract_cloud_cover(product: dict):
    """Облачность сцены из метаданных (Attributes -> cloudCover). Возвращает
    float в процентах или None, если атрибут отсутствует в ответе API."""
    for attr in product.get("Attributes", []) or []:
        if attr.get("Name") == "cloudCover":
            try:
                return float(attr.get("Value"))
            except (TypeError, ValueError):
                return None
    return None


def query_for_aoi(zakaz, feat, access_token: str, today_start: str, tomorrow: str) -> list:
    """Ищет сцены Sentinel-2 L1C, пересекающие AOI, за указанные сутки, и
    (если задан) фильтрует по списку ожидаемых тайлов mrgs_tiles."""
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
        "$expand": "Attributes",
        "$orderby": "ContentDate/Start desc",
        "$top": 20,
    }
    headers = {"Authorization": f"Bearer {access_token}"}

    def _do_request():
        resp = requests.get(_PRODUCTS_URL, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json().get("value", [])

    products = utils.retry(_do_request, attempts=3, delay_seconds=3, logger=logger, what=f"S2 query (заказ {zakaz})")
    logger.info("Заказ %s: получено от CDSE %s сырых продуктов", zakaz, len(products))

    mrgs_tiles = utils.parse_tile_list(feat.get("properties", {}).get("mrgs_tiles"))

    after_geo = 0
    filtered = []
    for p in products:
        # ВАЖНО: если у продукта нет GeoFootprint -- это НЕ повод его
        # выбрасывать (у CDSE такое бывает штатно для части ответов).
        # Как и в исходном ноутбуке, такой продукт всё равно попадает
        # дальше -- геопересечение просто не проверяется для него.
        footprint_raw = p.get("GeoFootprint")
        if footprint_raw:
            try:
                footprint = shape(footprint_raw)
                if not aoi_shape.intersects(footprint):
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("Заказ %s: не удалось разобрать GeoFootprint у %s (%s) -- оставляем как есть", zakaz, p.get("Name"), exc)
        else:
            logger.warning("Заказ %s: у продукта %s нет GeoFootprint -- оставляем без геофильтрации", zakaz, p.get("Name"))
        after_geo += 1

        tile_code = utils.extract_s2_tile(p.get("Name", ""))
        if mrgs_tiles and tile_code not in mrgs_tiles:
            continue

        pub_date = p.get("PublicationDate") or p.get("IngestionDate") or ""
        p["published_msk"] = utils.to_local_readable(pub_date)
        content_date = p.get("ContentDate") or {}
        p["start_time_msk"] = utils.to_local_readable(content_date.get("Start"))
        p["tile_code"] = tile_code
        p["cloud_cover"] = _extract_cloud_cover(p)
        filtered.append(p)

    logger.info(
        "Заказ %s: после геофильтра %s, после mrgs_tiles-фильтра %s снимков S2",
        zakaz, after_geo, len(filtered),
    )
    if filtered and all(p.get("cloud_cover") is None for p in filtered):
        sample_attrs = [a.get("Name") for a in (filtered[0].get("Attributes") or [])]
        logger.warning(
            "Заказ %s: ни у одного продукта не нашёлся cloudCover в Attributes. "
            "Доступные имена атрибутов у первого продукта: %s",
            zakaz, sample_attrs,
        )
    return filtered

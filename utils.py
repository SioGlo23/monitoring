"""Общие мелкие утилиты, переиспользуемые в нескольких модулях."""
import re
from datetime import datetime

import pytz

import config

_tz = pytz.timezone(config.TIMEZONE)

_S2_TILE_RE = re.compile(r'_T(\d{2}[A-Z]{3})_')


def now_local():
    return datetime.now(_tz)


def to_local_timestamp(iso_string: str) -> str:
    if not iso_string:
        return "Unknown"
    dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
    return dt.astimezone(_tz).strftime("%Y%m%dT%H%M%S")


def to_local_readable(iso_string: str) -> str:
    """Дата+время в читаемом виде для писем/UI, например '2026-08-01 14:23:05'."""
    if not iso_string:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
    except ValueError:
        return "—"
    return dt.astimezone(_tz).strftime("%Y-%m-%d %H:%M:%S")


def retry(fn, attempts: int = 3, delay_seconds: float = 2.0, logger=None, what: str = "operation"):
    """Простой ретрай с линейной паузой для нестабильных внешних API."""
    import time

    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - хотим ловить любые сетевые сбои
            last_exc = exc
            if logger:
                logger.warning("Попытка %s/%s для %s не удалась: %s", attempt, attempts, what, exc)
            if attempt < attempts:
                time.sleep(delay_seconds * attempt)
    raise last_exc


def parse_tile_list(raw) -> set:
    """'41VPD, 42VUJ' или ['41VPD','42VUJ'] -> {'41VPD','42VUJ'}. Пусто/None -> set()."""
    if not raw:
        return set()
    parts = raw.split(",") if isinstance(raw, str) else raw
    return {str(p).strip() for p in parts if str(p).strip()}


def extract_s2_tile(name: str):
    """'..._T41VPD_...' -> '41VPD'. Возвращает None, если тайл-код не найден."""
    m = _S2_TILE_RE.search(name or "")
    return m.group(1) if m else None


def detect_landsat_number(display_id: str) -> str:
    """Номер спутника Landsat — 4-й символ Product ID (например, 'LC09_L1TP_...' -> '9')."""
    return display_id[3] if len(display_id) >= 4 else "?"


def utm_crs_for_shape(shape_obj) -> str:
    """UTM-зона EPSG-код по центру геометрии (аналог TYPE_CHOICE_CRS=1 в исходном ноутбуке)."""
    minx, _, maxx, _ = shape_obj.bounds
    center_lon = (minx + maxx) / 2
    zone = int((center_lon + 180) / 6) + 1
    return f"EPSG:326{zone:02d}" if center_lon >= 0 else f"EPSG:327{zone:02d}"


def compressed_profile(base_profile: dict, count: int, dtype: str = "uint16") -> dict:
    """Единый профиль вывода: заданный dtype + сжатие ZSTD + тайлинг."""
    profile = base_profile.copy()
    profile.update(count=count, dtype=dtype, tiled=True, blockxsize=256, blockysize=256, BIGTIFF="YES")
    profile.update(compress="ZSTD", zstd_level=9, predictor=2)
    return profile

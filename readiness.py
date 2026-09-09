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
    """'41VPD, 42VUJ' или ['41VPD','42VUJ'] -> {'41VPD','42VUJ'}. Пусто/None -> set().
    Скобки и ведущее число (см. parse_tile_spec) отбрасываются."""
    return parse_tile_spec(raw)[1]


_TILE_SPEC_RE = re.compile(r"^\s*(\d+)?\s*\((.*)\)\s*$", re.DOTALL)


def parse_tile_spec(raw):
    """Разбирает значение атрибута mrgs_tiles / landsat_grid.

    Поддерживаются два формата:

      "2 (37UCB, 37UDB)"   -- нужно НЕ МЕНЕЕ 2 тайлов из перечисленных;
                              учитываются только тайлы из скобок
      "37UCB, 37UDB"       -- старый формат: нужны ВСЕ перечисленные
                              (эквивалент "2 (37UCB, 37UDB)")
      "" / None            -- ограничений нет

    Возвращает (min_count, tiles):
      tiles     -- множество допустимых тайлов (пустое = без ограничений)
      min_count -- сколько из них достаточно, чтобы начать загрузку
                   (None, если список тайлов не задан)

    min_count всегда в пределах 1..len(tiles): требовать больше тайлов,
    чем перечислено, бессмысленно -- такое задание никогда бы не
    запустилось.
    """
    if not raw:
        return None, set()

    if not isinstance(raw, str):
        tiles = {str(p).strip() for p in raw if str(p).strip()}
        return (len(tiles) if tiles else None), tiles

    text = raw.strip()
    if not text:
        return None, set()

    min_count = None
    match = _TILE_SPEC_RE.match(text)
    if match:
        if match.group(1):
            min_count = int(match.group(1))
        text = match.group(2)

    tiles = {p.strip() for p in text.split(",") if p.strip()}
    if not tiles:
        return None, set()

    if min_count is None:
        min_count = len(tiles)          # старый формат -- нужны все
    min_count = max(1, min(min_count, len(tiles)))
    return min_count, tiles


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


def utm_crs_for_s2_tile(tile_code: str):
    """UTM-зона по коду тайла Sentinel-2 ('41VPD' -> EPSG:32641).

    Точнее, чем определение по центроиду: тайл ВСЕГДА задан в своей
    UTM-зоне, а его центроид у краевых тайлов может попадать в соседнюю
    зону. Буква после номера зоны -- широтный пояс: C..M -- южное
    полушарие, N..X -- северное. Возвращает None, если код не разобран."""
    if not tile_code or len(tile_code) < 3:
        return None
    try:
        zone = int(tile_code[:2])
    except ValueError:
        return None
    band = tile_code[2].upper()
    if not ("C" <= band <= "X"):
        return None
    return f"EPSG:326{zone:02d}" if band >= "N" else f"EPSG:327{zone:02d}"


def compressed_profile(base_profile: dict, count: int, dtype: str = "uint16") -> dict:
    """Единый профиль вывода: заданный dtype + сжатие ZSTD + тайлинг."""
    profile = base_profile.copy()
    profile.update(count=count, dtype=dtype, tiled=True, blockxsize=256, blockysize=256, BIGTIFF="YES")
    profile.update(compress="ZSTD", zstd_level=9, predictor=2)
    return profile


def sorted_orders(orders):
    """Номера заказов по возрастанию как числа ('2000' < '2293'), с
    откатом на обычную сортировку строк, если номера не числовые."""
    try:
        return sorted(orders, key=lambda z: int(z))
    except (TypeError, ValueError):
        return sorted(orders, key=str)


def tile_attribute(kind: str, feat: dict):
    """Сырое значение атрибута со списком тайлов из свойств области интереса.

    kind: 's2' -> mrgs_tiles, 'landsat' -> landsat_grid.
    Для Landsat поддерживается и прежнее имя атрибута pr_tile -- чтобы
    старые geojson продолжали работать без переделки."""
    props = feat.get("properties", {}) or {}
    if kind == "s2":
        return props.get("mrgs_tiles")
    return props.get("landsat_grid") or props.get("pr_tile")

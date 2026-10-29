"""Общие мелкие утилиты, переиспользуемые в нескольких модулях."""
import errno
import http.client
import random
import re
import socket
import ssl
import time
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


# ============================== Временные сетевые сбои ==============================
#
# Обрыв соединения, SSL-ошибка, таймаут, HTTP 429/5xx -- это ВРЕМЕННЫЕ
# сбои: тот же запрос через несколько секунд/минут, как правило, проходит.
# Их нужно повторять, а не превращать в "ОШИБКУ ОБРАБОТКИ". Всё остальное
# (нет файла, нет каналов, битые данные, ошибка в коде, неверный пароль)
# -- настоящие ошибки, повтор которых ничего не даст.

class TransientError(RuntimeError):
    """Временный сбой, который не удалось преодолеть повторами на месте.
    Текст -- "Сервис [шаг]: Тип: сообщение", например
    "Google Drive [загрузка Мозаики/2000_....tif]: SSLEOFError: EOF ..."."""


_TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
_TRANSIENT_ERRNOS = {
    errno.ETIMEDOUT, errno.EPIPE, errno.ECONNABORTED, errno.ECONNREFUSED, errno.ECONNRESET,
    errno.ENETUNREACH, errno.ENETDOWN, errno.EHOSTUNREACH, errno.EADDRNOTAVAIL,
}


def http_status_of(exc):
    """HTTP-код из исключения googleapiclient (HttpError.resp.status) или
    requests (HTTPError.response.status_code). None, если кода нет."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _is_rate_limit_403(exc) -> bool:
    content = getattr(exc, "content", b"") or b""
    if isinstance(content, bytes):
        content = content.decode("utf-8", "replace")
    return "ratelimitexceeded" in str(content).lower()


def _is_transient_single(exc) -> bool:
    if isinstance(exc, TransientError):
        return True

    # Библиотеки импортируются лениво: utils.py используется и там, где
    # их может не быть (например, в облегчённом workflow бота).
    try:
        import requests
        if isinstance(exc, requests.RequestException):
            if isinstance(exc, (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)):
                return True
            status = http_status_of(exc)
            return status in _TRANSIENT_HTTP_STATUSES
    except ImportError:
        pass

    status = http_status_of(exc)
    if status is not None:
        return status in _TRANSIENT_HTTP_STATUSES or (status == 403 and _is_rate_limit_403(exc))

    try:
        import httplib2
        if isinstance(exc, httplib2.ServerNotFoundError):
            return True
    except ImportError:
        pass

    try:
        from google.auth import exceptions as gauth_exceptions
        if isinstance(exc, gauth_exceptions.TransportError):
            return True
        if isinstance(exc, gauth_exceptions.RefreshError):
            return bool(getattr(exc, "retryable", False))
    except ImportError:
        pass

    if isinstance(exc, ssl.SSLCertVerificationError):
        return False
    if isinstance(exc, (ssl.SSLError, ConnectionError, TimeoutError, socket.timeout,
                        socket.gaierror, http.client.HTTPException)):
        return True
    if isinstance(exc, OSError) and exc.errno in _TRANSIENT_ERRNOS:
        return True
    return False


def is_transient_error(exc) -> bool:
    """True, если исключение (или его причина, указанная через
    "raise ... from ...") -- временный сетевой сбой."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if _is_transient_single(exc):
            return True
        exc = exc.__cause__
    return False


def short_error(exc, limit: int = 300) -> str:
    """'Тип: сообщение' в одну строку и без HTML-простыней (HttpError
    Google кладёт в текст целую HTML-страницу ошибки)."""
    if isinstance(exc, TransientError):
        text = str(exc)
    else:
        status = http_status_of(exc)
        reason = getattr(exc, "reason", None)
        if status is not None and hasattr(exc, "resp"):
            text = f"{type(exc).__name__}: HTTP {status} {reason or ''}".rstrip()
        else:
            text = f"{type(exc).__name__}: {exc}"
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_error(exc) -> str:
    """Причина ошибки человеческим языком -- для уведомлений."""
    chain, cur = [], exc
    while cur is not None and all(cur is not e for e in chain):
        chain.append(cur)
        cur = cur.__cause__
    full_text = " ".join(str(e) for e in chain)

    if "Google Drive [" in full_text:
        service = "Google Drive"
    elif "CDSE" in full_text or "copernicus" in full_text.lower():
        service = "Copernicus (CDSE)"
    elif "EarthExplorer" in full_text or "usgs.gov" in full_text:
        service = "USGS EarthExplorer"
    else:
        service = None

    if not is_transient_error(exc):
        return "ошибка обработки (не сетевая -- повтор не поможет)"

    root = chain[-1]
    status = next((http_status_of(e) for e in chain if http_status_of(e) is not None), None)
    if status in (403, 429):
        text = "превышен лимит запросов"
    elif status is not None:
        text = f"временный сбой на стороне сервера (HTTP {status})"
    elif isinstance(root, (TimeoutError, socket.timeout)) or "timed out" in str(root).lower():
        text = "сервер не ответил вовремя (таймаут)"
    else:
        text = "обрыв связи"
    return f"{text} -- {service}" if service else text


def backoff_delay(attempt: int, base: float = 2.0, max_delay: float = 60.0) -> float:
    """Экспоненциальная пауза перед повтором номер attempt (1, 2, ...):
    2, 4, 8, 16, 32... секунд, не больше max_delay, со случайным разбросом
    +-30 %, чтобы параллельные потоки не повторяли запросы синхронно."""
    delay = min(max_delay, base * (2 ** (attempt - 1)))
    return delay * random.uniform(0.7, 1.3)


def retry_transient(fn, what: str, attempts: int = 6, base_delay: float = 2.0, max_delay: float = 60.0,
                    logger=None, on_retry=None):
    """Вызывает fn(), повторяя ТОЛЬКО временные сбои (см. is_transient_error)
    с экспоненциальной паузой. Настоящие ошибки пробрасываются сразу, без
    повторов. Если попытки кончились -- бросает TransientError с текстом
    "{what}: Тип: сообщение" (исходное исключение -- в __cause__).

    on_retry(exc) вызывается перед каждым повтором -- например, чтобы
    пересоздать HTTP-соединение."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if not is_transient_error(exc):
                raise
            if attempt >= attempts:
                raise TransientError(f"{what}: {short_error(exc)}") from exc
            delay = backoff_delay(attempt, base_delay, max_delay)
            if logger:
                logger.warning("%s: %s -- попытка %s/%s, повтор через %.0f с",
                               what, short_error(exc), attempt, attempts, delay)
            if on_retry:
                on_retry(exc)
            time.sleep(delay)

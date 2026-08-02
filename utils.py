from datetime import datetime
import pytz

import config

_tz = pytz.timezone(config.TIMEZONE)


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

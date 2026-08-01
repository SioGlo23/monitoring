"""
Конфигурация сервиса. Всё берётся из переменных окружения — никаких
паролей и токенов в коде. В Cloud Run секреты подключаются через
Secret Manager как env vars (см. README.md, шаг "Секреты").
"""
import os
from datetime import date


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return value


# --- Copernicus Dataspace (Sentinel-2) ---
COPERNICUS_USERNAME = _require("COPERNICUS_USERNAME")
COPERNICUS_PASSWORD = _require("COPERNICUS_PASSWORD")

# --- USGS M2M (Landsat) ---
M2M_USERNAME = os.environ.get("M2M_USERNAME")
M2M_PASSWORD = os.environ.get("M2M_PASSWORD")
M2M_TOKEN = os.environ.get("M2M_TOKEN")  # опционально, как запасной способ входа

# --- Хранилище (Google Cloud Storage) ---
GCS_BUCKET = _require("GCS_BUCKET")

# Пути ВНУТРИ бакета (не локальные!) до входных данных.
# Загрузите эти файлы в бакет один раз перед первым запуском (см. README).
AOI_GEOJSON_BLOB = os.environ.get("AOI_GEOJSON_BLOB", "config/All_ROI_2026_2.geojson")
GRID_GEOJSON_BLOB = os.environ.get("GRID_GEOJSON_BLOB", "config/GRID_Landsat.geojson")

# Префиксы для выходных данных внутри бакета
MODIS_PREFIX = os.environ.get("MODIS_PREFIX", "modis")
LOGS_PREFIX = os.environ.get("LOGS_PREFIX", "logs")
STATE_BLOB = os.environ.get("STATE_BLOB", "state/previous_state.json")

# --- Уведомления по почте (Gmail SMTP с app password) ---
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_APP_PASSWORD = os.environ.get("SMTP_APP_PASSWORD")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", SMTP_USER)

# --- Прочее ---
TIMEZONE = os.environ.get("TIMEZONE", "Europe/Moscow")

# Дата мониторинга: по умолчанию — сегодня, можно переопределить env-переменной
# (удобно для ручного теста конкретного дня)
MONITOR_DATE = os.environ.get("MONITOR_DATE") or date.today().isoformat()

# Сколько зон интереса обрабатывать параллельно за один прогон
MAX_PARALLEL_AOI = int(os.environ.get("MAX_PARALLEL_AOI", "8"))

LOCAL_TMP_DIR = "/tmp/s2_monitor"

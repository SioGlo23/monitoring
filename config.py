"""
Конфигурация сервиса. Всё берётся из переменных окружения (в GitHub
Actions -- из Secrets) -- никаких паролей и токенов в коде.
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

# --- USGS M2M + EarthExplorer (Landsat). Один и тот же аккаунт ERS
# используется и для поиска сцен (M2M API), и для скачивания файлов
# (веб-сессия earthexplorer.usgs.gov) -- как в исходном ноутбуке. ---
M2M_USERNAME = os.environ.get("M2M_USERNAME")
M2M_PASSWORD = os.environ.get("M2M_PASSWORD")
M2M_TOKEN = os.environ.get("M2M_TOKEN")  # опционально, запасной способ входа в M2M API

# --- Google Drive (замена Google Cloud Storage) ---
DRIVE_CLIENT_ID = _require("DRIVE_CLIENT_ID")
DRIVE_CLIENT_SECRET = _require("DRIVE_CLIENT_SECRET")
DRIVE_REFRESH_TOKEN = _require("DRIVE_REFRESH_TOKEN")
DRIVE_ROOT_FOLDER_ID = _require("DRIVE_ROOT_FOLDER_ID")

# Пути ВНУТРИ корневой папки на Диске до входных данных.
AOI_GEOJSON_BLOB = os.environ.get("AOI_GEOJSON_BLOB", "config/All_ROI_2026_2.geojson")
GRID_GEOJSON_BLOB = os.environ.get("GRID_GEOJSON_BLOB", "config/GRID_Landsat.geojson")

# Подпапки для выходных данных внутри корневой папки на Диске
MODIS_PREFIX = os.environ.get("MODIS_PREFIX", "modis")
LOGS_PREFIX = os.environ.get("LOGS_PREFIX", "logs")
STATE_BLOB = os.environ.get("STATE_BLOB", "state/previous_state.json")

MOSAICS_PREFIX = os.environ.get("MOSAICS_PREFIX", "Мозаики")
WATER_PREFIX = os.environ.get("WATER_PREFIX", "Мозаики/Water")
EIGHTBIT_PREFIX = os.environ.get("EIGHTBIT_PREFIX", "Мозаики/8bit")

QUEUE_PENDING_PREFIX = os.environ.get("QUEUE_PENDING_PREFIX", "queue/pending")
QUEUE_DONE_PREFIX = os.environ.get("QUEUE_DONE_PREFIX", "queue/done")

# --- Уведомления по почте (Gmail SMTP с app password) ---
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_APP_PASSWORD = os.environ.get("SMTP_APP_PASSWORD")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", SMTP_USER)

# --- Прочее ---
TIMEZONE = os.environ.get("TIMEZONE", "Europe/Moscow")

# Дата мониторинга: по умолчанию -- сегодня, можно переопределить
# переменной окружения (удобно для ручного теста конкретного дня)
MONITOR_DATE = os.environ.get("MONITOR_DATE") or date.today().isoformat()

# Сколько зон интереса обрабатывать параллельно за один прогон детекции
MAX_PARALLEL_AOI = int(os.environ.get("MAX_PARALLEL_AOI", "8"))

LOCAL_TMP_DIR = "/tmp/s2_monitor"

# ============================== ГОТОВНОСТЬ / ОБРАБОТКА (новое) ==============================

# Каналы, которые собираются в композит/мозаику -- единый набор для всего
# сервиса (как переменная `channels` в ноутбуке). Можно переопределить
# переменной окружения CHANNELS без правки кода.
CHANNELS = os.environ.get("CHANNELS", "SWIRNR")

CHANNELS_DICT = {
    "S2": {
        "RGB": ["B04", "B03", "B02"],
        "RGBN": ["B04", "B03", "B02", "B08"],
        "RGBNSWIR": ["B04", "B03", "B02", "B08", "B12"],
        "SWIRNR": ["B12", "B08", "B04"],
    },
    "L89": {
        "RGB": ["B4", "B3", "B2"],
        "RGBN": ["B4", "B3", "B2", "B5"],
        "RGBNSWIR": ["B4", "B3", "B2", "B5", "B7"],
        "SWIRNR": ["B7", "B5", "B4"],
    },
}
PAN_BAND_L89 = "B8"
PANSHARPEN_BLOCK_SIZE = int(os.environ.get("PANSHARPEN_BLOCK_SIZE", "2048"))


def selected_bands(satellite: str) -> list:
    return CHANNELS_DICT[satellite].get(CHANNELS, CHANNELS_DICT[satellite]["RGB"])


# Пороги воды и min/max для 8-бит -- свои для каждого спутника (см. v2 ноутбука)
WATER_THRESHOLDS = {
    "S2": {"porog1": 1500, "porog2": 1800, "ch1": 1, "ch2": 2},
    "L89": {"porog1": 5500, "porog2": 8500, "ch1": 1, "ch2": 2},
}
EIGHTBIT_MINMAX = {
    "S2": {"min_val": 1000, "max_val": 5000},
    "L89": {"min_val": 5000, "max_val": 23000},
}

# Сколько прогонов подряд число найденных сцен должно НЕ меняться,
# чтобы считать зону "созревшей" -- используется, когда явный список
# ожидаемых тайлов (mrgs_tiles / pr_tile) не задан.
LANDSAT_STABILITY_CYCLES = int(os.environ.get("LANDSAT_STABILITY_CYCLES", "2"))

# Порог средней облачности (%) по метаданным сцен -- выше этого значения
# обработка НЕ запускается. Средняя считается по всем сценам, попавшим
# в комплект по данному спутнику (для S2 -- по всем тайлам из mrgs_tiles,
# для Landsat -- по всем найденным/из pr_tile).
CLOUD_THRESHOLD_PERCENT = float(os.environ.get("CLOUD_THRESHOLD_PERCENT", "70"))

# Куда сохранять квиклуки (загрубленные превью) новых сцен
QUICKLOOKS_PREFIX = os.environ.get("QUICKLOOKS_PREFIX", "logs/quicklooks")

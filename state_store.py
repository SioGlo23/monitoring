"""
В исходном ноутбуке previous_s2 / previous_landsat жили только в
памяти процесса, пока крутился while True. В GitHub Actions каждый
запуск — это новый, чистый процесс, поэтому "память" между прогонами
нужно хранить снаружи. Здесь она хранится JSON-файлами на Google Drive:

  - state/previous_state.json — какие сцены уже видели (для детекта новизны)
  - state/cycle_counter.json  — номер цикла логов/карт, обнуляется каждый день
"""
import config
import storage

CYCLE_COUNTER_BLOB = "state/cycle_counter.json"
RUN_HISTORY_BLOB = "logs/run_history.log"


def load_previous_state() -> dict:
    """Возвращает {"s2": {...}, "landsat": {...}} с прошлого прогона."""
    state = storage.download_json(config.STATE_BLOB, default=None)
    if state is None:
        return {"s2": {}, "landsat": {}}
    return state


def save_state(current_s2: dict, current_landsat: dict) -> None:
    storage.upload_json(config.STATE_BLOB, {"s2": current_s2, "landsat": current_landsat})


def next_cycle_number(date_str: str) -> int:
    """Номер очередного лога/карты за день date_str (YYYY-MM-DD).
    Обнуляется автоматически при смене даты."""
    data = storage.download_json(CYCLE_COUNTER_BLOB, default=None)
    if not data or data.get("date") != date_str:
        data = {"date": date_str, "count": 0}
    data["count"] += 1
    storage.upload_json(CYCLE_COUNTER_BLOB, data)
    return data["count"]


def append_run_history(line: str) -> None:
    """Добавляет одну строку в единый файл истории всех прогонов
    (logs/run_history.log) — растёт со временем, по одной строке на
    каждый запуск пайплайна, независимо от того, писался ли полный
    лог/карта в этот раз."""
    try:
        existing = storage.download_text(RUN_HISTORY_BLOB)
    except FileNotFoundError:
        existing = ""
    updated = existing + line + "\n"
    storage.upload_bytes(updated.encode("utf-8"), RUN_HISTORY_BLOB, content_type="text/plain; charset=utf-8")

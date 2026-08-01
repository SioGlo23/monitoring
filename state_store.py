"""
В исходном ноутбуке previous_s2 / previous_landsat жили только в
памяти процесса, пока крутился while True. В облаке каждый запуск —
это новый, чистый процесс (Cloud Run Job), поэтому "память" между
прогонами нужно хранить снаружи. Здесь она хранится одним JSON-файлом
в том же бакете GCS.
"""
import config
import storage


def load_previous_state() -> dict:
    """Возвращает {"s2": {...}, "landsat": {...}} с прошлого прогона."""
    state = storage.download_json(config.STATE_BLOB, default=None)
    if state is None:
        return {"s2": {}, "landsat": {}}
    return state


def save_state(current_s2: dict, current_landsat: dict) -> None:
    storage.upload_json(config.STATE_BLOB, {"s2": current_s2, "landsat": current_landsat})

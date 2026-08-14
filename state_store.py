"""
Внешняя "память" сервиса на Google Drive (процесс GitHub Actions не
живёт между запусками):

  state/previous_state.json     -- какие сцены S2/Landsat уже видели
  state/cycle_counter.json      -- номер цикла логов/карт (обнуляется каждый день)
  state/stability_counters.json -- счётчик "стабильности" числа найденных
                                    сцен Landsat/S2, когда явный список
                                    ожидаемых тайлов не задан
  state/processing_decisions.json -- решения по обработке (queued/done/
                                    skipped_cloud) на каждую AOI+дата+спутник,
                                    чтобы не ставить одно и то же в очередь
                                    повторно и не пересчитывать то, что
                                    уже решено
  logs/run_history.log          -- единая история всех прогонов
  queue/pending/*.json          -- задания, ожидающие обработки (process.py)
  queue/done/*.json             -- обработанные (или упавшие) задания
"""
import config
import storage

CYCLE_COUNTER_BLOB = "state/cycle_counter.json"
RUN_HISTORY_BLOB = "logs/run_history.log"
STABILITY_BLOB = "state/stability_counters.json"
DECISIONS_BLOB = "state/processing_decisions.json"


# ============================== S2/Landsat "видели ли уже" ==============================

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
    """Добавляет одну строку в единый файл истории всех прогонов. Новые
    записи -- сверху (самый свежий прогон всегда первая строка файла)."""
    try:
        existing = storage.download_text(RUN_HISTORY_BLOB)
    except FileNotFoundError:
        existing = ""
    updated = line + "\n" + existing
    storage.upload_bytes(updated.encode("utf-8"), RUN_HISTORY_BLOB, content_type="text/plain; charset=utf-8")


# ============================== Готовность (стабильность числа сцен) ==============================

def update_stability_counter(kind: str, zakaz, date_str: str, current_count: int) -> tuple:
    """kind -- 's2' или 'landsat'. Возвращает (stable_cycles, current_count).
    stable_cycles растёт на 1 каждый прогон, пока current_count не меняется;
    при изменении числа сцен сбрасывается на 1 (текущий прогон = первое
    наблюдение нового значения)."""
    data = storage.download_json(STABILITY_BLOB, default={})
    key = f"{kind}_{zakaz}_{date_str}"
    prev = data.get(key, {"count": -1, "stable_cycles": 0})

    stable_cycles = prev["stable_cycles"] + 1 if prev["count"] == current_count else 1
    data[key] = {"count": current_count, "stable_cycles": stable_cycles}
    storage.upload_json(STABILITY_BLOB, data)
    return stable_cycles, current_count


# ============================== Решения по обработке ==============================

def get_processing_decision(zakaz, date_str: str, satellite: str):
    data = storage.download_json(DECISIONS_BLOB, default={})
    return data.get(f"{zakaz}_{date_str}_{satellite}")


def set_processing_decision(zakaz, date_str: str, satellite: str, decision: str) -> None:
    data = storage.download_json(DECISIONS_BLOB, default={})
    data[f"{zakaz}_{date_str}_{satellite}"] = decision
    storage.upload_json(DECISIONS_BLOB, data)


def get_all_decisions_for_date(date_str: str) -> dict:
    """Все известные решения (queued/done/skipped_cloud/failed) за
    указанную дату в виде {zakaz: {satellite: status}} -- чтобы показать
    полную картину в письмах (та же форма, что письмо про очередь)."""
    data = storage.download_json(DECISIONS_BLOB, default={})
    result = {}
    for key, status in data.items():
        parts = key.split("_")
        if len(parts) != 3:
            continue
        zakaz, key_date, satellite = parts
        if key_date != date_str:
            continue
        result.setdefault(zakaz, {})[satellite] = status
    return result


# ============================== Очередь заданий на обработку ==============================

def write_queue_job(blob_path: str, job: dict) -> None:
    storage.upload_json(blob_path, job)


def list_pending_jobs() -> list:
    """Список (blob_path, job_dict) для всех заданий в очереди."""
    paths = storage.list_files(config.QUEUE_PENDING_PREFIX)
    jobs = []
    for p in paths:
        if not p.endswith(".json"):
            continue
        job = storage.download_json(p, default=None)
        if job:
            jobs.append((p, job))
    return jobs


def mark_job_done(job_blob_path: str, job: dict) -> None:
    filename = job_blob_path.rsplit("/", 1)[-1]
    storage.upload_json(f"{config.QUEUE_DONE_PREFIX}/{filename}", job)
    storage.delete_blob(job_blob_path)
    set_processing_decision(job["zakaz"], job["date"], job["satellite"], "done")


def mark_job_failed(job_blob_path: str, job: dict, error: str) -> None:
    """Задание с ошибкой уходит в queue/done/FAILED_*.json и удаляется из
    очереди. Решение processing_decision выставляется в "failed" (было
    "queued" -- вводило в заблуждение в письмах: задание давно упало, а
    показывалось как "в очереди"). Побочный эффект: raз "failed" не
    входит в список статусов, блокирующих повторную постановку в
    очередь (readiness.py), при следующем обнаружении тех же готовых
    сцен задание автоматически попробует поставиться в очередь заново."""
    failed_job = dict(job)
    failed_job["error"] = str(error)
    filename = job_blob_path.rsplit("/", 1)[-1]
    storage.upload_json(f"{config.QUEUE_DONE_PREFIX}/FAILED_{filename}", failed_job)
    storage.delete_blob(job_blob_path)
    set_processing_decision(job["zakaz"], job["date"], job["satellite"], "failed")

"""
Лог операций тяжёлой обработки (process.py) -- формат СТРОКА В СТРОКУ
повторяет log_operation()/log_history из +S2_L89.ipynb (Блок 3):

    "{timestamp} | {order} | {operation} | {status} | {filename} | {size_mb:.2f} МБ | {duration:.2f} сек"

Как и в ноутбуке, история копится в памяти за весь прогон и при каждой
записи целиком (отсортированная по убыванию) перезаписывается в новый
файл с именем "{timestamp}_{order}_{operation}.txt". Единственное
отличие от ноутбука -- пишется в ОТДЕЛЬНУЮ папку (config.PROCESS_LOGS_PREFIX),
а не в общую "logs", чтобы не смешиваться с логами мониторинга.
"""
import logging
import os

import config
import storage
import utils

logger = logging.getLogger("s2monitor.process_log")

_log_history = []  # копится за весь прогон process.py, как в ноутбуке


def log_operation(order, operation: str, file_path: str = None, duration: float = 0.0, status: str = "OK") -> None:
    timestamp = utils.now_local().strftime("%Y%m%dT%H%M%S")

    size_mb = 0.0
    filename = ""
    if file_path and os.path.exists(file_path):
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        filename = os.path.basename(file_path)

    line = f"{timestamp} | {order} | {operation} | {status} | {filename} | {size_mb:.2f} МБ | {duration:.2f} сек"
    _log_history.append(line)

    sorted_log = sorted(_log_history, reverse=True)
    log_filename = f"{timestamp}_{order}_{operation.replace(' ', '_')}.txt"
    blob_path = f"{config.PROCESS_LOGS_PREFIX}/{log_filename}"

    try:
        storage.upload_bytes(
            ("\n".join(sorted_log) + "\n").encode("utf-8"), blob_path, content_type="text/plain; charset=utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        # Логирование не должно ронять саму обработку.
        logger.warning("Не удалось записать лог операции на Drive: %s", exc)

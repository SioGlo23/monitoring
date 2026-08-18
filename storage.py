"""
Обёртка над Google Drive API. Заменяет собой Google Cloud Storage --
все файлы (растры, логи, карты, состояние, очередь заданий) хранятся
в одной папке на личном Google Drive пользователя, через официальный
API с OAuth-refresh-токеном.

ВАЖНО про потоки: google-api-python-client (через httplib2) НЕ является
потокобезопасным -- один и тот же service-объект нельзя дёргать из
нескольких потоков одновременно (monitor.py обрабатывает несколько зон
интереса параллельно через ThreadPoolExecutor). Поэтому ВСЕ функции
этого модуля целиком сериализованы одним общим RLock: одновременно с
Drive работает только один поток, остальные ждут своей очереди. Без
этого возникает повреждение памяти на уровне C (сегфолты, "corrupted
unsorted chunks", случайные SSL-ошибки) -- именно это и происходило.

Все функции работают с "виртуальными путями" вида "logs/2026.../x.json"
внутри корневой папки DRIVE_ROOT_FOLDER_ID. Папки создаются автоматически
по мере необходимости.
"""
import io
import json
import logging
import os
import threading

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

import config

logger = logging.getLogger("s2monitor.storage")

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

_service = None
_folder_cache = {}  # "путь/до/папки" -> folder_id, чтобы не искать заново каждый раз

# Один общий лок на ВСЕ обращения к Drive из этого процесса. RLock (не
# Lock), т.к. функции вызывают друг друга изнутри уже захваченной
# блокировки (например, download_json -> download_text -> download_bytes).
_drive_lock = threading.RLock()


def _get_service():
    global _service
    if _service is not None:
        return _service
    creds = Credentials(
        token=None,
        refresh_token=config.DRIVE_REFRESH_TOKEN,
        client_id=config.DRIVE_CLIENT_ID,
        client_secret=config.DRIVE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=_SCOPES,
    )
    _service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _service


def _escape(name: str) -> str:
    return name.replace("'", "\\'")


def _find_child(name: str, parent_id: str, folder_only: bool = False):
    service = _get_service()
    q = f"name = '{_escape(name)}' and '{parent_id}' in parents and trashed = false"
    if folder_only:
        q += " and mimeType = 'application/vnd.google-apps.folder'"
    res = service.files().list(q=q, spaces="drive", fields="files(id, name)").execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def _ensure_folder(name: str, parent_id: str) -> str:
    existing = _find_child(name, parent_id, folder_only=True)
    if existing:
        return existing
    service = _get_service()
    metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    folder = service.files().create(body=metadata, fields="id").execute()
    logger.info("Создана папка на Google Drive: %s", name)
    return folder["id"]


def _resolve_path(blob_path: str):
    """'logs/2026.../file.json' -> (folder_id для logs/2026..., 'file.json').
    Вызывается только изнутри уже заблокированных публичных функций."""
    parts = blob_path.strip("/").split("/")
    filename = parts[-1]
    folder_parts = parts[:-1]

    cache_key = "/".join(folder_parts)
    if cache_key in _folder_cache:
        return _folder_cache[cache_key], filename

    parent_id = config.DRIVE_ROOT_FOLDER_ID
    for part in folder_parts:
        parent_id = _ensure_folder(part, parent_id)
    _folder_cache[cache_key] = parent_id
    return parent_id, filename


def _resolve_folder(folder_path: str) -> str:
    """Как _resolve_path, но весь путь целиком -- цепочка папок (без
    имени файла). Тоже только изнутри уже заблокированных функций."""
    parts = [p for p in folder_path.strip("/").split("/") if p]
    cache_key = "/".join(parts)
    if cache_key in _folder_cache:
        return _folder_cache[cache_key]

    parent_id = config.DRIVE_ROOT_FOLDER_ID
    for part in parts:
        parent_id = _ensure_folder(part, parent_id)
    _folder_cache[cache_key] = parent_id
    return parent_id


def blob_exists(blob_path: str) -> bool:
    with _drive_lock:
        folder_id, filename = _resolve_path(blob_path)
        return _find_child(filename, folder_id) is not None


def download_bytes(blob_path: str) -> bytes:
    with _drive_lock:
        folder_id, filename = _resolve_path(blob_path)
        file_id = _find_child(filename, folder_id)
        if not file_id:
            raise FileNotFoundError(f"Не найден файл на Google Drive: {blob_path}")

        service = _get_service()
        request = service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()


def download_text(blob_path: str) -> str:
    return download_bytes(blob_path).decode("utf-8")


def download_to_file(blob_path: str, local_path: str) -> None:
    """Скачивает файл с Drive прямо на диск -- используется для
    восстановления уже готовых промежуточных/финальных артефактов
    (канал, композит, бандл, мозаика...), чтобы не пересчитывать их
    заново на каждом прогоне."""
    data = download_bytes(blob_path)
    os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
    with open(local_path, "wb") as f:
        f.write(data)


def download_json(blob_path: str, default=None):
    try:
        return json.loads(download_text(blob_path))
    except FileNotFoundError:
        return default


def _upload_media(folder_id: str, filename: str, media, existing_id):
    service = _get_service()
    if existing_id:
        service.files().update(fileId=existing_id, media_body=media).execute()
        return existing_id
    metadata = {"name": filename, "parents": [folder_id]}
    created = service.files().create(body=metadata, media_body=media, fields="id").execute()
    return created["id"]


def upload_json(blob_path: str, data) -> None:
    with _drive_lock:
        folder_id, filename = _resolve_path(blob_path)
        existing_id = _find_child(filename, folder_id)
        payload = json.dumps(data, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json; charset=utf-8")
        _upload_media(folder_id, filename, media, existing_id)
        logger.info("Сохранён JSON на Google Drive: %s", blob_path)


def upload_bytes(data: bytes, blob_path: str, content_type: str = None) -> str:
    with _drive_lock:
        folder_id, filename = _resolve_path(blob_path)
        existing_id = _find_child(filename, folder_id)
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=content_type or "application/octet-stream")
        file_id = _upload_media(folder_id, filename, media, existing_id)
        return f"https://drive.google.com/file/d/{file_id}/view"


def upload_file(local_path: str, blob_path: str, content_type: str = None, public: bool = False) -> str:
    """Загружает локальный файл на Drive, возвращает ссылку. Если
    public=True -- дополнительно открывает файл на просмотр по ссылке
    ("anyone with the link") и возвращает ссылку в формате, пригодном
    для встраивания как <img src="...">  (нужно для квиклуков на карте)."""
    with _drive_lock:
        folder_id, filename = _resolve_path(blob_path)
        existing_id = _find_child(filename, folder_id)
        media = MediaFileUpload(local_path, mimetype=content_type, resumable=True)
        file_id = _upload_media(folder_id, filename, media, existing_id)
        logger.info("Загружен файл на Google Drive: %s", blob_path)

        if public:
            try:
                _get_service().permissions().create(
                    fileId=file_id, body={"type": "anyone", "role": "reader"}
                ).execute()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Не удалось открыть публичный доступ для %s: %s -- картинка не будет "
                    "отображаться на карте (но файл на Диске сохранён)", blob_path, exc,
                )
            # ВАЖНО: именно /thumbnail, а не /uc?export=view. Google закрыл
            # uc?export=view для встраивания в <img> -- он отдаёт HTML-страницу
            # просмотрщика вместо байтов картинки, из-за чего <img> показывает
            # "битую" иконку. /thumbnail отдаёт настоящее изображение.
            return f"https://drive.google.com/thumbnail?id={file_id}&sz=w800"

        return f"https://drive.google.com/file/d/{file_id}/view"


def list_files(folder_prefix: str) -> list:
    """Список виртуальных путей 'folder_prefix/filename' для всех файлов
    (не папок) внутри указанной папки. Папка создаётся, если её ещё нет --
    тогда возвращается пустой список."""
    with _drive_lock:
        folder_id = _resolve_folder(folder_prefix)
        service = _get_service()
        q = f"'{folder_id}' in parents and trashed = false and mimeType != 'application/vnd.google-apps.folder'"
        res = service.files().list(q=q, spaces="drive", fields="files(id, name)").execute()
        prefix_clean = folder_prefix.strip("/")
        return [f"{prefix_clean}/{f['name']}" for f in res.get("files", [])]


def delete_blob(blob_path: str) -> None:
    with _drive_lock:
        folder_id, filename = _resolve_path(blob_path)
        file_id = _find_child(filename, folder_id)
        if file_id:
            _get_service().files().delete(fileId=file_id).execute()


def ensure_local_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

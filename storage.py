"""
Обёртка над Google Drive API. Заменяет собой Google Cloud Storage —
все файлы (растры, логи, карты, состояние) хранятся в одной папке на
личном Google Drive пользователя, как и в исходном ноутбуке
(drive.mount), но через официальный API с OAuth-refresh-токеном —
без Cloud Storage и без биллинг-аккаунта.

Все функции работают с "виртуальными путями" вида "logs/2026.../x.json"
внутри корневой папки DRIVE_ROOT_FOLDER_ID — так же, как раньше работали
blob-пути внутри бакета GCS. Папки создаются автоматически по мере
необходимости.
"""
import io
import json
import logging
import os

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

import config

logger = logging.getLogger("s2monitor.storage")

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

_service = None
_folder_cache = {}  # "путь/до/папки" -> folder_id, чтобы не искать заново каждый раз


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
    """'logs/2026.../file.json' -> (folder_id для logs/2026..., 'file.json')."""
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


def blob_exists(blob_path: str) -> bool:
    folder_id, filename = _resolve_path(blob_path)
    return _find_child(filename, folder_id) is not None


def download_text(blob_path: str) -> str:
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
    return buf.getvalue().decode("utf-8")


def download_json(blob_path: str, default=None):
    try:
        return json.loads(download_text(blob_path))
    except FileNotFoundError:
        return default


def _upload_media(folder_id: str, filename: str, media, existing_id: str | None) -> str:
    service = _get_service()
    if existing_id:
        service.files().update(fileId=existing_id, media_body=media).execute()
        return existing_id
    metadata = {"name": filename, "parents": [folder_id]}
    created = service.files().create(body=metadata, media_body=media, fields="id").execute()
    return created["id"]


def upload_json(blob_path: str, data) -> None:
    folder_id, filename = _resolve_path(blob_path)
    existing_id = _find_child(filename, folder_id)
    payload = json.dumps(data, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json; charset=utf-8")
    _upload_media(folder_id, filename, media, existing_id)
    logger.info("Сохранён JSON на Google Drive: %s", blob_path)


def upload_bytes(data: bytes, blob_path: str, content_type: str = None) -> str:
    folder_id, filename = _resolve_path(blob_path)
    existing_id = _find_child(filename, folder_id)
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=content_type or "application/octet-stream")
    file_id = _upload_media(folder_id, filename, media, existing_id)
    return f"https://drive.google.com/file/d/{file_id}/view"


def upload_file(local_path: str, blob_path: str, content_type: str = None) -> str:
    """Загружает локальный файл на Drive, возвращает ссылку на просмотр."""
    folder_id, filename = _resolve_path(blob_path)
    existing_id = _find_child(filename, folder_id)
    media = MediaFileUpload(local_path, mimetype=content_type, resumable=True)
    file_id = _upload_media(folder_id, filename, media, existing_id)
    logger.info("Загружен файл на Google Drive: %s", blob_path)
    return f"https://drive.google.com/file/d/{file_id}/view"


def ensure_local_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

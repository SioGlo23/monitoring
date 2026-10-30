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

УСТОЙЧИВОСТЬ К СБОЯМ СЕТИ. Каждое обращение к Drive идёт через
_with_retries(): при обрыве соединения/SSL-ошибке/таймауте/HTTP 429 и 5xx
запрос повторяется до _MAX_ATTEMPTS раз с нарастающей паузой, а перед
повтором HTTP-соединение создаётся заново (_reset_connection). Это лечит
"EOF occurred in violation of protocol": пока process.py минутами качает
каналы с CDSE, простаивающее TLS-соединение с Google закрывается на той
стороне, и первый следующий запрос в него падает. Большие файлы грузятся
и скачиваются кусками по _CHUNK_SIZE с продолжением с места обрыва, а
скачивание пишет прямо в файл, не держа его целиком в памяти. Если
попытки кончились -- бросается utils.TransientError с указанием шага,
например "Google Drive [загрузка Мозаики/2000_....tif]: SSLEOFError: ...".
"""
import io
import json
import logging
import os
import threading

import google_auth_httplib2
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload, build_http

import config
import utils

logger = logging.getLogger("s2monitor.storage")

# storage.py опирается на новые функции utils.py. Если utils.py в
# репозитории старый или залился обрезанным -- падаем сразу и понятно,
# а не на первом обращении к Drive посреди прогона.
if not all(hasattr(utils, name) for name in ("retry_transient", "TransientError", "http_status_of")):
    raise RuntimeError(
        "utils.py в репозитории устарел -- нет функций retry_transient/TransientError/http_status_of, "
        "нужных storage.py. Перезалейте utils.py вместе с storage.py (Add file -> Upload files)."
    )

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# Таймаут одной сетевой операции (подключение / чтение / запись), сек.
_HTTP_TIMEOUT = int(os.environ.get("DRIVE_HTTP_TIMEOUT", "120"))
# Сколько раз пробовать одно обращение к Drive при временных сбоях
# (паузы между попытками: ~2, 4, 8, 16, 32 с).
_MAX_ATTEMPTS = int(os.environ.get("DRIVE_MAX_ATTEMPTS", "6"))
# Быстрые повторы внутри самой библиотеки Google (на каждую попытку).
_NUM_RETRIES = 2
# Размер куска при загрузке/скачивании больших файлов.
_CHUNK_SIZE = 16 * 1024 * 1024

_credentials = None
_service = None
_folder_cache = {}  # "путь/до/папки" -> folder_id, чтобы не искать заново каждый раз

# Один общий лок на ВСЕ обращения к Drive из этого процесса. RLock (не
# Lock), т.к. функции вызывают друг друга изнутри уже захваченной
# блокировки (например, download_json -> download_text -> download_bytes).
_drive_lock = threading.RLock()

# Глубина вложенных _with_retries: повторы делает только самый внешний
# вызов, чтобы попытки не перемножались (например, _ensure_folder ->
# _find_child).
_retry_depth = threading.local()


def _get_credentials():
    global _credentials
    if _credentials is None:
        _credentials = Credentials(
            token=None,
            refresh_token=config.DRIVE_REFRESH_TOKEN,
            client_id=config.DRIVE_CLIENT_ID,
            client_secret=config.DRIVE_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=_SCOPES,
        )
    return _credentials


def _new_http():
    """Новое HTTP-соединение с Drive: явный таймаут + авторизация по
    refresh-токену (access-токен обновляется автоматически).

    ВАЖНО: соединение строится через build_http() библиотеки Google, а не
    голым httplib2.Http(). build_http() убирает код 308 из списка
    перенаправлений: Drive отвечает 308 на каждый принятый кусок
    resumable-загрузки, а голый httplib2 принимает его за редирект и
    падает с RedirectMissingLocation на любом файле больше одного куска."""
    http = build_http()
    http.timeout = _HTTP_TIMEOUT
    return google_auth_httplib2.AuthorizedHttp(_get_credentials(), http=http)


def _get_service():
    global _service
    if _service is None:
        _service = build("drive", "v3", http=_new_http(), cache_discovery=False)
    return _service


def _reset_connection(_exc=None) -> None:
    """Выбрасывает текущее (возможно, мёртвое) соединение -- следующий
    запрос откроет новое."""
    global _service
    old, _service = _service, None
    try:
        if old is not None:
            old._http.http.close()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass


def _with_retries(what: str, fn):
    """Выполняет fn() с повторами при временных сбоях (см. docstring
    модуля). Вызывается только изнутри _drive_lock."""
    depth = getattr(_retry_depth, "value", 0)
    if depth:
        return fn()
    _retry_depth.value = 1
    try:
        return utils.retry_transient(
            fn, f"Google Drive [{what}]", attempts=_MAX_ATTEMPTS,
            logger=logger, on_retry=_reset_connection,
        )
    except utils.TransientError as exc:
        logger.error("%s", exc)
        raise
    finally:
        _retry_depth.value = 0


def _escape(name: str) -> str:
    return name.replace("'", "\\'")


def _find_child(name: str, parent_id: str, folder_only: bool = False):
    q = f"name = '{_escape(name)}' and '{parent_id}' in parents and trashed = false"
    if folder_only:
        q += " and mimeType = 'application/vnd.google-apps.folder'"

    def _do():
        return _get_service().files().list(
            q=q, spaces="drive", fields="files(id, name)"
        ).execute(num_retries=_NUM_RETRIES)

    files = _with_retries(f"поиск {name}", _do).get("files", [])
    return files[0]["id"] if files else None


def _ensure_folder(name: str, parent_id: str) -> str:
    # Поиск и создание -- одна попытка целиком: если ответ на создание
    # потерялся, повтор сначала найдёт уже созданную папку, а не сделает
    # вторую такую же.
    def _do():
        existing = _find_child(name, parent_id, folder_only=True)
        if existing:
            return existing
        metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
        folder = _get_service().files().create(body=metadata, fields="id").execute()
        logger.info("Создана папка на Google Drive: %s", name)
        return folder["id"]

    return _with_retries(f"папка {name}", _do)


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


def _download_stream(file_id: str, fd, what: str) -> None:
    """Скачивает файл кусками по _CHUNK_SIZE (заголовок Range) и пишет в
    открытый на запись файл fd. Кусок пишется только целиком, поэтому
    после обрыва скачивание продолжается с конца последнего записанного
    куска, а не с начала. Повторы -- на каждый кусок отдельно."""
    state = {"offset": 0, "total": None}

    def _next_piece():
        start = state["offset"]
        service = _get_service()
        uri = service.files().get_media(fileId=file_id).uri
        resp, content = service._http.request(  # noqa: SLF001
            uri, "GET", headers={"range": f"bytes={start}-{start + _CHUNK_SIZE - 1}"},
        )
        if resp.status == 206:
            fd.write(content)
            state["offset"] = start + len(content)
            content_range = resp.get("content-range", "")
            if "/" in content_range and content_range.rsplit("/", 1)[1].isdigit():
                state["total"] = int(content_range.rsplit("/", 1)[1])
            elif not content:
                state["total"] = state["offset"]
        elif resp.status == 200:
            # Сервер проигнорировал Range и отдал файл целиком.
            fd.seek(0)
            fd.truncate()
            fd.write(content)
            state["offset"] = state["total"] = len(content)
        elif resp.status == 416 and start == 0:
            state["total"] = 0  # пустой файл
        else:
            raise HttpError(resp, content, uri=uri)

    while state["total"] is None or state["offset"] < state["total"]:
        _with_retries(f"{what}, байт {state['offset']}+", _next_piece)


def _download_by_path(blob_path: str, fd) -> None:
    folder_id, filename = _resolve_path(blob_path)
    file_id = _find_child(filename, folder_id)
    if not file_id:
        raise FileNotFoundError(f"Не найден файл на Google Drive: {blob_path}")
    _download_stream(file_id, fd, f"скачивание {blob_path}")


def download_bytes(blob_path: str) -> bytes:
    """Для небольших файлов (JSON, тексты). Растры качайте через
    download_to_file -- он не держит файл в памяти."""
    with _drive_lock:
        buf = io.BytesIO()
        _download_by_path(blob_path, buf)
        return buf.getvalue()


def download_text(blob_path: str) -> str:
    return download_bytes(blob_path).decode("utf-8")


def download_to_file(blob_path: str, local_path: str) -> None:
    """Скачивает файл с Drive прямо на диск -- используется для
    восстановления уже готовых промежуточных/финальных артефактов
    (канал, композит, бандл, мозаика...), чтобы не пересчитывать их
    заново на каждом прогоне. Пишет во временный .part и переименовывает
    только после успешного скачивания -- недокачанный файл никогда не
    окажется под итоговым именем."""
    os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
    tmp_path = local_path + ".part"
    with _drive_lock:
        try:
            with open(tmp_path, "wb") as f:
                _download_by_path(blob_path, f)
            os.replace(tmp_path, local_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


def download_json(blob_path: str, default=None):
    try:
        return json.loads(download_text(blob_path))
    except FileNotFoundError:
        return default


def _upload_small(blob_path: str, payload: bytes, mimetype: str) -> str:
    """Небольшой файл одним запросом. Поиск существующего файла входит в
    ту же попытку: если ответ на создание потерялся, повтор найдёт уже
    созданный файл и обновит его, а не сделает дубликат."""
    folder_id, filename = _resolve_path(blob_path)

    def _do():
        existing_id = _find_child(filename, folder_id)
        media = MediaIoBaseUpload(io.BytesIO(payload), mimetype=mimetype)
        service = _get_service()
        if existing_id:
            service.files().update(fileId=existing_id, media_body=media).execute()
            return existing_id
        metadata = {"name": filename, "parents": [folder_id]}
        return service.files().create(body=metadata, media_body=media, fields="id").execute()["id"]

    return _with_retries(f"загрузка {blob_path}", _do)


def upload_json(blob_path: str, data) -> None:
    with _drive_lock:
        payload = json.dumps(data, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        _upload_small(blob_path, payload, "application/json; charset=utf-8")
        logger.info("Сохранён JSON на Google Drive: %s", blob_path)


def upload_bytes(data: bytes, blob_path: str, content_type: str = None) -> str:
    with _drive_lock:
        file_id = _upload_small(blob_path, data, content_type or "application/octet-stream")
        return f"https://drive.google.com/file/d/{file_id}/view"


def _upload_resumable(local_path: str, blob_path: str, content_type: str = None) -> str:
    """Загрузка файла кусками по _CHUNK_SIZE (resumable upload). При обрыве
    соединение пересоздаётся, а загрузка продолжается с того места,
    которое подтвердил сервер (запрос состояния делает сама библиотека),
    а не с нуля. Если сессия загрузки протухла (HTTP 404/410) --
    начинается заново."""
    folder_id, filename = _resolve_path(blob_path)
    state = {"request": None}

    def _do():
        if state["request"] is None:
            existing_id = _find_child(filename, folder_id)
            media = MediaFileUpload(local_path, mimetype=content_type, resumable=True, chunksize=_CHUNK_SIZE)
            files = _get_service().files()
            if existing_id:
                state["request"] = files.update(fileId=existing_id, media_body=media, fields="id")
            else:
                metadata = {"name": filename, "parents": [folder_id]}
                state["request"] = files.create(body=metadata, media_body=media, fields="id")

        request = state["request"]
        response = None
        try:
            while response is None:
                # http передаём явно: после _reset_connection у запроса
                # остаётся ссылка на старое (мёртвое) соединение.
                _, response = request.next_chunk(http=_get_service()._http, num_retries=_NUM_RETRIES)  # noqa: SLF001
        except HttpError as exc:
            if utils.http_status_of(exc) in (404, 410):
                state["request"] = None
                raise utils.TransientError("сессия загрузки устарела -- начинаю заново") from exc
            raise
        return response["id"]

    return _with_retries(f"загрузка {blob_path}", _do)


def upload_file(local_path: str, blob_path: str, content_type: str = None, public: bool = False) -> str:
    """Загружает локальный файл на Drive, возвращает ссылку. Если
    public=True -- дополнительно открывает файл на просмотр по ссылке
    ("anyone with the link") и возвращает ссылку в формате, пригодном
    для встраивания как <img src="...">  (нужно для квиклуков на карте)."""
    with _drive_lock:
        file_id = _upload_resumable(local_path, blob_path, content_type)
        logger.info("Загружен файл на Google Drive: %s", blob_path)

        if public:
            try:
                _with_retries(
                    f"публичный доступ {blob_path}",
                    lambda: _get_service().permissions().create(
                        fileId=file_id, body={"type": "anyone", "role": "reader"}
                    ).execute(num_retries=_NUM_RETRIES),
                )
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
        q = f"'{folder_id}' in parents and trashed = false and mimeType != 'application/vnd.google-apps.folder'"
        res = _with_retries(
            f"список {folder_prefix}",
            lambda: _get_service().files().list(
                q=q, spaces="drive", fields="files(id, name)"
            ).execute(num_retries=_NUM_RETRIES),
        )
        prefix_clean = folder_prefix.strip("/")
        return [f"{prefix_clean}/{f['name']}" for f in res.get("files", [])]


def delete_blob(blob_path: str) -> None:
    with _drive_lock:
        folder_id, filename = _resolve_path(blob_path)

        # Поиск входит в попытку: если удаление на самом деле прошло, а
        # ответ потерялся, повтор просто не найдёт файл.
        def _do():
            file_id = _find_child(filename, folder_id)
            if file_id:
                _get_service().files().delete(fileId=file_id).execute()

        _with_retries(f"удаление {blob_path}", _do)


def ensure_local_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

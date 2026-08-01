"""
ОДНОРАЗОВЫЙ скрипт. Запускается ЛОКАЛЬНО на вашем компьютере (не в
GitHub Actions!) один раз перед первым деплоем. Делает три вещи:

  1. Открывает браузер для входа в ваш Google-аккаунт (OAuth) —
     разрешаете доступ, скрипт получает refresh_token.
  2. Создаёт на вашем Google Drive папку "S2_monitoring" со
     структурой config/ modis/ logs/ state/.
  3. Загружает в config/ ваши geojson-файлы (положите их рядом с этим
     скриптом перед запуском).

В конце печатает 4 значения — их нужно один раз вручную вставить в
GitHub Secrets репозитория. После этого скрипт больше не нужен —
дальше сервис работает через refresh_token, без браузера.

Установка перед запуском:
    pip install google-auth-oauthlib google-api-python-client
"""
import json
import os

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# Скачивается в Google Cloud Console: APIs & Services -> Credentials ->
# Create Credentials -> OAuth client ID -> Desktop app -> Download JSON.
# Переименуйте скачанный файл в client_secret.json и положите рядом.
CLIENT_SECRET_FILE = "client_secret.json"

# Положите эти два файла рядом со скриптом перед запуском —
# те же, что использовались в исходном Colab-ноутбуке.
AOI_GEOJSON_LOCAL = "All_ROI_2026_2.geojson"
GRID_GEOJSON_LOCAL = "GRID_Landsat.geojson"

ROOT_FOLDER_NAME = "S2_monitoring"


def ensure_folder(service, name, parent_id=None):
    q = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    res = service.files().list(q=q, fields="files(id, name)").execute()
    files = res.get("files", [])
    if files:
        print(f"Папка уже существует: {name}")
        return files[0]["id"]
    metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = service.files().create(body=metadata, fields="id").execute()
    print(f"Создана папка: {name}")
    return folder["id"]


def upload_file(service, local_path, folder_id):
    filename = os.path.basename(local_path)
    media = MediaFileUpload(local_path, resumable=True)
    metadata = {"name": filename, "parents": [folder_id]}
    service.files().create(body=metadata, media_body=media, fields="id").execute()
    print(f"Загружен файл: {filename}")


def main():
    if not os.path.exists(CLIENT_SECRET_FILE):
        raise SystemExit(
            f"Не найден {CLIENT_SECRET_FILE}. Скачайте OAuth-credentials из Google "
            "Cloud Console (см. README.md, шаг 3) и положите рядом со скриптом."
        )

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
    # Откроет браузер для входа в ваш Google-аккаунт
    creds = flow.run_local_server(port=0)

    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    root_id = ensure_folder(service, ROOT_FOLDER_NAME)
    config_id = ensure_folder(service, "config", root_id)
    ensure_folder(service, "modis", root_id)
    ensure_folder(service, "logs", root_id)
    ensure_folder(service, "state", root_id)

    if os.path.exists(AOI_GEOJSON_LOCAL):
        upload_file(service, AOI_GEOJSON_LOCAL, config_id)
    else:
        print(f"⚠️  Не найден {AOI_GEOJSON_LOCAL} рядом со скриптом — загрузите позже вручную в config/")

    if os.path.exists(GRID_GEOJSON_LOCAL):
        upload_file(service, GRID_GEOJSON_LOCAL, config_id)
    else:
        print(f"⚠️  Не найден {GRID_GEOJSON_LOCAL} рядом со скриптом — загрузите позже вручную в config/")

    with open(CLIENT_SECRET_FILE) as f:
        client_data = json.load(f)["installed"]

    print("\n" + "=" * 78)
    print("ГОТОВО. Вставьте эти 4 значения в GitHub Secrets репозитория")
    print("(Settings -> Secrets and variables -> Actions -> New repository secret):")
    print("=" * 78)
    print(f"DRIVE_CLIENT_ID       = {client_data['client_id']}")
    print(f"DRIVE_CLIENT_SECRET   = {client_data['client_secret']}")
    print(f"DRIVE_REFRESH_TOKEN   = {creds.refresh_token}")
    print(f"DRIVE_ROOT_FOLDER_ID  = {root_id}")
    print("=" * 78)

    if not creds.refresh_token:
        print(
            "\n⚠️  refresh_token пустой! Обычно это значит, что вы уже давали "
            "разрешение этому приложению раньше. Зайдите на "
            "https://myaccount.google.com/permissions, отзовите доступ для "
            "вашего OAuth-приложения и запустите скрипт заново."
        )


if __name__ == "__main__":
    main()

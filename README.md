# S2 Monitor Service — версия без Google Cloud (GitHub Actions + Google Drive)

Полностью бесплатная схема, без привязки карты где-либо:

- **Планировщик и исполнение** — GitHub Actions (запускает скрипт каждые 10 минут)
- **Хранилище** — ваш личный Google Drive (через Drive API, без Cloud Storage/биллинга)

Функционально всё то же самое: скачивает MODIS, ищет новые Sentinel-2/Landsat
по вашим зонам интереса, ведёт лог, шлёт email при новых снимках.

## Архитектура

```
GitHub Actions (cron: */10 * * * *)
        │  каждые 10 минут запускает раннер
        ▼
python monitor.py  (один прогон, без Docker — ставится requirements.txt)
        │  1. читает AOI + сетку Landsat с Google Drive
        │  2. параллельно опрашивает Copernicus / USGS M2M / NASA GIBS
        │  3. сравнивает с прошлым состоянием (тоже на Google Drive)
        │  4. пишет лог + карту на Google Drive, шлёт email при новых сценах
        │  5. завершается
        ▼
Google Drive, папка "S2_monitoring"
   ├── config/    — ваши geojson (загружаются один раз через setup_drive.py)
   ├── modis/     — скачанные растры MODIS (.tif)
   ├── logs/      — JSON-логи и HTML-карты
   └── state/     — previous_state.json (память между прогонами)
```

Ни Google Cloud Run, ни Cloud Storage, ни привязка карты — не задействованы.

---

## Шаг 1. Завести GitHub-аккаунт

1. Откройте https://github.com/signup
2. Введите email, пароль, придумайте username
3. Подтвердите email по ссылке из письма

Это бесплатно, карта нигде не запрашивается.

## Шаг 2. Создать репозиторий

1. На github.com нажмите **+** в правом верхнем углу → **New repository**
2. Имя, например `s2-monitor`
3. **Public** (обязательно — для приватного репозитория бесплатных минут
   GitHub Actions мало для 144 запусков в сутки, для публичного лимита нет)
4. Не добавляйте README/gitignore на этом экране — они уже есть в архиве
5. **Create repository**

### Загрузить код в репозиторий

Проще всего — прямо в браузере: на странице пустого репозитория есть
ссылка **"uploading an existing file"** → перетащите туда все файлы и
папки из этого архива (включая скрытую папку `.github/` — если браузер
её не показывает при перетаскивании, используйте способ через git ниже).

Либо через git в терминале (Cloud Shell, WSL, обычный терминал — где
удобно):

```bash
cd s2_monitor_service
git init
git remote add origin https://github.com/ВАШ_USERNAME/s2-monitor.git
git add .
git commit -m "initial commit"
git branch -M main
git push -u origin main
```

При первом `git push` GitHub попросит авторизацию — либо через браузер,
либо через Personal Access Token вместо пароля (GitHub больше не
принимает обычный пароль для git-операций): Settings → Developer
settings → Personal access tokens → Generate new token (достаточно
прав `repo`), и этот токен один раз вставить вместо пароля.

**Важно:** `.gitignore` в архиве уже настроен так, чтобы `*.geojson`,
`.env` и `client_secret.json` не попали в публичный репозиторий —
проверьте перед пушем, что вы их туда не добавили руками.

## Шаг 3. Google Cloud — только ради Drive API (без биллинга)

Здесь Google Cloud используется исключительно как место для создания
OAuth-credentials к Drive API — **включение платных сервисов и
привязка карты не требуются**.

1. https://console.cloud.google.com/projectcreate — создайте новый проект (любое имя)
2. В поиске сверху введите **"Google Drive API"** → откройте → **Enable**
   (эта кнопка не просит биллинг — Drive API бесплатный)
3. **OAuth consent screen** (экран согласия):
   - User Type: **External**
   - App name: любое (например `s2-monitor`), ваш email в контактах
   - Scopes: можно пропустить этот шаг, добавим позже при запросе доступа
   - Test users: добавьте свой же email
   - Сохраните
4. **Credentials** → **Create Credentials** → **OAuth client ID**:
   - Application type: **Desktop app**
   - Имя любое
   - **Create** → скачайте JSON (кнопка "Download JSON")
   - Переименуйте скачанный файл в `client_secret.json`

### Важный нюанс — не пропустите

Пока OAuth-приложение в статусе **Testing**, выданный refresh-токен
живёт всего **7 дней**, после чего сервис молча перестанет иметь
доступ к Drive. Чтобы токен не протухал:

- Зайдите в **OAuth consent screen** → кнопка **"PUBLISH APP"**
- Google покажет предупреждение "приложение не проверено" — это
  нормально для scope `drive.file` (он не требует прохождения полной
  верификации Google), просто подтвердите публикацию
- При следующей авторизации (шаг 4) вы увидите экран
  "Google hasn't verified this app" — нажмите **Advanced** →
  **Go to s2-monitor (unsafe)**. Это ваше собственное приложение,
  это безопасно.

## Шаг 4. Локально: получить refresh-токен и залить geojson на Drive

На своём компьютере (не в GitHub, не в облаке):

```bash
pip install google-auth-oauthlib google-api-python-client

# Положите рядом друг с другом:
#   setup_drive.py
#   client_secret.json      (из шага 3)
#   All_ROI_2026_2.geojson
#   GRID_Landsat.geojson

python setup_drive.py
```

Откроется браузер, войдите под своим Google-аккаунтом, разрешите
доступ. Скрипт создаст на вашем Google Drive папку `S2_monitoring` со
структурой `config/ modis/ logs/ state/`, загрузит туда geojson и в
конце напечатает:

```
DRIVE_CLIENT_ID       = ...
DRIVE_CLIENT_SECRET   = ...
DRIVE_REFRESH_TOKEN   = ...
DRIVE_ROOT_FOLDER_ID  = ...
```

Сохраните эти 4 строки — они понадобятся в следующем шаге.

## Шаг 5. Секреты в GitHub

**Прежде чем продолжить: смените пароли**, которые были в открытом
виде в исходном ноутбуке (Copernicus, USGS M2M, Gmail app password) —
ниже нужно вводить уже новые.

В репозитории: **Settings → Secrets and variables → Actions → New
repository secret**. Добавьте по одному:

| Имя secret | Значение |
|---|---|
| `COPERNICUS_USERNAME` | ваш логин Copernicus Dataspace |
| `COPERNICUS_PASSWORD` | новый пароль Copernicus |
| `M2M_USERNAME` | логин USGS M2M |
| `M2M_PASSWORD` | новый пароль USGS M2M |
| `M2M_TOKEN` | токен USGS M2M (если используете) |
| `DRIVE_CLIENT_ID` | из шага 4 |
| `DRIVE_CLIENT_SECRET` | из шага 4 |
| `DRIVE_REFRESH_TOKEN` | из шага 4 |
| `DRIVE_ROOT_FOLDER_ID` | из шага 4 |
| `SMTP_USER` | ваш Gmail |
| `SMTP_APP_PASSWORD` | новый app password Gmail |
| `NOTIFY_EMAIL` | куда слать уведомления (обычно тот же Gmail) |

## Шаг 6. Проверка

1. Вкладка **Actions** в репозитории → слева workflow **"S2 Monitor"**
2. Кнопка **Run workflow** (справа) → **Run workflow** — запустит вручную,
   не дожидаясь расписания
3. Откройте запуск, смотрите логи по шагам — если что-то упало,
   текст ошибки будет прямо там
4. Откройте свой Google Drive → папку `S2_monitoring/logs/` — должен
   появиться JSON-лог и HTML-карта этого прогона

Если всё прошло — с этого момента workflow сам запускается каждые 10
минут по расписанию, без вашего участия.

## Экономия: когда сервис пишет на Drive

Как и обсуждали — не на каждый прогон, а только когда реально найдены
новые сцены, плюс раз в час "heartbeat"-лог для контроля, что сервис
жив. Это уже встроено в `monitor.py`.

## Честные ограничения этой схемы

- **Точность расписания.** GitHub гарантирует минимум 5-минутный
  интервал, но не гарантирует, что запуск случится ровно в срок — в
  пиковые моменты возможна задержка 10-30 минут. Для вашей задачи
  (не миллисекундный мониторинг) это не критично.
- **60 дней тишины = авто-отключение.** Если в репозитории совсем нет
  активности 60 дней, GitHub сам выключает расписание. Решено
  отдельным workflow `keepalive.yml` — он раз в неделю делает пустой
  коммит именно для того, чтобы этого не произошло.
- **Публичный репозиторий.** Код виден всем, но секреты (пароли,
  токены) — нет, они зашифрованы и не выводятся даже в логах. Сами
  geojson с зонами интереса в репозиторий не попадают вообще (лежат
  только на вашем Drive) — за это отвечает `.gitignore`.
- **Refresh-токен Drive.** Если вы отзовёте доступ приложению в
  https://myaccount.google.com/permissions (случайно или намеренно) —
  сервис перестанет писать на Диск, придётся повторить шаг 4.

## Если что-то пойдёт не так

Самый частый сценарий — ошибка в логе конкретного шага в Actions.
Скопируйте текст ошибки, разберём.

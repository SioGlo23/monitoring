"""
Telegram-бот: приём команд и меню выбора заказов.

Как он работает в условиях GitHub Actions. У бота нет постоянно
работающего сервера, поэтому вебхук использовать не получится. Вместо
этого бот работает короткими сессиями: workflow запускается по
расписанию, скрипт несколько минут слушает Telegram методом длинного
опроса (getUpdates с таймаутом) и завершается. Следующий запуск
подхватывает всё, что накопилось, начиная с сохранённого offset.

Список заказов НИКОГДА не хардкодится -- он каждый раз читается из того
же файла областей интереса на Google Drive, который использует
мониторинг. Поэтому меню автоматически показывает актуальный набор
заказов: обновили geojson -- в боте сразу новый список.

Команды:
    /start, /orders  -- меню выбора заказов
    /status          -- что сейчас с моими заказами
    /whoami          -- показать свой Telegram id (для белого списка)
    /stop            -- отписаться от всех уведомлений
"""
import json
import logging
import time

import config
import state_store
import storage
import telegram_notify as tg
import utils

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("s2monitor.telegram_bot")

OFFSET_BLOB = "state/telegram_offset.json"

_HELP_TEXT = (
    "Бот присылает уведомления о новых снимках и о статусе обработки заказов.\n\n"
    "Команды:\n"
    "/orders — выбрать заказы, по которым присылать уведомления\n"
    "/status — что сейчас с моими заказами\n"
    "/whoami — показать мой Telegram id\n"
    "/stop — отписаться от всех уведомлений"
)

_STATUS_RU = {
    "queued": "в очереди на загрузку",
    "done": "готово",
    "skipped_cloud": "отбраковано по облачности",
    "failed": "ошибка обработки",
}


# ============================== Заказы из областей интереса ==============================

def load_available_orders() -> list:
    """Актуальный список заказов -- прямо из geojson областей интереса на
    Google Drive. Специально читаем файл напрямую, а не через aoi_source:
    тому нужен geopandas, а боту тяжёлые гео-библиотеки ни к чему (это
    заметно ускоряет запуск каждой сессии)."""
    try:
        raw = json.loads(storage.download_text(config.AOI_GEOJSON_BLOB))
    except Exception as exc:  # noqa: BLE001
        logger.error("Не удалось прочитать области интереса: %s", exc)
        return []

    orders = []
    for feature in raw.get("features", []):
        zakaz = (feature.get("properties") or {}).get("zakaz")
        if zakaz is not None:
            orders.append(str(zakaz))
    return utils.sorted_orders(set(orders))


# ============================== Меню ==============================

def _menu_markup(orders: list, sub: dict) -> dict:
    selected = set(sub.get("orders", []))
    is_all = bool(sub.get("all"))

    rows, row = [], []
    for zakaz in orders:
        mark = "✅" if (is_all or zakaz in selected) else "▫️"
        row.append({"text": f"{mark} {zakaz}", "callback_data": f"t:{zakaz}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([
        {"text": "Выбрать все", "callback_data": "all"},
        {"text": "Снять все", "callback_data": "none"},
    ])
    rows.append([{"text": "Готово", "callback_data": "done"}])
    return {"inline_keyboard": rows}


def _selection_summary(orders: list, sub: dict) -> str:
    if sub.get("all"):
        return "Сейчас выбраны: все заказы (включая те, что появятся позже)."
    selected = utils.sorted_orders(set(sub.get("orders", [])) & set(orders))
    if not selected:
        return "Сейчас не выбрано ни одного заказа — уведомления приходить не будут."
    return "Сейчас выбраны: " + ", ".join(selected)


def _send_menu(chat_id: str, subs: dict) -> None:
    orders = load_available_orders()
    if not orders:
        tg.send_message(chat_id, "Не удалось получить список заказов. Попробуйте позже.")
        return

    sub = subs.setdefault(chat_id, {"orders": [], "all": False})
    text = (
        "Выберите заказы, по которым присылать уведомления.\n"
        "Нажимайте на номера, чтобы включить или выключить их.\n\n"
        + _selection_summary(orders, sub)
    )
    tg.send_message(chat_id, text, reply_markup=_menu_markup(orders, sub))


def _send_status(chat_id: str, subs: dict) -> None:
    orders = load_available_orders()
    sub = subs.get(chat_id, {})
    mine = tg.subscriber_orders(sub, orders)

    if not mine:
        tg.send_message(chat_id, "У вас не выбрано ни одного заказа. Откройте /orders, чтобы выбрать.")
        return

    today = utils.now_local().strftime("%Y-%m-%d")
    try:
        decisions = state_store.get_all_decisions_for_date(today)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось получить статусы: %s", exc)
        decisions = {}

    lines = [f"Статус на {today}:", ""]
    for zakaz in utils.sorted_orders(mine):
        per_sat = decisions.get(zakaz)
        if not per_sat:
            lines.append(f"Заказ {zakaz}: пока ничего не запускалось")
            continue
        lines.append(f"Заказ {zakaz}:")
        for satellite, status in per_sat.items():
            lines.append(f"  • {satellite}: {_STATUS_RU.get(status, status)}")
    tg.send_message(chat_id, "\n".join(lines))


# ============================== Обработка апдейтов ==============================

def _is_allowed(user_id) -> bool:
    if not config.TELEGRAM_ALLOWED_USERS:
        return True
    return str(user_id) in config.TELEGRAM_ALLOWED_USERS


def _handle_message(msg: dict, subs: dict) -> bool:
    """Возвращает True, если подписки изменились и их надо сохранить."""
    chat_id = str(msg.get("chat", {}).get("id"))
    user = msg.get("from", {}) or {}
    text = (msg.get("text") or "").strip()
    command = text.split()[0].lower().split("@")[0] if text else ""

    if command == "/whoami":
        tg.send_message(chat_id, f"Ваш Telegram id: {user.get('id')}")
        return False

    if not _is_allowed(user.get("id")):
        tg.send_message(
            chat_id,
            "Доступ к этому боту ограничен. Если это ваш бот -- добавьте свой id "
            "в секрет TELEGRAM_ALLOWED_USERS (узнать id: /whoami).",
        )
        logger.info("Отклонён неразрешённый пользователь: %s (%s)", user.get("id"), user.get("username"))
        return False

    if command in ("/start", "/orders", "/settings"):
        sub = subs.setdefault(chat_id, {"orders": [], "all": False})
        sub["username"] = user.get("username") or user.get("first_name") or ""
        sub["updated_msk"] = utils.now_local().strftime("%Y-%m-%d %H:%M:%S")
        if command == "/start":
            tg.send_message(chat_id, _HELP_TEXT)
        _send_menu(chat_id, subs)
        return True

    if command == "/status":
        _send_status(chat_id, subs)
        return False

    if command == "/stop":
        if subs.pop(chat_id, None) is not None:
            tg.send_message(chat_id, "Вы отписаны от всех уведомлений. Вернуться: /orders")
            return True
        tg.send_message(chat_id, "Вы и так не подписаны. Подписаться: /orders")
        return False

    tg.send_message(chat_id, _HELP_TEXT)
    return False


def _handle_callback(callback: dict, subs: dict) -> bool:
    """Нажатие на кнопку меню. Возвращает True, если надо сохранить подписки."""
    callback_id = callback.get("id")
    message = callback.get("message", {}) or {}
    chat_id = str(message.get("chat", {}).get("id"))
    message_id = message.get("message_id")
    data = callback.get("data") or ""
    user = callback.get("from", {}) or {}

    if not _is_allowed(user.get("id")):
        tg.api("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Доступ ограничен"})
        return False

    orders = load_available_orders()
    sub = subs.setdefault(chat_id, {"orders": [], "all": False})

    if data == "done":
        tg.api("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Сохранено"})
        tg.api("editMessageText", {
            "chat_id": chat_id, "message_id": message_id,
            "text": "Настройки сохранены.\n\n" + _selection_summary(orders, sub)
                    + "\n\nИзменить в любой момент: /orders",
        })
        return False

    if data == "all":
        # Флаг all -- это "все, включая будущие": если добавите новую
        # область интереса, уведомления по ней начнут приходить сами.
        sub["all"] = True
        sub["orders"] = list(orders)
    elif data == "none":
        sub["all"] = False
        sub["orders"] = []
    elif data.startswith("t:"):
        zakaz = data[2:]
        current = set(sub.get("orders", []))
        if sub.get("all"):
            # Разворачиваем "все" в явный список, иначе снять один заказ
            # было бы невозможно.
            current = set(orders)
        if zakaz in current:
            current.discard(zakaz)
        else:
            current.add(zakaz)
        sub["orders"] = utils.sorted_orders(current)
        # Если в итоге отмечены все существующие заказы -- считаем это
        # выбором "все", чтобы будущие заказы тоже подхватывались.
        sub["all"] = set(sub["orders"]) == set(orders) and bool(orders)
    else:
        tg.api("answerCallbackQuery", {"callback_query_id": callback_id})
        return False

    sub["username"] = user.get("username") or user.get("first_name") or ""
    sub["updated_msk"] = utils.now_local().strftime("%Y-%m-%d %H:%M:%S")

    tg.api("answerCallbackQuery", {"callback_query_id": callback_id})
    tg.api("editMessageText", {
        "chat_id": chat_id, "message_id": message_id,
        "text": "Выберите заказы, по которым присылать уведомления.\n"
                "Нажимайте на номера, чтобы включить или выключить их.\n\n"
                + _selection_summary(orders, sub),
        "reply_markup": _menu_markup(orders, sub),
    })
    return True


# ============================== Цикл опроса ==============================

def _load_offset() -> int:
    data = storage.download_json(OFFSET_BLOB, default={}) or {}
    return int(data.get("offset", 0))


def _save_offset(offset: int) -> None:
    storage.upload_json(OFFSET_BLOB, {"offset": offset})


def _register_commands() -> None:
    tg.api("setMyCommands", {"commands": [
        {"command": "orders", "description": "Выбрать заказы для уведомлений"},
        {"command": "status", "description": "Что сейчас с моими заказами"},
        {"command": "whoami", "description": "Показать мой Telegram id"},
        {"command": "stop", "description": "Отписаться от уведомлений"},
    ]})


def run_once() -> dict:
    if not tg.enabled():
        logger.error("TELEGRAM_BOT_TOKEN не задан -- бот не запускается")
        return {"handled": 0}

    _register_commands()

    deadline = time.time() + config.TELEGRAM_POLL_SECONDS
    offset = _load_offset()
    subs = tg.load_subscribers()
    subs_dirty = False
    handled = 0

    logger.info("Бот слушает Telegram %s сек (offset=%s)", config.TELEGRAM_POLL_SECONDS, offset)

    while time.time() < deadline:
        remaining = int(deadline - time.time())
        if remaining <= 0:
            break
        # Telegram сам держит соединение до timeout секунд, если новых
        # сообщений нет -- это и есть длинный опрос, он не жжёт запросы
        # вхолостую.
        poll_timeout = max(1, min(45, remaining))

        payload = {"timeout": poll_timeout, "allowed_updates": ["message", "callback_query"]}
        if offset:
            payload["offset"] = offset + 1

        updates = tg.api("getUpdates", payload, timeout=poll_timeout + 20)
        if updates is None:
            # Сетевая ошибка или конфликт двух одновременных опросов --
            # переждём, чтобы не крутить цикл вхолостую.
            time.sleep(3)
            continue

        for update in updates:
            offset = max(offset, int(update.get("update_id", 0)))
            try:
                if "message" in update:
                    subs_dirty |= _handle_message(update["message"], subs)
                elif "callback_query" in update:
                    subs_dirty |= _handle_callback(update["callback_query"], subs)
                handled += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("Ошибка обработки апдейта %s: %s", update.get("update_id"), exc)

        if updates:
            _save_offset(offset)
            if subs_dirty:
                tg.save_subscribers(subs)
                subs_dirty = False

    if subs_dirty:
        tg.save_subscribers(subs)

    logger.info("Сессия бота завершена. Обработано апдейтов: %s", handled)
    return {"handled": handled, "subscribers": len(subs)}


if __name__ == "__main__":
    run_once()

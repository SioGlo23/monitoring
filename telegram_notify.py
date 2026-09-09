"""
Отправка уведомлений в Telegram.

Этот модуль отвечает ТОЛЬКО за отправку (его дёргает notifier.py вместе
с почтой). Приём команд и меню выбора заказов -- в telegram_bot.py.

Список подписчиков лежит на Google Drive рядом с остальным состоянием:
    state/telegram_subscribers.json
    {
      "123456789": {
          "orders": ["2000", "2293"],   # явно выбранные заказы
          "all": false,                 # или "все, включая будущие"
          "username": "igrek",
          "updated_msk": "2026-08-26 21:00:00"
      }
    }

Флаг "all" важен: если пользователь выбрал все заказы, то при добавлении
НОВОЙ области интереса он начнёт получать уведомления и по ней тоже,
без повторного захода в меню.

Сообщения отправляются ОБЫЧНЫМ текстом, без Markdown/HTML. Это
сознательно: имена сцен вида S2C_MSIL1C_20260818T065621_N0512_... полны
подчёркиваний, которые Telegram в режиме Markdown принял бы за разметку
и либо испортил бы текст, либо вернул ошибку разбора.
"""
import logging

import requests

import config
import storage
import utils

logger = logging.getLogger("s2monitor.telegram")

SUBSCRIBERS_BLOB = "state/telegram_subscribers.json"

_API_URL = "https://api.telegram.org/bot{token}/{method}"

# Telegram режет сообщения длиннее 4096 символов -- берём с запасом
_MAX_MESSAGE_LEN = 3800


def enabled() -> bool:
    """Настроен ли бот вообще. Если токена нет -- весь модуль работает
    вхолостую, ничего не ломая: почта продолжает ходить как обычно."""
    return bool(config.TELEGRAM_BOT_TOKEN)


def api(method: str, payload: dict = None, timeout: int = 30):
    """Вызов Telegram Bot API. Возвращает result или None при любой
    ошибке (ошибки только логируются -- уведомления не должны ронять
    ни детекцию, ни обработку)."""
    if not enabled():
        return None
    url = _API_URL.format(token=config.TELEGRAM_BOT_TOKEN, method=method)
    try:
        resp = requests.post(url, json=payload or {}, timeout=timeout)
        data = resp.json()
        if not data.get("ok"):
            logger.warning("Telegram %s: %s", method, data.get("description"))
            return None
        return data.get("result")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram %s: ошибка запроса: %s", method, exc)
        return None


# ============================== Подписчики ==============================

def load_subscribers() -> dict:
    return storage.download_json(SUBSCRIBERS_BLOB, default={}) or {}


def save_subscribers(data: dict) -> None:
    storage.upload_json(SUBSCRIBERS_BLOB, data)


def subscriber_orders(sub: dict, available_orders) -> set:
    """Какие из СУЩЕСТВУЮЩИХ сейчас заказов интересны подписчику.

    Пересечение с available_orders нужно на случай, если область интереса
    удалили из geojson: в подписке номер мог остаться, но слать по нему
    уже нечего."""
    available = set(available_orders)
    if sub.get("all"):
        return available
    return {str(o) for o in sub.get("orders", [])} & available


# ============================== Отправка ==============================

def _split_message(text: str) -> list:
    """Режет длинный текст по строкам, чтобы уложиться в лимит Telegram."""
    if len(text) <= _MAX_MESSAGE_LEN:
        return [text]

    chunks, current = [], []
    current_len = 0
    for line in text.split("\n"):
        # +1 -- перевод строки
        if current_len + len(line) + 1 > _MAX_MESSAGE_LEN and current:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def send_message(chat_id, text: str, reply_markup: dict = None) -> bool:
    """Отправляет сообщение (при необходимости разбив на части).
    Клавиатура прикрепляется к последней части."""
    if not enabled():
        return False

    chunks = _split_message(text)
    ok = True
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        if api("sendMessage", payload) is None:
            ok = False
    return ok


def broadcast_by_order(header: str, lines_by_order: dict, footer: str = None) -> None:
    """Рассылка с фильтрацией по заказам.

    lines_by_order -- {номер_заказа: [строки про этот заказ]}. Каждому
    подписчику уходит только то, что относится к ЕГО заказам; если
    пересечения нет -- сообщение вообще не отправляется.
    """
    if not enabled():
        return
    if not lines_by_order:
        return

    subscribers = load_subscribers()
    if not subscribers:
        logger.info("Telegram: подписчиков нет -- рассылка пропущена")
        return

    available = set(lines_by_order.keys())
    sent = 0

    for chat_id, sub in subscribers.items():
        mine = subscriber_orders(sub, available)
        if not mine:
            continue

        parts = [header, ""]
        for zakaz in utils.sorted_orders(mine):
            parts.append(f"Заказ {zakaz}:")
            parts.extend(lines_by_order[zakaz])
            parts.append("")
        if footer:
            parts.append(footer)

        if send_message(chat_id, "\n".join(parts).strip()):
            sent += 1

    logger.info("Telegram: уведомление отправлено %s подписчик(ам)", sent)

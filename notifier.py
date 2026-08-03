"""
Уведомления. Браузерные Notification/alert из исходного ноутбука убраны —
на сервере без браузера они бессмысленны. Email — через SMTP с app
password (пароль — из env).

В письме — только название каждой сцены (кратко, для быстрого
просмотра). Полные данные (Start Time, время публикации, время
обнаружения — всё по МСК) сохраняются в JSON-логе и на карте (HTML),
сюда не дублируются.

Структура письма:
  1. Новые снимки за этот прогон (сгруппированы по заказу)
  2. Все остальные снимки, которые уже были известны раньше
     (тоже по заказам, для полной картины дня)
"""
import logging
import smtplib
from email.mime.text import MIMEText

import config

logger = logging.getLogger("s2monitor.notifier")


def _sorted_zakazy(keys):
    try:
        return sorted(keys, key=lambda z: int(z))
    except (TypeError, ValueError):
        return sorted(keys, key=str)


def _format_line(p: dict, kind: str) -> str:
    return f"  • [{kind}] {p.get('Name', '—')}"


def notify_new_scenes(current_s2: dict, current_landsat: dict, map_url: str | None) -> None:
    all_zakazy = _sorted_zakazy(set(current_s2.keys()) | set(current_landsat.keys()))

    new_block, old_block = [], []
    zakazy_with_new = 0

    for zakaz in all_zakazy:
        s2_list = current_s2.get(zakaz, [])
        l_list = current_landsat.get(zakaz, [])

        s2_new = [p for p in s2_list if p.get("is_new")]
        s2_old = [p for p in s2_list if not p.get("is_new")]
        l_new = [p for p in l_list if p.get("is_new")]
        l_old = [p for p in l_list if not p.get("is_new")]

        if s2_new or l_new:
            zakazy_with_new += 1
            new_block.append(f"Заказ {zakaz}:")
            new_block += [_format_line(p, "S2") for p in s2_new]
            new_block += [_format_line(p, "Landsat") for p in l_new]

        if s2_old or l_old:
            old_block.append(f"Заказ {zakaz}:")
            old_block += [_format_line(p, "S2") for p in s2_old]
            old_block += [_format_line(p, "Landsat") for p in l_old]

    if not new_block:
        # Уведомление вызывается только когда changed_info непустой,
        # но на всякий случай — если вдруг блока новых нет, письмо не шлём.
        return

    body_parts = ["НОВЫЕ СНИМКИ:", ""] + new_block

    if old_block:
        body_parts += ["", "-" * 40, "", "УЖЕ БЫЛИ ИЗВЕСТНЫ РАНЕЕ (для справки):", ""] + old_block

    if map_url:
        body_parts += ["", f"Карта (полные данные по каждой сцене): {map_url}"]

    body = "\n".join(body_parts)
    subject = f"Новые сцены S2/Landsat — {zakazy_with_new} заказ(ов)"

    if not (config.SMTP_USER and config.SMTP_APP_PASSWORD and config.NOTIFY_EMAIL):
        logger.warning("SMTP не настроен — письмо не отправлено. Текст уведомления:\n%s", body)
        return

    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = config.SMTP_USER
        msg["To"] = config.NOTIFY_EMAIL

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(config.SMTP_USER, config.SMTP_APP_PASSWORD)
            server.sendmail(config.SMTP_USER, config.NOTIFY_EMAIL, msg.as_string())
        logger.info("Письмо отправлено на %s", config.NOTIFY_EMAIL)
    except Exception as exc:  # noqa: BLE001
        logger.error("Ошибка отправки письма: %s", exc)

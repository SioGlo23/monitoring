"""
Уведомления. Браузерные Notification/alert из исходного ноутбука убраны —
на сервере без браузера они бессмысленны. Email оставлен через SMTP
с app password (тем же способом, что и раньше, но пароль — из env).
"""
import logging
import smtplib
from email.mime.text import MIMEText

import config

logger = logging.getLogger("s2monitor.notifier")


def notify_new_scenes(changed_info: dict, map_url: str | None) -> None:
    if not changed_info:
        return

    lines = ["Появились новые спутниковые данные:"]
    for zakaz, info in changed_info.items():
        parts = []
        if info.get("s2_new"):
            parts.append(f"S2: {len(info['s2_new'])}")
        if info.get("landsat_new"):
            parts.append(f"Landsat: {len(info['landsat_new'])}")
        if parts:
            lines.append(f"• Заказ {zakaz} — {', '.join(parts)} новых сцен")

    if map_url:
        lines.append(f"\nКарта: {map_url}")

    body = "\n".join(lines)
    subject = f"Новые сцены S2/Landsat — {len(changed_info)} заказ(ов)"

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

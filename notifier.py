"""
Уведомления по email (SMTP с app password). Помимо исходного письма о
новых сценах -- добавлены короткие письма о ходе тяжёлой обработки:
поставлена в очередь / пропущена из-за облачности / готова / упала с
ошибкой.
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


def _send_email(subject: str, body: str) -> None:
    if not (config.SMTP_USER and config.SMTP_APP_PASSWORD and config.NOTIFY_EMAIL):
        logger.warning("SMTP не настроен -- письмо не отправлено. Тема: %s\n%s", subject, body)
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
        logger.info("Письмо отправлено на %s: %s", config.NOTIFY_EMAIL, subject)
    except Exception as exc:  # noqa: BLE001
        logger.error("Ошибка отправки письма (%s): %s", subject, exc)


def notify_new_scenes(current_s2: dict, current_landsat: dict, map_url) -> None:
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
        return

    body_parts = ["НОВЫЕ СНИМКИ:", ""] + new_block
    if old_block:
        body_parts += ["", "-" * 40, "", "УЖЕ БЫЛИ ИЗВЕСТНЫ РАНЕЕ (для справки):", ""] + old_block
    if map_url:
        body_parts += ["", f"Карта (полные данные по каждой сцене): {map_url}"]

    _send_email(f"Новые сцены S2/Landsat -- {zakazy_with_new} заказ(ов)", "\n".join(body_parts))


def notify_processing_queued(zakaz, date_str, satellite, scenes_count, cloud_percent) -> None:
    body = (
        f"Заказ {zakaz}, спутник {satellite}, дата {date_str}.\n"
        f"Собран полный комплект тайлов ({scenes_count} сцен), облачность MODIS: {cloud_percent}%.\n"
        f"Задание поставлено в очередь на обработку (process.py)."
    )
    _send_email(f"[В очередь] Заказ {zakaz} / {satellite} / {date_str}", body)


def notify_processing_skipped_cloud(zakaz, date_str, satellite, cloud_percent) -> None:
    body = (
        f"Заказ {zakaz}, спутник {satellite}, дата {date_str}.\n"
        f"Облачность MODIS в AOI составила {cloud_percent}% (порог: {config.CLOUD_THRESHOLD_PERCENT}%).\n"
        f"Обработка НЕ запущена -- слишком облачно."
    )
    _send_email(f"[Пропущено: облачно] Заказ {zakaz} / {satellite} / {date_str}", body)


def notify_processing_done(zakaz, date_str, satellite, result: dict) -> None:
    lines = [f"Заказ {zakaz}, спутник {satellite}, дата {date_str}. Обработка завершена.", ""]
    for key, value in result.items():
        lines.append(f"  {key}: {value}")
    _send_email(f"[Готово] Заказ {zakaz} / {satellite} / {date_str}", "\n".join(lines))


def notify_processing_failed(zakaz, date_str, satellite, error: str) -> None:
    body = f"Заказ {zakaz}, спутник {satellite}, дата {date_str}.\nОбработка завершилась с ошибкой:\n\n{error}"
    _send_email(f"[Ошибка обработки] Заказ {zakaz} / {satellite} / {date_str}", body)

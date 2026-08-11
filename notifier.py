"""
Уведомления по email (SMTP с app password).
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
            if new_block:
                new_block.append("")
            new_block.append(f"Заказ {zakaz}:")
            new_block += [_format_line(p, "S2") for p in s2_new]
            new_block += [_format_line(p, "Landsat") for p in l_new]

        if s2_old or l_old:
            if old_block:
                old_block.append("")
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


_STATUS_RU = {"queued": "в очереди на загрузку", "done": "готово", "skipped_cloud": "отбраковано по облачности"}


def notify_processing_summary(newly_queued: list, newly_skipped: list, all_decisions: dict) -> None:
    """Одно сводное письмо за прогон детекции про решения по обработке:
    что отправлено на загрузку и что отбраковано по облачности ИМЕННО
    на этом прогоне, плюс (для контекста, как "старые" сцены в письме о
    новых снимках) -- текущее состояние вообще всех заказов, у которых
    есть хоть какое-то решение."""
    if not newly_queued and not newly_skipped:
        return

    lines = []

    if newly_queued:
        lines.append("ОТПРАВЛЕНО НА ЗАГРУЗКУ:")
        lines.append("")
        for zakaz, satellite, scenes, avg_cloud in newly_queued:
            cloud_str = f"{avg_cloud}%" if avg_cloud is not None else "неизвестна"
            lines.append(f"  • Заказ {zakaz} / {satellite}: {scenes} сцен, средняя облачность {cloud_str}")
        lines.append("")

    if newly_skipped:
        lines.append("ОТБРАКОВАНО ПО ОБЛАЧНОСТИ:")
        lines.append("")
        for zakaz, satellite, avg_cloud in newly_skipped:
            lines.append(f"  • Заказ {zakaz} / {satellite}: средняя облачность {avg_cloud}% (порог {config.CLOUD_THRESHOLD_PERCENT}%)")
        lines.append("")

    context_lines = []
    for zakaz in _sorted_zakazy(all_decisions.keys()):
        for satellite, info in all_decisions[zakaz].items():
            status_ru = _STATUS_RU.get(info["status"], info["status"])
            context_lines.append(f"  • Заказ {zakaz} / {satellite}: {status_ru}")

    if context_lines:
        lines.append("-" * 40)
        lines.append("")
        lines.append("ТЕКУЩЕЕ СОСТОЯНИЕ ВСЕХ ЗАКАЗОВ (для справки):")
        lines.append("")
        lines += context_lines

    _send_email("Обработка снимков -- изменения в очереди", "\n".join(lines))


def notify_processing_done(zakaz, date_str, satellite, result: dict = None) -> None:
    body = (
        f"Заказ {zakaz}, спутник {satellite}, дата {date_str}.\n"
        f"Обработка завершена -- мозаика, водная маска и 8-бит готовы на Google Drive."
    )
    _send_email(f"[Готово] Заказ {zakaz} / {satellite} / {date_str}", body)


def notify_processing_failed(zakaz, date_str, satellite, error: str) -> None:
    body = f"Заказ {zakaz}, спутник {satellite}, дата {date_str}.\nОбработка завершилась с ошибкой:\n\n{error}"
    _send_email(f"[Ошибка обработки] Заказ {zakaz} / {satellite} / {date_str}", body)

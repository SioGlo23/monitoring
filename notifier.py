"""
Уведомления по email (SMTP с app password).
"""
import logging
import smtplib
from email.mime.text import MIMEText

import config

# Telegram -- опциональная часть. Если модуль ещё не загружен в
# репозиторий или токен не задан, всё продолжает работать на одной
# почте: заглушка молча проглатывает вызовы, пайплайн не ломается.
try:
    import telegram_notify as tg
except Exception as _tg_import_error:  # noqa: BLE001
    logging.getLogger("s2monitor.notifier").info(
        "Telegram-уведомления отключены (%s) -- работает только почта", _tg_import_error
    )

    class _TelegramDisabled:
        @staticmethod
        def broadcast_by_order(*args, **kwargs):
            return None

    tg = _TelegramDisabled()

logger = logging.getLogger("s2monitor.notifier")


def _email_orders(zakazy):
    """Оставляет только те заказы, по которым разрешено слать почту
    (config.EMAIL_NOTIFY_ORDERS). Пустой список в конфиге = слать по всем."""
    allowed = {str(z) for z in config.EMAIL_NOTIFY_ORDERS}
    if not allowed:
        return list(zakazy)
    return [z for z in zakazy if str(z) in allowed]


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


def _broadcast_new_scenes_tg(all_zakazy, current_s2: dict, current_landsat: dict, map_url) -> None:
    """Рассылка про новые сцены в Telegram -- по ВСЕМ заказам: кому что
    показывать, решает выбор самого подписчика в меню бота, а не
    config.EMAIL_NOTIFY_ORDERS (тот управляет только почтой)."""
    tg_lines = {}
    for zakaz in all_zakazy:
        lines = [_format_line(p, "S2") for p in current_s2.get(zakaz, []) if p.get("is_new")]
        lines += [_format_line(p, "Landsat") for p in current_landsat.get(zakaz, []) if p.get("is_new")]
        if lines:
            tg_lines[str(zakaz)] = lines
    tg.broadcast_by_order("НОВЫЕ СНИМКИ", tg_lines,
                          footer=f"Карта: {map_url}" if map_url else None)


def notify_new_scenes(current_s2: dict, current_landsat: dict, map_url) -> None:
    all_zakazy = _sorted_zakazy(set(current_s2.keys()) | set(current_landsat.keys()))
    email_zakazy = _email_orders(all_zakazy)

    new_block, old_block = [], []
    zakazy_with_new = 0

    for zakaz in email_zakazy:
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
        _broadcast_new_scenes_tg(all_zakazy, current_s2, current_landsat, map_url)
        return

    body_parts = ["НОВЫЕ СНИМКИ:", ""] + new_block
    if old_block:
        body_parts += ["", "-" * 40, "", "УЖЕ БЫЛИ ИЗВЕСТНЫ РАНЕЕ (для справки):", ""] + old_block
    if map_url:
        body_parts += ["", f"Карта (полные данные по каждой сцене): {map_url}"]

    _send_email(f"Новые сцены S2/Landsat -- {zakazy_with_new} заказ(ов)", "\n".join(body_parts))

    # В Telegram уходит только про НОВЫЕ сцены и только по тем заказам,
    # на которые подписан конкретный получатель.
    _broadcast_new_scenes_tg(all_zakazy, current_s2, current_landsat, map_url)


_STATUS_RU = {
    "queued": "в очереди на загрузку",
    "done": "готово",
    "skipped_cloud": "отбраковано по облачности",
    "failed": "ошибка обработки",
}


def _status_lines(all_decisions: dict) -> list:
    """all_decisions: {zakaz: {satellite: status_string}} -> строки вида
    '  • Заказ N / SPUTNIK: статус' для раздела "текущее состояние"."""
    lines = []
    for zakaz in _sorted_zakazy(all_decisions.keys()):
        for satellite, status in all_decisions[zakaz].items():
            status_ru = _STATUS_RU.get(status, status)
            lines.append(f"  • Заказ {zakaz} / {satellite}: {status_ru}")
    return lines


def notify_processing_summary(newly_queued: list, newly_skipped: list, all_decisions: dict) -> None:
    """Одно сводное письмо за прогон детекции про решения по обработке:
    что отправлено на загрузку и что отбраковано по облачности ИМЕННО
    на этом прогоне, плюс (для контекста, как "старые" сцены в письме о
    новых снимках) -- текущее состояние вообще всех заказов, у которых
    есть хоть какое-то решение. all_decisions: {zakaz: {satellite: status}}."""
    if not newly_queued and not newly_skipped:
        return

    lines = []

    email_allowed = set(_email_orders({str(q[0]) for q in newly_queued} | {str(s[0]) for s in newly_skipped}))

    if newly_queued and email_allowed:
        lines.append("ОТПРАВЛЕНО НА ЗАГРУЗКУ:")
        lines.append("")
        for zakaz, satellite, scenes, avg_cloud in (q for q in newly_queued if str(q[0]) in email_allowed):
            cloud_str = f"{avg_cloud}%" if avg_cloud is not None else "неизвестна"
            lines.append(f"  • Заказ {zakaz} / {satellite}: {scenes} сцен, средняя облачность {cloud_str}")
        lines.append("")

    if newly_skipped and email_allowed:
        lines.append("ОТБРАКОВАНО ПО ОБЛАЧНОСТИ:")
        lines.append("")
        for zakaz, satellite, avg_cloud in (k for k in newly_skipped if str(k[0]) in email_allowed):
            lines.append(f"  • Заказ {zakaz} / {satellite}: средняя облачность {avg_cloud}% (порог {config.CLOUD_THRESHOLD_PERCENT}%)")
        lines.append("")

    context_lines = _status_lines(all_decisions)
    if context_lines:
        lines.append("-" * 40)
        lines.append("")
        lines.append("ТЕКУЩЕЕ СОСТОЯНИЕ ВСЕХ ЗАКАЗОВ (для справки):")
        lines.append("")
        lines += context_lines

    if email_allowed:
        _send_email("Обработка снимков -- изменения в очереди", "\n".join(lines))

    tg_lines = {}
    for zakaz, satellite, scenes, avg_cloud in newly_queued:
        cloud_str = f"{avg_cloud}%" if avg_cloud is not None else "неизвестна"
        tg_lines.setdefault(str(zakaz), []).append(
            f"  • {satellite}: отправлено на загрузку ({scenes} сцен, облачность {cloud_str})"
        )
    for zakaz, satellite, avg_cloud in newly_skipped:
        tg_lines.setdefault(str(zakaz), []).append(
            f"  • {satellite}: отбраковано по облачности ({avg_cloud}%, порог {config.CLOUD_THRESHOLD_PERCENT}%)"
        )
    tg.broadcast_by_order("ОБРАБОТКА СНИМКОВ", tg_lines)


def notify_processing_done(zakaz, date_str, satellite, result: dict = None, all_decisions: dict = None) -> None:
    """Та же форма письма, что у notify_processing_summary -- раздел
    "готово" + раздел "текущее состояние всех заказов"."""
    lines = [
        "ГОТОВО:", "",
        f"  • Заказ {zakaz} / {satellite}: мозаика, водная маска и 8-бит на Google Drive", "",
    ]

    if all_decisions:
        context_lines = _status_lines(all_decisions)
        if context_lines:
            lines.append("-" * 40)
            lines.append("")
            lines.append("ТЕКУЩЕЕ СОСТОЯНИЕ ВСЕХ ЗАКАЗОВ (для справки):")
            lines.append("")
            lines += context_lines

    if _email_orders([str(zakaz)]):
        _send_email(f"[Готово] Заказ {zakaz} / {satellite} / {date_str}", "\n".join(lines))

    tg.broadcast_by_order(
        "ЗАКАЗ ГОТОВ",
        {str(zakaz): [f"  • {satellite}, дата {date_str}: мозаика, водная маска и 8-бит готовы на Google Drive"]},
    )


def notify_processing_failed(zakaz, date_str, satellite, error: str, all_decisions: dict = None) -> None:
    lines = [
        "ОШИБКА ОБРАБОТКИ:", "",
        f"  • Заказ {zakaz} / {satellite}", "", error, "",
    ]

    if all_decisions:
        context_lines = _status_lines(all_decisions)
        if context_lines:
            lines.append("-" * 40)
            lines.append("")
            lines.append("ТЕКУЩЕЕ СОСТОЯНИЕ ВСЕХ ЗАКАЗОВ (для справки):")
            lines.append("")
            lines += context_lines

    if _email_orders([str(zakaz)]):
        _send_email(f"[Ошибка обработки] Заказ {zakaz} / {satellite} / {date_str}", "\n".join(lines))

    tg.broadcast_by_order(
        "ОШИБКА ОБРАБОТКИ",
        {str(zakaz): [f"  • {satellite}, дата {date_str}", f"    {error}"]},
    )

"""
Определяет, "созрела" ли зона интереса для запуска тяжёлого пайплайна
обработки (скачивание -> композит -> мозаика -> вода -> 8-бит), и
ставит задание в очередь на Google Drive (queue/pending/), откуда его
заберёт process.py.

Критерии готовности:
  S2      -- найдены ВСЕ тайлы из mrgs_tiles AOI. Если mrgs_tiles не
             задан -- готовность по стабилизации числа сцен (как для
             Landsat без pr_tile, см. ниже).
  Landsat -- если задан pr_tile -- найдены ВСЕ тайлы из pr_tile;
             если pr_tile не задан/пуст -- ЛЮБЫЕ найденные на AOI
             сцены Landsat, как только их число не меняется
             config.LANDSAT_STABILITY_CYCLES прогонов подряд (по
             умолчанию 2, то есть ~20 минут при цикле мониторинга
             10 минут).

Гейт по облачности MODIS -- общий на оба спутника для одной AOI+даты:
  Обработка ставится в очередь ТОЛЬКО если облачность по MODIS уже
  известна (снимок MODIS скачан и не пуст) И меньше
  config.CLOUD_THRESHOLD_PERCENT. Если облачность ещё неизвестна --
  ждём следующих прогонов. Если облачность >= порога -- решение
  фиксируется как "skipped_cloud" НАВСЕГДА для этой AOI+даты+спутника
  (MODIS-снимок за прошедшую дату не меняется, повторно не проверяем).
"""
import logging

import config
import notifier
import state_store
import utils

logger = logging.getLogger("s2monitor.readiness")


def _s2_ready(feat, s2_prods, zakaz, date_str) -> bool:
    expected = utils.parse_tile_list(feat.get("properties", {}).get("mrgs_tiles"))
    if expected:
        found = {utils.extract_s2_tile(p.get("Name", "")) for p in s2_prods}
        found.discard(None)
        return expected.issubset(found)

    stable_cycles, count = state_store.update_stability_counter("s2", zakaz, date_str, len(s2_prods))
    return count > 0 and stable_cycles >= config.LANDSAT_STABILITY_CYCLES


def _landsat_ready(feat, landsat_prods, zakaz, date_str) -> bool:
    expected = utils.parse_tile_list(feat.get("properties", {}).get("pr_tile"))
    if expected:
        found = {p.get("PR") for p in landsat_prods}
        found.discard(None)
        return expected.issubset(found)

    stable_cycles, count = state_store.update_stability_counter("landsat", zakaz, date_str, len(landsat_prods))
    return count > 0 and stable_cycles >= config.LANDSAT_STABILITY_CYCLES


def _enqueue(zakaz, date_str, satellite, products, cloud_percent) -> None:
    job = {
        "zakaz": zakaz,
        "date": date_str,
        "satellite": satellite,
        "products": [{"Name": p.get("Name"), "Id": p.get("Id")} for p in products],
        "cloud_percent": cloud_percent,
        "created_msk": utils.now_local().strftime("%Y-%m-%d %H:%M:%S"),
    }
    blob_path = f"{config.QUEUE_PENDING_PREFIX}/{zakaz}_{date_str}_{satellite}.json"
    state_store.write_queue_job(blob_path, job)
    state_store.set_processing_decision(zakaz, date_str, satellite, "queued")
    logger.info("Заказ %s (%s): поставлен в очередь на обработку (%s сцен)", zakaz, satellite, len(products))
    notifier.notify_processing_queued(zakaz, date_str, satellite, len(products), cloud_percent)


def evaluate_and_enqueue(zakaz, feat, date_str, s2_prods, landsat_prods, cloud_percent) -> dict:
    """Возвращает {satellite: decision} для тех спутников, где решение
    было принято/уже есть на этом прогоне -- для лога прогона."""
    decisions = {}

    candidates = (
        ("S2", _s2_ready(feat, s2_prods, zakaz, date_str), s2_prods),
        ("L89", _landsat_ready(feat, landsat_prods, zakaz, date_str), landsat_prods),
    )

    for satellite, ready, prods in candidates:
        if not ready or not prods:
            continue

        existing = state_store.get_processing_decision(zakaz, date_str, satellite)
        if existing in ("queued", "done", "skipped_cloud"):
            decisions[satellite] = existing
            continue

        if cloud_percent is None:
            decisions[satellite] = "waiting_modis"
            continue

        if cloud_percent >= config.CLOUD_THRESHOLD_PERCENT:
            state_store.set_processing_decision(zakaz, date_str, satellite, "skipped_cloud")
            decisions[satellite] = "skipped_cloud"
            logger.info(
                "Заказ %s (%s): пропущена обработка -- облачность MODIS %.1f%% >= порога %.1f%%",
                zakaz, satellite, cloud_percent, config.CLOUD_THRESHOLD_PERCENT,
            )
            notifier.notify_processing_skipped_cloud(zakaz, date_str, satellite, cloud_percent)
            continue

        _enqueue(zakaz, date_str, satellite, prods, cloud_percent)
        decisions[satellite] = "queued"

    return decisions

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
             config.LANDSAT_STABILITY_CYCLES прогонов подряд.

Гейт по облачности -- считается по метаданным самих сцен (поле
cloud_cover, которое providers/copernicus.py и providers/usgs_m2m.py
уже кладут в каждый продукт), а не по MODIS. Если по зоне готово
несколько сцен одного спутника (например, 2 тайла S2 по mrgs_tiles),
берётся СРЕДНЕЕ АРИФМЕТИЧЕСКОЕ их облачности. Обработка ставится в
очередь, если это среднее < config.CLOUD_THRESHOLD_PERCENT. Гейт
независим для каждого спутника (у S2 и Landsat своя облачность из
своих же метаданных).

Если ни у одной сцены нет данных об облачности в метаданных (у API
такое бывает редко, но не исключено) -- решение НЕ блокируется:
логируется предупреждение, и обработка запускается как обычно, чтобы
не зависнуть в ожидании данных, которых никогда не появится.
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


def _average_cloud(prods: list):
    values = [p["cloud_cover"] for p in prods if isinstance(p.get("cloud_cover"), (int, float))]
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _enqueue(zakaz, date_str, satellite, products, avg_cloud) -> None:
    job = {
        "zakaz": zakaz,
        "date": date_str,
        "satellite": satellite,
        "products": [{"Name": p.get("Name"), "Id": p.get("Id")} for p in products],
        "avg_cloud_percent": avg_cloud,
        "created_msk": utils.now_local().strftime("%Y-%m-%d %H:%M:%S"),
    }
    blob_path = f"{config.QUEUE_PENDING_PREFIX}/{zakaz}_{date_str}_{satellite}.json"
    state_store.write_queue_job(blob_path, job)
    state_store.set_processing_decision(zakaz, date_str, satellite, "queued")
    logger.info(
        "Заказ %s (%s): поставлен в очередь на обработку (%s сцен, средняя облачность %s%%)",
        zakaz, satellite, len(products), avg_cloud,
    )
    notifier.notify_processing_queued(zakaz, date_str, satellite, len(products), avg_cloud)


def evaluate_and_enqueue(zakaz, feat, date_str, s2_prods, landsat_prods) -> dict:
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

        avg_cloud = _average_cloud(prods)
        if avg_cloud is None:
            logger.warning(
                "Заказ %s (%s): в метаданных сцен нет данных об облачности -- обрабатываем без гейта",
                zakaz, satellite,
            )
        elif avg_cloud >= config.CLOUD_THRESHOLD_PERCENT:
            state_store.set_processing_decision(zakaz, date_str, satellite, "skipped_cloud")
            decisions[satellite] = "skipped_cloud"
            logger.info(
                "Заказ %s (%s): пропущена обработка -- средняя облачность %.1f%% >= порога %.1f%%",
                zakaz, satellite, avg_cloud, config.CLOUD_THRESHOLD_PERCENT,
            )
            notifier.notify_processing_skipped_cloud(zakaz, date_str, satellite, avg_cloud)
            continue

        _enqueue(zakaz, date_str, satellite, prods, avg_cloud)
        decisions[satellite] = "queued"

    return decisions

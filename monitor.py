"""
Один прогон детекции: то, что раньше было одной итерацией while-цикла
в ноутбуке. GitHub Actions вызывает run_once() каждые 10 минут.

Главное отличие от исходного ноутбука: AOI обрабатываются ПАРАЛЛЕЛЬНО
(ThreadPoolExecutor). Кроме поиска сцен S2/Landsat и скачивания MODIS,
на каждом прогоне для каждой AOI также:
  1. считается облачность MODIS внутри точного полигона AOI;
  2. проверяется готовность (см. readiness.py) -- собран ли полный
     ожидаемый комплект тайлов;
  3. если готово и облачность в норме -- задание на тяжёлую обработку
     (скачивание/композит/мозаика/вода/8-бит) ставится в очередь на
     Google Drive, откуда его заберёт отдельный процесс process.py.

monitor.py НИКОГДА сам не скачивает каналы и не строит мозаики -- это
сознательное разделение, чтобы 10-минутный цикл детекции не блокировался
долгой обработкой (см. process.py и .github/workflows/process.yml).
"""
import json
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

from shapely.geometry import shape

import aoi_source
import config
import mapping
import notifier
import readiness
import state_store
import storage
import utils
from providers import copernicus, modis, usgs_m2m

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("s2monitor.monitor")


def _process_one_aoi(zakaz, feat, access_token, m2m_session, grid_gdf,
                      today_start, tomorrow, monitor_date,
                      previous_s2_for_zakaz, previous_landsat_for_zakaz, discovered_readable):
    """Вся работа по одной AOI. Запускается в отдельном потоке на каждую
    AOI одновременно."""
    aoi_shape = shape(feat["geometry"])

    try:
        s2_prods = copernicus.query_for_aoi(zakaz, feat, access_token, today_start, tomorrow)
    except Exception as exc:  # noqa: BLE001
        # Не считаем сбой запроса "снимков нет" -- иначе временный
        # сетевой сбой затёр бы память об уже известных сценах.
        logger.warning(
            "Заказ %s: сбой запроса S2 (%s) -- используем данные с прошлого прогона без изменений",
            zakaz, exc,
        )
        s2_prods = [dict(p) for p in previous_s2_for_zakaz]

    old_s2_ids = {p.get("Id") for p in previous_s2_for_zakaz}
    for p in s2_prods:
        old_match = next((o for o in previous_s2_for_zakaz if o.get("Id") == p.get("Id")), None)
        p["discovered_msk"] = old_match["discovered_msk"] if old_match else discovered_readable
        p["is_new"] = old_match is None

    landsat_prods = []
    if m2m_session:
        try:
            landsat_prods = usgs_m2m.query_for_aoi(zakaz, feat, m2m_session, monitor_date, grid_gdf)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Заказ %s: сбой запроса Landsat (%s) -- используем данные с прошлого прогона без изменений",
                zakaz, exc,
            )
            landsat_prods = [dict(p) for p in previous_landsat_for_zakaz]

        old_l_ids = {p.get("Id") for p in previous_landsat_for_zakaz}
        for p in landsat_prods:
            old_match = next((o for o in previous_landsat_for_zakaz if o.get("Id") == p.get("Id")), None)
            p["discovered_msk"] = old_match["discovered_msk"] if old_match else discovered_readable
            p["is_new"] = old_match is None

    # MODIS + облачность проверяются на КАЖДОМ прогоне.
    modis_path, cloud_percent = modis.download_for_aoi(zakaz, aoi_shape, monitor_date)
    if cloud_percent is not None:
        state_store.save_cloud_percent(zakaz, monitor_date, cloud_percent)
    else:
        cloud_percent = state_store.get_cloud_percent(zakaz, monitor_date)

    new_s2_ids = {p.get("Id") for p in s2_prods}
    new_l_ids = {p.get("Id") for p in landsat_prods}
    changes = {}
    if new_s2_ids - old_s2_ids:
        changes["s2_new"] = list(new_s2_ids - old_s2_ids)
    if landsat_prods and (new_l_ids - {p.get("Id") for p in previous_landsat_for_zakaz}):
        changes["landsat_new"] = list(new_l_ids - {p.get("Id") for p in previous_landsat_for_zakaz})

    processing_decisions = readiness.evaluate_and_enqueue(
        zakaz, feat, monitor_date, s2_prods, landsat_prods, cloud_percent
    )

    return zakaz, s2_prods, landsat_prods, modis_path, cloud_percent, changes, processing_decisions


def run_once() -> dict:
    """Точка входа: один полный прогон детекции по всем AOI."""
    cycle_start = utils.now_local()
    cycle_ts = cycle_start.strftime("%Y%m%dT%H%M%S")
    cycle_readable = cycle_start.strftime("%Y-%m-%d %H:%M:%S")
    cycle_date = cycle_start.strftime("%Y-%m-%d")
    monitor_date = config.MONITOR_DATE
    logger.info("=== Запуск прогона детекции %s (дата мониторинга: %s) ===", cycle_ts, monitor_date)

    aoi_dict = aoi_source.load_aoi_dict()
    grid_gdf = aoi_source.load_grid_gdf()
    logger.info("Загружено %s зон интереса", len(aoi_dict))

    access_token = copernicus.get_access_token()
    m2m_session = usgs_m2m.get_session()

    prev_state = state_store.load_previous_state()
    prev_s2 = prev_state.get("s2", {})
    prev_landsat = prev_state.get("landsat", {})

    date_obj = date.fromisoformat(monitor_date)
    today_start = f"{monitor_date}T00:00:00Z"
    tomorrow = (date_obj + timedelta(days=1)).isoformat() + "T00:00:00Z"

    current_s2, current_landsat, changed_info, modis_status, cloud_by_zakaz, decisions_by_zakaz = (
        {}, {}, {}, {}, {}, {}
    )

    with ThreadPoolExecutor(max_workers=config.MAX_PARALLEL_AOI) as pool:
        futures = {
            pool.submit(
                _process_one_aoi, zakaz, feat, access_token, m2m_session, grid_gdf,
                today_start, tomorrow, monitor_date,
                prev_s2.get(str(zakaz), []), prev_landsat.get(str(zakaz), []), cycle_readable,
            ): zakaz
            for zakaz, feat in aoi_dict.items()
        }
        for future in as_completed(futures):
            zakaz = futures[future]
            try:
                zakaz, s2_prods, landsat_prods, modis_path, cloud_percent, changes, decisions = future.result()
            except Exception as exc:  # noqa: BLE001
                logger.error("Заказ %s: прогон завершился с ошибкой: %s", zakaz, exc)
                continue
            current_s2[str(zakaz)] = s2_prods
            current_landsat[str(zakaz)] = landsat_prods
            modis_status[str(zakaz)] = modis_path
            cloud_by_zakaz[str(zakaz)] = cloud_percent
            if decisions:
                decisions_by_zakaz[str(zakaz)] = decisions
            if changes:
                changed_info[str(zakaz)] = changes

    if m2m_session:
        usgs_m2m.logout(m2m_session)

    modis_downloaded_count = sum(1 for v in modis_status.values() if v)

    log_data = {
        "timestamp_local": cycle_readable,
        "monitored_date": monitor_date,
        "total_s2_scenes": sum(len(v) for v in current_s2.values()),
        "total_landsat_scenes": sum(len(v) for v in current_landsat.values()),
        "modis_available_count": modis_downloaded_count,
        "modis_total_aoi": len(aoi_dict),
        "changed_info": changed_info,
        "cloud_percent_by_zakaz": cloud_by_zakaz,
        "processing_decisions": decisions_by_zakaz,
        "zakaz_data": {
            zakaz: {
                "s2": [
                    {
                        "id": p.get("Id"), "name": p.get("Name"),
                        "start_time_msk": p.get("start_time_msk"), "published_msk": p.get("published_msk"),
                        "discovered_msk": p.get("discovered_msk"), "is_new": p.get("is_new", False),
                    }
                    for p in current_s2.get(zakaz, [])
                ],
                "landsat": [
                    {
                        "id": p.get("Id"), "name": p.get("Name"),
                        "start_time_msk": p.get("start_time_msk"),
                        "discovered_msk": p.get("discovered_msk"), "is_new": p.get("is_new", False),
                    }
                    for p in current_landsat.get(zakaz, [])
                ],
                "modis_available": bool(modis_status.get(zakaz)),
                "cloud_percent": cloud_by_zakaz.get(zakaz),
            }
            for zakaz in aoi_dict
        },
    }

    is_heartbeat = cycle_start.minute < 10
    cycle_number = None
    map_gs_path = None

    if changed_info or is_heartbeat:
        cycle_number = state_store.next_cycle_number(cycle_date)
        storage.upload_json(f"{config.LOGS_PREFIX}/{cycle_ts}_{cycle_number:03d}_log.json", log_data)

        m = mapping.build_map(current_s2, current_landsat, aoi_dict)
        map_gs_path = mapping.save_map(m, cycle_ts, cycle_number)
    else:
        logger.info("Изменений нет, не heartbeat-минута -- полный лог и карта в этот раз не пишутся")

    if changed_info:
        logger.info("Обнаружены новые сцены: %s", list(changed_info.keys()))
        notifier.notify_new_scenes(current_s2, current_landsat, map_gs_path)

    state_store.save_state(current_s2, current_landsat)

    if changed_info:
        status_note = f"новые сцены ({len(changed_info)} заказ(ов)), лог #{cycle_number:03d}"
    elif is_heartbeat:
        status_note = f"новых нет, heartbeat, лог #{cycle_number:03d}"
    else:
        status_note = "новых нет, лог не писался"
    history_line = (
        f"{cycle_readable} | OK | AOI: {len(aoi_dict)} | "
        f"S2: {log_data['total_s2_scenes']} | Landsat: {log_data['total_landsat_scenes']} | "
        f"MODIS: {modis_downloaded_count}/{len(aoi_dict)} | {status_note}"
    )
    state_store.append_run_history(history_line)

    result = {
        "cycle_number": cycle_number,
        "aoi_processed": len(aoi_dict),
        "total_s2_scenes": log_data["total_s2_scenes"],
        "total_landsat_scenes": log_data["total_landsat_scenes"],
        "modis_available_count": modis_downloaded_count,
        "new_scenes_found": bool(changed_info),
        "processing_decisions": decisions_by_zakaz,
        "log_written": bool(changed_info or is_heartbeat),
    }
    logger.info("=== Прогон детекции завершён: %s ===", result)
    return result


if __name__ == "__main__":
    try:
        run_once()
    except Exception as exc:  # noqa: BLE001
        try:
            failure_line = f"{utils.now_local().strftime('%Y-%m-%d %H:%M:%S')} | FAILED | {exc}"
            state_store.append_run_history(failure_line)
        except Exception:  # noqa: BLE001
            pass
        traceback.print_exc()
        raise

"""
Один прогон мониторинга: то, что раньше было одной итерацией
while-цикла в ноутбуке. GitHub Actions вызывает run_once() каждые
10 минут — сам процесс не "живёт" между вызовами, поэтому все данные,
которые раньше лежали в переменных Python (previous_s2 и т.п.),
теперь читаются/пишутся через state_store (Google Drive).

Главное отличие от ноутбука: AOI обрабатываются ПАРАЛЛЕЛЬНО
(ThreadPoolExecutor) — запросы к API и скачивание MODIS это
I/O-bound работа, поэтому это может в разы ускорить один прогон.

MODIS проверяется/скачивается КАЖДЫЙ прогон (каждые 10 минут) для
каждой AOI, независимо от того, пишется ли в этот раз общий лог —
см. вызов modis.download_for_aoi() ниже. Если снимок MODIS за сутки
ещё не полностью покрывает область (see providers/modis.py::_is_empty),
он не сохраняется и попытка повторяется на следующих прогонах — так
что "появится не сразу" для MODIS ожидаемо, это не сбой.
"""
import json
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import geopandas as gpd
from shapely.geometry import shape

import config
import mapping
import notifier
import state_store
import storage
import utils
from providers import copernicus, modis, usgs_m2m

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("s2monitor.monitor")


def _load_aoi_dict() -> dict:
    raw = json.loads(storage.download_text(config.AOI_GEOJSON_BLOB))
    aoi_dict = {}
    for feat in raw.get("features", []):
        zakaz = feat.get("properties", {}).get("zakaz")
        if zakaz is not None:
            aoi_dict[zakaz] = feat
    return aoi_dict


def _load_grid_gdf() -> "gpd.GeoDataFrame":
    import tempfile

    text = storage.download_text(config.GRID_GEOJSON_BLOB)
    with tempfile.NamedTemporaryFile(suffix=".geojson", mode="w", delete=False, encoding="utf-8") as f:
        f.write(text)
        tmp_path = f.name
    grid_gdf = gpd.read_file(tmp_path)
    if grid_gdf.crs is None:
        grid_gdf.set_crs("EPSG:4326", inplace=True)
    else:
        grid_gdf = grid_gdf.to_crs("EPSG:4326")
    grid_gdf["PR"] = grid_gdf["PR"].astype(str).str.strip()
    return grid_gdf


def _process_one_aoi(zakaz, feat, access_token, m2m_session, grid_gdf,
                      today_start, tomorrow, target_date_str, monitor_date,
                      previous_s2_for_zakaz, previous_landsat_for_zakaz, discovered_readable):
    """Вся работа по одной AOI — то, что цикл в ноутбуке делал строго
    последовательно. Эта функция запускается в отдельном потоке на
    каждую AOI одновременно."""
    aoi_shape = shape(feat["geometry"])

    s2_prods = copernicus.query_for_aoi(zakaz, feat, access_token, today_start, tomorrow)
    old_s2_ids = {p.get("Id") for p in previous_s2_for_zakaz}
    for p in s2_prods:
        old_match = next((o for o in previous_s2_for_zakaz if o.get("Id") == p.get("Id")), None)
        p["discovered_msk"] = old_match["discovered_msk"] if old_match else discovered_readable
        p["is_new"] = old_match is None

    landsat_prods = []
    if m2m_session:
        landsat_prods = usgs_m2m.query_for_aoi(zakaz, feat, m2m_session, target_date_str, grid_gdf)
        old_l_ids = {p.get("Id") for p in previous_landsat_for_zakaz}
        for p in landsat_prods:
            old_match = next((o for o in previous_landsat_for_zakaz if o.get("Id") == p.get("Id")), None)
            p["discovered_msk"] = old_match["discovered_msk"] if old_match else discovered_readable
            p["is_new"] = old_match is None

    # MODIS проверяется/скачивается на КАЖДОМ прогоне, вне зависимости
    # от heartbeat/изменений — см. docstring модуля.
    modis_path = modis.download_for_aoi(zakaz, aoi_shape, monitor_date)

    new_s2_ids = {p.get("Id") for p in s2_prods}
    new_l_ids = {p.get("Id") for p in landsat_prods}
    changes = {}
    if new_s2_ids - old_s2_ids:
        changes["s2_new"] = list(new_s2_ids - old_s2_ids)
    if landsat_prods and (new_l_ids - {p.get("Id") for p in previous_landsat_for_zakaz}):
        changes["landsat_new"] = list(new_l_ids - {p.get("Id") for p in previous_landsat_for_zakaz})

    return zakaz, s2_prods, landsat_prods, modis_path, changes


def run_once() -> dict:
    """Точка входа: один полный прогон мониторинга по всем AOI."""
    cycle_start = utils.now_local()
    cycle_ts = cycle_start.strftime("%Y%m%dT%H%M%S")  # для внутренних нужд (не для имён файлов лога/карты)
    cycle_readable = cycle_start.strftime("%Y-%m-%d %H:%M:%S")
    cycle_date = cycle_start.strftime("%Y-%m-%d")  # календарная дата по TIMEZONE — для сброса нумерации
    monitor_date = config.MONITOR_DATE
    logger.info("=== Запуск прогона %s (дата мониторинга: %s) ===", cycle_ts, monitor_date)

    aoi_dict = _load_aoi_dict()
    grid_gdf = _load_grid_gdf()
    logger.info("Загружено %s зон интереса", len(aoi_dict))

    access_token = copernicus.get_access_token()
    m2m_session = usgs_m2m.get_session()

    prev_state = state_store.load_previous_state()
    prev_s2 = prev_state.get("s2", {})
    prev_landsat = prev_state.get("landsat", {})

    date_obj = date.fromisoformat(monitor_date)
    today_start = f"{monitor_date}T00:00:00Z"
    tomorrow = (date_obj + timedelta(days=1)).isoformat() + "T00:00:00Z"

    current_s2, current_landsat, changed_info, modis_status = {}, {}, {}, {}

    with ThreadPoolExecutor(max_workers=config.MAX_PARALLEL_AOI) as pool:
        futures = {
            pool.submit(
                _process_one_aoi, zakaz, feat, access_token, m2m_session, grid_gdf,
                today_start, tomorrow, monitor_date, monitor_date,
                prev_s2.get(str(zakaz), []), prev_landsat.get(str(zakaz), []), cycle_readable,
            ): zakaz
            for zakaz, feat in aoi_dict.items()
        }
        for future in as_completed(futures):
            zakaz = futures[future]
            try:
                zakaz, s2_prods, landsat_prods, modis_path, changes = future.result()
            except Exception as exc:  # noqa: BLE001
                logger.error("Заказ %s: прогон завершился с ошибкой: %s", zakaz, exc)
                continue
            current_s2[str(zakaz)] = s2_prods
            current_landsat[str(zakaz)] = landsat_prods
            modis_status[str(zakaz)] = modis_path
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
        "zakaz_data": {
            zakaz: {
                "s2": [
                    {
                        "id": p.get("Id"),
                        "start_time_msk": p.get("start_time_msk"),
                        "published_msk": p.get("published_msk"),
                        "discovered_msk": p.get("discovered_msk"),
                        "is_new": p.get("is_new", False),
                    }
                    for p in current_s2.get(zakaz, [])
                ],
                "landsat": [
                    {
                        "id": p.get("Id"),
                        "start_time_msk": p.get("start_time_msk"),
                        "discovered_msk": p.get("discovered_msk"),
                        "is_new": p.get("is_new", False),
                    }
                    for p in current_landsat.get(zakaz, [])
                ],
                "modis_available": bool(modis_status.get(zakaz)),
            }
            for zakaz in aoi_dict
        },
    }

    # Экономия хранилища/операций: полный лог+карту пишем не на каждый
    # из 144 прогонов в сутки, а только когда реально что-то изменилось.
    # Раз в час (в минуту :00-:09) дополнительно пишем "heartbeat" —
    # чтобы можно было убедиться, что сервис вообще жив.
    is_heartbeat = cycle_start.minute < 10
    map_gs_path = None
    cycle_number = None

    if changed_info or is_heartbeat:
        cycle_number = state_store.next_cycle_number(cycle_date)
        storage.upload_json(f"{config.LOGS_PREFIX}/{cycle_ts}_{cycle_number:03d}_log.json", log_data)

        m = mapping.build_map(current_s2, current_landsat, aoi_dict)
        map_gs_path = mapping.save_map(m, cycle_ts, cycle_number)
    else:
        logger.info("Изменений нет, не heartbeat-минута — лог и карта в этот раз не пишутся")

    if changed_info:
        logger.info("Обнаружены новые сцены: %s", list(changed_info.keys()))
        notifier.notify_new_scenes(current_s2, current_landsat, map_gs_path)

    # Сохраняем состояние для следующего прогона
    state_store.save_state(current_s2, current_landsat)

    # Единая история запусков — по одной строке на КАЖДЫЙ прогон,
    # вне зависимости от того, писался ли полный лог.
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
        "log_written": bool(changed_info or is_heartbeat),
        "map_path": map_gs_path,
    }
    logger.info("=== Прогон завершён: %s ===", result)
    return result


if __name__ == "__main__":
    try:
        run_once()
    except Exception as exc:  # noqa: BLE001
        # Даже при падении фиксируем факт попытки в единой истории —
        # чтобы её было видно, не заходя каждый раз в Actions.
        try:
            failure_line = f"{utils.now_local().strftime('%Y-%m-%d %H:%M:%S')} | FAILED | {exc}"
            state_store.append_run_history(failure_line)
        except Exception:  # noqa: BLE001
            pass  # если даже запись истории не удалась — не мешаем исходной ошибке всплыть
        traceback.print_exc()
        raise

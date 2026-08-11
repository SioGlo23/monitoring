"""
Один прогон детекции: то, что раньше было одной итерацией while-цикла
в ноутбуке. GitHub Actions вызывает run_once() каждые 10 минут.

AOI обрабатываются ПАРАЛЛЕЛЬНО (ThreadPoolExecutor). Кроме поиска сцен
S2/Landsat и скачивания MODIS (для карты), на каждом прогоне для
каждой AOI также:
  1. для новых сцен скачивается квиклук (загрубленное превью) и
     заливается на Google Drive -- для старых сцен ссылка просто
     переносится из памяти прошлого прогона, повторно не скачивается;
  2. проверяется готовность (readiness.py) -- собран ли полный
     ожидаемый комплект тайлов, и укладывается ли средняя облачность
     по метаданным сцен в порог;
  3. если готово -- задание на тяжёлую обработку ставится в очередь
     на Google Drive, откуда его заберёт process.py.

ВАЖНО (устойчивость к сбоям): если обработка конкретной AOI на этом
прогоне упала с ошибкой -- в state/previous_state.json сохраняются
данные С ПРОШЛОГО УСПЕШНОГО прогона для этой AOI, а не пустой список.
Раньше ошибка в одной AOI могла тихо стереть всю накопленную память о
сценах для неё -- теперь это исключено на двух уровнях (внутри
_process_one_aoi и ещё раз в run_once на всякий случай).

monitor.py НИКОГДА сам не скачивает каналы и не строит мозаики -- это
сознательное разделение, чтобы 10-минутный цикл детекции не блокировался
долгой обработкой (см. process.py и .github/workflows/process.yml).
"""
import logging
import os
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import requests
from shapely.geometry import shape

import aoi_source
import config
import mapping
import notifier
import readiness
import state_store
import storage
import utils
from pipeline import s2_download
from providers import copernicus, modis, usgs_m2m

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("s2monitor.monitor")

_REQUIRED_UTILS_FUNCS = (
    "now_local", "to_local_readable", "retry", "parse_tile_list",
    "extract_s2_tile", "detect_landsat_number", "utm_crs_for_shape", "compressed_profile",
)
_missing_utils = [name for name in _REQUIRED_UTILS_FUNCS if not hasattr(utils, name)]
if _missing_utils:
    raise RuntimeError(
        f"utils.py в репозитории устарел -- отсутствуют функции: {_missing_utils}. "
        "Скорее всего, при последнем обновлении файл utils.py не был заменён на актуальную "
        "версию. Скачайте свежий utils.py и замените им файл в корне репозитория."
    )


def _fetch_quicklook(satellite: str, p: dict) -> str:
    """Скачивает квиклук новой сцены и заливает на Drive. Возвращает
    ссылку или None -- при любой ошибке просто логирует и не мешает
    остальной детекции (квиклук -- вспомогательная функция)."""
    try:
        if satellite == "S2":
            local_path = os.path.join(config.LOCAL_TMP_DIR, "quicklooks", f"{p['Name']}.png")
            if not s2_download.download_quicklook(p, local_path):
                return None
            blob_path = f"{config.QUICKLOOKS_PREFIX}/{p['Name']}.png"
            link = storage.upload_file(local_path, blob_path, content_type="image/png")
        else:
            url = p.get("quicklook_url")
            if not url:
                return None
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            blob_path = f"{config.QUICKLOOKS_PREFIX}/{p['Name']}.jpg"
            link = storage.upload_bytes(resp.content, blob_path, content_type="image/jpeg")
        return link
    except Exception as exc:  # noqa: BLE001
        logger.warning("Квиклук для %s (%s) не скачан: %s", p.get("Name"), satellite, exc)
        return None
    finally:
        local_path = os.path.join(config.LOCAL_TMP_DIR, "quicklooks", f"{p.get('Name')}.png")
        if os.path.exists(local_path):
            os.remove(local_path)


def _merge_with_previous(satellite: str, prods: list, previous_prods: list, discovered_readable: str) -> None:
    """Проставляет discovered_msk/is_new/quicklook_link, сравнивая с
    предыдущим известным списком, и скачивает квиклуки для НОВЫХ сцен."""
    for p in prods:
        old_match = next((o for o in previous_prods if o.get("Id") == p.get("Id")), None)
        p["discovered_msk"] = old_match["discovered_msk"] if old_match else discovered_readable
        p["is_new"] = old_match is None
        if old_match and old_match.get("quicklook_link"):
            p["quicklook_link"] = old_match["quicklook_link"]
        elif p["is_new"]:
            p["quicklook_link"] = _fetch_quicklook(satellite, p)
        else:
            p["quicklook_link"] = None


def _process_one_aoi(zakaz, feat, access_token, m2m_session, grid_gdf,
                      today_start, tomorrow, monitor_date,
                      previous_s2_for_zakaz, previous_landsat_for_zakaz, discovered_readable):
    """Вся работа по одной AOI. Запускается в отдельном потоке на каждую
    AOI одновременно. При ЛЮБОЙ необработанной ошибке возвращает данные
    с прошлого прогона как есть (см. docstring модуля) -- никогда не
    даёт вызывающему коду затереть память о сценах пустотой."""
    try:
        aoi_shape = shape(feat["geometry"])

        try:
            s2_prods = copernicus.query_for_aoi(zakaz, feat, access_token, today_start, tomorrow)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Заказ %s: сбой запроса S2 (%s) -- используем данные с прошлого прогона без изменений",
                zakaz, exc,
            )
            s2_prods = [dict(p) for p in previous_s2_for_zakaz]

        _merge_with_previous("S2", s2_prods, previous_s2_for_zakaz, discovered_readable)

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
            _merge_with_previous("L89", landsat_prods, previous_landsat_for_zakaz, discovered_readable)

        # MODIS -- только для визуального слоя на карте, не влияет на гейтинг.
        try:
            modis_path = modis.download_for_aoi(zakaz, aoi_shape, monitor_date)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Заказ %s: сбой скачивания MODIS (%s)", zakaz, exc)
            modis_path = None

        old_s2_ids = {p.get("Id") for p in previous_s2_for_zakaz}
        old_l_ids = {p.get("Id") for p in previous_landsat_for_zakaz}
        new_s2_ids = {p.get("Id") for p in s2_prods}
        new_l_ids = {p.get("Id") for p in landsat_prods}
        changes = {}
        if new_s2_ids - old_s2_ids:
            changes["s2_new"] = list(new_s2_ids - old_s2_ids)
        if new_l_ids - old_l_ids:
            changes["landsat_new"] = list(new_l_ids - old_l_ids)

        try:
            processing_decisions = readiness.evaluate_and_enqueue(zakaz, feat, monitor_date, s2_prods, landsat_prods)
        except Exception as exc:  # noqa: BLE001
            logger.error("Заказ %s: сбой при оценке готовности/постановке в очередь: %s", zakaz, exc)
            processing_decisions = {}

        return zakaz, s2_prods, landsat_prods, modis_path, changes, processing_decisions, None

    except Exception as exc:  # noqa: BLE001
        # Полный неожиданный сбой где-то в теле функции -- отдаём
        # предыдущие данные как есть, чтобы вызывающий код НИЧЕГО не потерял.
        logger.error("Заказ %s: НЕОЖИДАННЫЙ сбой обработки AOI: %s", zakaz, exc)
        logger.error(traceback.format_exc())
        return zakaz, list(previous_s2_for_zakaz), list(previous_landsat_for_zakaz), None, {}, {}, str(exc)


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

    current_s2, current_landsat, changed_info, modis_status, decisions_by_zakaz, aoi_errors = (
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
                zakaz, s2_prods, landsat_prods, modis_path, changes, decisions, error = future.result()
            except Exception as exc:  # noqa: BLE001
                # Такого практически не должно случаться (см. try/except
                # внутри _process_one_aoi), но на всякий случай тоже не
                # теряем прошлые данные по этой AOI.
                logger.error("Заказ %s: future.result() неожиданно упал: %s", zakaz, exc)
                s2_prods = list(prev_s2.get(str(zakaz), []))
                landsat_prods = list(prev_landsat.get(str(zakaz), []))
                modis_path, changes, decisions = None, {}, {}
                error = str(exc)

            current_s2[str(zakaz)] = s2_prods
            current_landsat[str(zakaz)] = landsat_prods
            modis_status[str(zakaz)] = modis_path
            if decisions:
                decisions_by_zakaz[str(zakaz)] = decisions
            if changes:
                changed_info[str(zakaz)] = changes
            if error:
                aoi_errors[str(zakaz)] = error

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
        "processing_decisions": decisions_by_zakaz,
        "aoi_errors": aoi_errors,
        "zakaz_data": {
            zakaz: {
                "s2": [
                    {
                        "id": p.get("Id"), "name": p.get("Name"),
                        "cloud_cover": p.get("cloud_cover"),
                        "start_time_msk": p.get("start_time_msk"), "published_msk": p.get("published_msk"),
                        "discovered_msk": p.get("discovered_msk"), "is_new": p.get("is_new", False),
                        "quicklook_link": p.get("quicklook_link"),
                    }
                    for p in current_s2.get(zakaz, [])
                ],
                "landsat": [
                    {
                        "id": p.get("Id"), "name": p.get("Name"),
                        "cloud_cover": p.get("cloud_cover"),
                        "start_time_msk": p.get("start_time_msk"),
                        "discovered_msk": p.get("discovered_msk"), "is_new": p.get("is_new", False),
                        "quicklook_link": p.get("quicklook_link"),
                    }
                    for p in current_landsat.get(zakaz, [])
                ],
                "modis_available": bool(modis_status.get(zakaz)),
            }
            for zakaz in aoi_dict
        },
    }

    is_heartbeat = cycle_start.minute < 10
    cycle_number = None
    map_gs_path = None

    if changed_info or is_heartbeat or aoi_errors:
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

    if aoi_errors:
        status_note = f"ОШИБКИ по AOI: {list(aoi_errors.keys())}, лог #{cycle_number:03d}"
    elif changed_info:
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
        "aoi_errors": aoi_errors,
        "log_written": bool(changed_info or is_heartbeat or aoi_errors),
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

"""
Тяжёлая обработка: скачивание каналов -> композит -> мозаика -> вода
-> 8-бит. Работает НЕЗАВИСИМО от monitor.py (отдельный workflow,
отдельное расписание) и забирает готовые задания из очереди на Google
Drive (queue/pending/), которые туда кладёт readiness.py по итогам
детекции в monitor.py.

Также поддерживает РУЧНОЙ запуск в обход очереди и всех порогов
готовности/облачности -- если заданы переменные окружения
ZAKAZ_OVERRIDE и SATELLITE_OVERRIDE (их выставляет workflow_dispatch
в .github/workflows/process.yml). Это единственный способ форсировать
обработку конкретного заказа немедленно, не дожидаясь автоматики.
"""
import logging
import os
import shutil
import traceback
from datetime import date, timedelta

from shapely.geometry import shape

import aoi_source
import config
import notifier
import state_store
import storage
import utils
from pipeline import eightbit, landsat_download, landsat_pansharpen, mosaic, s2_download, water
from providers import copernicus, usgs_m2m

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("s2monitor.process")

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


def _final_mosaic_name(zakaz: str, date_str: str, satellite: str, products: list) -> str:
    date_compact = date_str.replace("-", "")
    if satellite == "L89":
        num = utils.detect_landsat_number(products[0]["Name"]) if products else "?"
        return f"{zakaz}_{date_compact}_L{num}_{config.CHANNELS}"
    return f"{zakaz}_{date_compact}_S2_{config.CHANNELS}"


def _process_job(job: dict, aoi_dict: dict) -> dict:
    zakaz = job["zakaz"]
    date_str = job["date"]
    satellite = job["satellite"]
    products = job["products"]

    feat = aoi_dict.get(str(zakaz))
    if feat is None:
        raise RuntimeError(f"Заказ {zakaz} отсутствует в текущем AOI_GEOJSON_BLOB -- обработка невозможна")

    aoi_shape = shape(feat["geometry"])
    target_crs = utils.utm_crs_for_shape(aoi_shape)
    selected_bands = config.selected_bands(satellite)

    job_id = f"{zakaz}_{date_str}_{satellite}"
    work_dir = os.path.join(config.LOCAL_TMP_DIR, "process", job_id)
    bands_dir = os.path.join(work_dir, "bands")
    zip_dir = os.path.join(work_dir, "zip")
    composites_dir = os.path.join(work_dir, "composites")
    temp_clip_dir = os.path.join(work_dir, "temp_clipped")
    mosaics_dir = os.path.join(work_dir, "mosaics")
    for d in (bands_dir, zip_dir, composites_dir, temp_clip_dir, mosaics_dir):
        os.makedirs(d, exist_ok=True)

    logger.info("=== Обработка: заказ %s, спутник %s, дата %s, %s сцен ===", zakaz, satellite, date_str, len(products))

    composite_paths = []

    if satellite == "S2":
        access_token = copernicus.get_access_token()  # noqa: F841 - используется внутри s2_download через отдельный токен-кэш
        for p in products:
            sid = p["Name"].replace(".SAFE", "")
            prod_bands_dir = os.path.join(bands_dir, sid)
            composite_path = os.path.join(composites_dir, f"{sid}_{config.CHANNELS}.tif")

            s2_download.download_selected_bands(p, selected_bands, prod_bands_dir)
            s2_download.create_composite_from_bands(prod_bands_dir, selected_bands, composite_path)
            composite_paths.append(composite_path)

    else:  # L89
        m2m_session = usgs_m2m.get_session()
        if not m2m_session:
            raise RuntimeError("Не удалось авторизоваться в USGS M2M -- обработка Landsat невозможна")
        ee_session = landsat_download.ee_login()
        catalog = landsat_download.dataset_download_options(m2m_session)

        try:
            for p in products:
                sid = p["Name"]
                entity_id = p["Id"]
                composite_path = os.path.join(composites_dir, f"{sid}_{config.CHANNELS}.tif")

                band_paths = landsat_download.fetch_scene_bands(
                    sid, entity_id, selected_bands, config.PAN_BAND_L89,
                    ee_session, catalog, zip_dir, os.path.join(bands_dir, sid),
                )
                landsat_pansharpen.pansharpen_hpf(
                    band_paths, tuple(selected_bands), band_paths[config.PAN_BAND_L89],
                    composite_path, block_size=config.PANSHARPEN_BLOCK_SIZE,
                )
                composite_paths.append(composite_path)
        finally:
            usgs_m2m.logout(m2m_session)
            landsat_download.ee_logout(ee_session)

    final_name = _final_mosaic_name(zakaz, date_str, satellite, products)
    mosaic_local_path = os.path.join(mosaics_dir, f"{final_name}.tif")
    mosaic.create_mosaic(composite_paths, aoi_shape, target_crs, mosaic_local_path, temp_clip_dir)
    mosaic.build_pyramids(mosaic_local_path)

    wt = config.WATER_THRESHOLDS[satellite]
    water_local_path = os.path.join(mosaics_dir, "water", f"{final_name}_water.geojson")
    water.extract_water_mask(mosaic_local_path, water_local_path, wt["porog1"], wt["porog2"], wt["ch1"], wt["ch2"])

    eb = config.EIGHTBIT_MINMAX[satellite]
    eightbit_paths = eightbit.convert_to_8bit(mosaic_local_path, os.path.join(mosaics_dir, "8bit"),
                                               min_val=eb["min_val"], max_val=eb["max_val"])

    # --- Заливаем результаты на Google Drive ---
    result_links = {}
    result_links["mosaic"] = storage.upload_file(
        mosaic_local_path, f"{config.MOSAICS_PREFIX}/{final_name}.tif", content_type="image/tiff"
    )
    ovr_path = mosaic_local_path + ".ovr"
    if os.path.exists(ovr_path):
        storage.upload_file(ovr_path, f"{config.MOSAICS_PREFIX}/{final_name}.tif.ovr", content_type="application/octet-stream")

    result_links["water_geojson"] = storage.upload_file(
        water_local_path, f"{config.WATER_PREFIX}/{final_name}_water.geojson", content_type="application/geo+json"
    )
    for eb_path in eightbit_paths:
        eb_name = os.path.basename(eb_path)
        result_links[eb_name] = storage.upload_file(eb_path, f"{config.EIGHTBIT_PREFIX}/{eb_name}", content_type="image/tiff")

    shutil.rmtree(work_dir, ignore_errors=True)
    logger.info("=== Заказ %s (%s) обработан успешно ===", zakaz, satellite)
    return result_links


def _run_queue() -> dict:
    jobs = state_store.list_pending_jobs()
    if not jobs:
        logger.info("Очередь пуста -- нечего обрабатывать")
        return {"jobs_processed": 0, "jobs_failed": 0}

    aoi_dict = aoi_source.load_aoi_dict()
    processed, failed = 0, 0

    for job_blob, job in jobs:
        try:
            result = _process_job(job, aoi_dict)
            state_store.mark_job_done(job_blob, job)
            notifier.notify_processing_done(job["zakaz"], job["date"], job["satellite"], result)
            processed += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("Задание %s завершилось с ошибкой: %s", job_blob, exc)
            traceback.print_exc()
            state_store.mark_job_failed(job_blob, job, str(exc))
            notifier.notify_processing_failed(job["zakaz"], job["date"], job["satellite"], str(exc))
            failed += 1

    return {"jobs_processed": processed, "jobs_failed": failed}


def _run_manual(zakaz: str, satellite: str, date_override: str = None) -> dict:
    """Ручной запуск в обход очереди/готовности/облачности -- ищет сцены
    на указанную (или сегодняшнюю, если не задана) дату и сразу
    обрабатывает то, что найдётся."""
    monitor_date = date_override or config.MONITOR_DATE
    logger.info("=== РУЧНОЙ ЗАПУСК: заказ %s, спутник %s, дата %s ===", zakaz, satellite, monitor_date)
    aoi_dict = aoi_source.load_aoi_dict()
    feat = aoi_dict.get(str(zakaz))
    if feat is None:
        raise RuntimeError(f"Заказ {zakaz} не найден в AOI_GEOJSON_BLOB")

    if satellite == "S2":
        access_token = copernicus.get_access_token()
        date_obj = date.fromisoformat(monitor_date)
        today_start = f"{monitor_date}T00:00:00Z"
        tomorrow = (date_obj + timedelta(days=1)).isoformat() + "T00:00:00Z"
        products = copernicus.query_for_aoi(zakaz, feat, access_token, today_start, tomorrow)
    else:
        grid_gdf = aoi_source.load_grid_gdf()
        m2m_session = usgs_m2m.get_session()
        products = usgs_m2m.query_for_aoi(zakaz, feat, m2m_session, monitor_date, grid_gdf)
        usgs_m2m.logout(m2m_session)

    if not products:
        logger.warning("Ручной запуск: сцены для заказа %s (%s) на дату %s не найдены", zakaz, satellite, monitor_date)
        return {"jobs_processed": 0, "jobs_failed": 0}

    job = {"zakaz": str(zakaz), "date": monitor_date, "satellite": satellite, "products": products}
    try:
        result = _process_job(job, aoi_dict)
        notifier.notify_processing_done(zakaz, monitor_date, satellite, result)
        return {"jobs_processed": 1, "jobs_failed": 0}
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        notifier.notify_processing_failed(zakaz, monitor_date, satellite, str(exc))
        return {"jobs_processed": 0, "jobs_failed": 1}


def run_once() -> dict:
    zakaz_override = (os.environ.get("ZAKAZ_OVERRIDE") or "").strip()
    satellite_override = (os.environ.get("SATELLITE_OVERRIDE") or "").strip()
    date_override = (os.environ.get("DATE_OVERRIDE") or "").strip() or None

    if zakaz_override and satellite_override:
        return _run_manual(zakaz_override, satellite_override, date_override)

    return _run_queue()


if __name__ == "__main__":
    result = run_once()
    logger.info("=== process.py завершён: %s ===", result)

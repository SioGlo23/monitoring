"""
Тяжёлая обработка: скачивание каналов -> композит -> мозаика -> вода
-> 8-бит. Работает НЕЗАВИСИМО от monitor.py (отдельный workflow,
отдельное расписание) и забирает готовые задания из очереди на Google
Drive (queue/pending/), которые туда кладёт readiness.py по итогам
детекции в monitor.py.

ВАЖНО про структуру папок на Google Drive: имена папок для всех
артефактов (bands/, ZIP/, Composites/, Мозаики/, Мозаики/Water/,
Мозаики/8bit/, logs/) подобраны так, чтобы ТОЧНО совпадать с тем, что
использует +S2_L89.ipynb. Если DRIVE_ROOT_FOLDER_ID указывает на ту же
папку "S2" на Диске, которой вы уже пользуетесь вручную -- все
результаты (включая логи) лягут прямо туда, рядом с тем, что создаёт
сам ноутбук.

ВАЖНО про повторные запуски: перед КАЖДЫМ тяжёлым шагом (скачивание
канала, сборка композита, сборка мозаики, пирамиды, вода, 8-бит)
сначала проверяется, нет ли уже готового результата на Google Drive --
если есть, он скачивается вместо пересборки. Так же, как process_order()
в исходном ноутбуке проверяет "если файл уже существует -- пропустить",
только на всех уровнях, а не только для композита.

Также поддерживает РУЧНОЙ запуск в обход очереди и всех порогов
готовности/облачности -- если заданы переменные окружения
ZAKAZ_OVERRIDE и SATELLITE_OVERRIDE (их выставляет workflow_dispatch
в .github/workflows/process.yml), опционально DATE_OVERRIDE.
"""
import logging
import os
import shutil
import time
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


def _write_process_log(job: dict, status: str, elapsed_sec: float, details: dict) -> None:
    """Пишет лог обработки задания в ту же папку logs/, что использует
    monitor.py (и куда же пишет ноутбук) -- отдельным JSON-файлом на
    задание, чтобы был виден результат каждого прогона process.py."""
    entry = {
        "timestamp_local": utils.now_local().strftime("%Y-%m-%d %H:%M:%S"),
        "zakaz": job.get("zakaz"), "satellite": job.get("satellite"), "date": job.get("date"),
        "status": status, "elapsed_sec": round(elapsed_sec, 1), "details": details,
    }
    ts = utils.now_local().strftime("%Y%m%dT%H%M%S")
    blob = f"{config.LOGS_PREFIX}/{ts}_{job.get('zakaz')}_{job.get('satellite')}_process.json"
    try:
        storage.upload_json(blob, entry)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось записать лог обработки на Drive: %s", exc)


# ============================== Sentinel-2: композит с резюмированием ==============================

def _get_or_build_s2_composite(p: dict, selected_bands: list, bands_dir: str, composites_dir: str) -> str:
    sid = p["Name"].replace(".SAFE", "")
    composite_blob = f"{config.COMPOSITES_PREFIX}/{sid}_{config.CHANNELS}.tif"
    composite_local = os.path.join(composites_dir, f"{sid}_{config.CHANNELS}.tif")

    if storage.blob_exists(composite_blob):
        logger.info("Композит %s уже есть на Drive -- скачиваю вместо пересборки", sid)
        storage.download_to_file(composite_blob, composite_local)
        return composite_local

    prod_bands_dir = os.path.join(bands_dir, sid)
    os.makedirs(prod_bands_dir, exist_ok=True)

    # Подтягиваем с Drive уже скачанные ранее каналы (если есть) --
    # download_selected_bands сам пропустит те, что уже лежат локально.
    for band in selected_bands:
        local_band_path = os.path.join(prod_bands_dir, f"{band}.jp2")
        band_blob = f"{config.BANDS_PREFIX}/{sid}/{band}.jp2"
        if not os.path.exists(local_band_path) and storage.blob_exists(band_blob):
            logger.info("Канал %s для %s уже есть на Drive -- скачиваю", band, sid)
            storage.download_to_file(band_blob, local_band_path)

    s2_download.download_selected_bands(p, selected_bands, prod_bands_dir)

    # Заливаем на Drive каналы, которых там ещё не было (в том числе
    # свежескачанные с CDSE только что).
    for band in selected_bands:
        local_band_path = os.path.join(prod_bands_dir, f"{band}.jp2")
        band_blob = f"{config.BANDS_PREFIX}/{sid}/{band}.jp2"
        if os.path.exists(local_band_path) and not storage.blob_exists(band_blob):
            storage.upload_file(local_band_path, band_blob, content_type="image/jp2")

    s2_download.create_composite_from_bands(prod_bands_dir, selected_bands, composite_local)
    storage.upload_file(composite_local, composite_blob, content_type="image/tiff")
    return composite_local


# ============================== Landsat: композит с резюмированием ==============================

def _get_landsat_band(sid: str, entity_id: str, band: str, ee_session, catalog: dict,
                       zip_dir: str, bands_dir_local: str) -> str:
    """Возвращает локальный путь к каналу, используя по порядку:
    локальный файл -> кэш на Drive -> прямое скачивание через EE ->
    извлечение из бандла (при необходимости сначала скачивая сам
    бандл, тоже с проверкой кэша на Drive)."""
    os.makedirs(bands_dir_local, exist_ok=True)
    local_path = os.path.join(bands_dir_local, f"{sid}_{band}.TIF")
    if os.path.exists(local_path):
        return local_path

    band_blob = f"{config.BANDS_PREFIX}/{sid}/{sid}_{band}.TIF"
    if storage.blob_exists(band_blob):
        logger.info("Канал %s для %s уже есть на Drive -- скачиваю", band, sid)
        storage.download_to_file(band_blob, local_path)
        return local_path

    fp = landsat_download.download_band_direct(ee_session, catalog, entity_id, band, bands_dir_local, sid)
    if fp:
        storage.upload_file(fp, band_blob, content_type="image/tiff")
        return fp

    # Не вышло скачать канал напрямую -- нужен бандл.
    bundle_local = os.path.join(zip_dir, f"{sid}_bundle.tar")
    if not os.path.exists(bundle_local):
        bundle_blob = f"{config.ZIP_PREFIX}/{sid}_bundle.tar"
        if storage.blob_exists(bundle_blob):
            logger.info("Бандл %s уже есть на Drive -- скачиваю", sid)
            storage.download_to_file(bundle_blob, bundle_local)
        else:
            landsat_download.download_bundle(ee_session, entity_id, sid, zip_dir)
            storage.upload_file(bundle_local, bundle_blob, content_type="application/x-tar")

    fp = landsat_download.extract_band_from_bundle(bundle_local, band, bands_dir_local)
    if not fp:
        raise RuntimeError(f"Не удалось получить канал {band} для {sid} (ни напрямую, ни из бандла)")
    storage.upload_file(fp, band_blob, content_type="image/tiff")
    return fp


def _get_or_build_landsat_composite(p: dict, selected_bands: list, ee_session, catalog: dict,
                                     zip_dir: str, bands_dir: str, composites_dir: str) -> str:
    sid = p["Name"]
    entity_id = p["Id"]
    composite_blob = f"{config.COMPOSITES_PREFIX}/{sid}_{config.CHANNELS}.tif"
    composite_local = os.path.join(composites_dir, f"{sid}_{config.CHANNELS}.tif")

    if storage.blob_exists(composite_blob):
        logger.info("Композит %s уже есть на Drive -- скачиваю вместо пересборки", sid)
        storage.download_to_file(composite_blob, composite_local)
        return composite_local

    bands_dir_local = os.path.join(bands_dir, sid)
    bands_to_fetch = list(selected_bands) + [config.PAN_BAND_L89]
    band_paths = {
        band: _get_landsat_band(sid, entity_id, band, ee_session, catalog, zip_dir, bands_dir_local)
        for band in bands_to_fetch
    }

    landsat_pansharpen.pansharpen_hpf(
        band_paths, tuple(selected_bands), band_paths[config.PAN_BAND_L89],
        composite_local, block_size=config.PANSHARPEN_BLOCK_SIZE,
    )
    storage.upload_file(composite_local, composite_blob, content_type="image/tiff")
    return composite_local


# ============================== Общая обработка задания ==============================

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

    final_name = _final_mosaic_name(zakaz, date_str, satellite, products)
    mosaic_blob = f"{config.MOSAICS_PREFIX}/{final_name}.tif"
    mosaic_local_path = os.path.join(mosaics_dir, f"{final_name}.tif")

    # --- Мозаика: если уже готова на Drive -- просто скачиваем, минуя всё остальное ---
    if storage.blob_exists(mosaic_blob):
        logger.info("Мозаика %s уже есть на Drive -- скачиваю вместо пересборки", final_name)
        storage.download_to_file(mosaic_blob, mosaic_local_path)
    else:
        composite_paths = []
        if satellite == "S2":
            copernicus.get_access_token()  # прогреваем токен-кэш перед серией скачиваний
            for p in products:
                composite_paths.append(_get_or_build_s2_composite(p, selected_bands, bands_dir, composites_dir))
        else:
            m2m_session = usgs_m2m.get_session()
            if not m2m_session:
                raise RuntimeError("Не удалось авторизоваться в USGS M2M -- обработка Landsat невозможна")
            ee_session = landsat_download.ee_login()
            catalog = landsat_download.dataset_download_options(m2m_session)
            try:
                for p in products:
                    composite_paths.append(
                        _get_or_build_landsat_composite(p, selected_bands, ee_session, catalog, zip_dir, bands_dir, composites_dir)
                    )
            finally:
                usgs_m2m.logout(m2m_session)
                landsat_download.ee_logout(ee_session)

        mosaic.create_mosaic(composite_paths, aoi_shape, target_crs, mosaic_local_path, temp_clip_dir)
        storage.upload_file(mosaic_local_path, mosaic_blob, content_type="image/tiff")

    # --- Пирамиды ---
    ovr_blob = f"{mosaic_blob}.ovr"
    if not storage.blob_exists(ovr_blob):
        if mosaic.build_pyramids(mosaic_local_path):
            ovr_local = mosaic_local_path + ".ovr"
            if os.path.exists(ovr_local):
                storage.upload_file(ovr_local, ovr_blob, content_type="application/octet-stream")

    # --- Вода ---
    water_blob = f"{config.WATER_PREFIX}/{final_name}_water.geojson"
    water_local_path = os.path.join(mosaics_dir, "water", f"{final_name}_water.geojson")
    if storage.blob_exists(water_blob):
        logger.info("Водная маска %s уже есть на Drive -- пропускаю пересборку", final_name)
    else:
        wt = config.WATER_THRESHOLDS[satellite]
        water.extract_water_mask(mosaic_local_path, water_local_path, wt["porog1"], wt["porog2"], wt["ch1"], wt["ch2"])
        storage.upload_file(water_local_path, water_blob, content_type="application/geo+json")

    # --- 8-бит ---
    eb_zstd_blob = f"{config.EIGHTBIT_PREFIX}/{final_name}_8bit_ZSTD.tif"
    eb_jpeg_blob = f"{config.EIGHTBIT_PREFIX}/{final_name}_8bit_JPEG.tif"
    if storage.blob_exists(eb_zstd_blob) and storage.blob_exists(eb_jpeg_blob):
        logger.info("8-бит для %s уже есть на Drive -- пропускаю пересборку", final_name)
    else:
        eb = config.EIGHTBIT_MINMAX[satellite]
        eightbit_paths = eightbit.convert_to_8bit(mosaic_local_path, os.path.join(mosaics_dir, "8bit"),
                                                   min_val=eb["min_val"], max_val=eb["max_val"])
        for eb_path in eightbit_paths:
            eb_name = os.path.basename(eb_path)
            storage.upload_file(eb_path, f"{config.EIGHTBIT_PREFIX}/{eb_name}", content_type="image/tiff")

    result_links = {
        "mosaic": f"drive:{mosaic_blob}",
        "water_geojson": f"drive:{water_blob}",
        "8bit_zstd": f"drive:{eb_zstd_blob}",
        "8bit_jpeg": f"drive:{eb_jpeg_blob}",
    }

    shutil.rmtree(work_dir, ignore_errors=True)
    logger.info("=== Заказ %s (%s) обработан успешно ===", zakaz, satellite)
    return result_links


# ============================== Точки входа ==============================

def _run_queue() -> dict:
    jobs = state_store.list_pending_jobs()
    if not jobs:
        logger.info("Очередь пуста -- нечего обрабатывать")
        return {"jobs_processed": 0, "jobs_failed": 0}

    aoi_dict = aoi_source.load_aoi_dict()
    processed, failed = 0, 0

    for job_blob, job in jobs:
        start = time.time()
        try:
            result = _process_job(job, aoi_dict)
            state_store.mark_job_done(job_blob, job)
            notifier.notify_processing_done(job["zakaz"], job["date"], job["satellite"], result)
            _write_process_log(job, "done", time.time() - start, result)
            processed += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("Задание %s завершилось с ошибкой: %s", job_blob, exc)
            traceback.print_exc()
            state_store.mark_job_failed(job_blob, job, str(exc))
            notifier.notify_processing_failed(job["zakaz"], job["date"], job["satellite"], str(exc))
            _write_process_log(job, "failed", time.time() - start, {"error": str(exc)})
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
    start = time.time()
    try:
        result = _process_job(job, aoi_dict)
        notifier.notify_processing_done(zakaz, monitor_date, satellite, result)
        _write_process_log(job, "done", time.time() - start, result)
        return {"jobs_processed": 1, "jobs_failed": 0}
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        notifier.notify_processing_failed(zakaz, monitor_date, satellite, str(exc))
        _write_process_log(job, "failed", time.time() - start, {"error": str(exc)})
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

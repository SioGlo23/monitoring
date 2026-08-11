"""
Landsat 8/9: авторизация M2M (каталог продуктов для скачивания
отдельных каналов) + веб-сессия earthexplorer.usgs.gov (скачивание
бандла/каналов) + извлечение канала из .tar-бандла. Прямой перенос
Блока 4б + скачивающей части Блока 7б исходного ноутбука.

Функции здесь намеренно "мелкие" (отдельно бандл, отдельно канал
напрямую, отдельно извлечение из бандла) -- process.py сам решает,
какой шаг делать, в зависимости от того, что уже закэшировано на
Google Drive (см. process.py).
"""
import logging
import os
import re
import tarfile

import requests

import config

logger = logging.getLogger("s2monitor.pipeline.landsat_download")

_M2M_BASE = "https://m2m.cr.usgs.gov/api/api/json/stable/"
_M2M_DATASET = "landsat_ot_c2_l1"

_EE_LOGIN_URL = "https://ers.cr.usgs.gov/login/"
_EE_LOGOUT_URL = "https://earthexplorer.usgs.gov/logout"
_EE_DOWNLOAD_URL = "https://earthexplorer.usgs.gov/download/{data_product_id}/{entity_id}/EE/"
_EE_BUNDLE_PRODUCT_IDS = ["632211e26883b1f7", "5e81f14ff4f9941c", "5e81f14f92acf9ef"]

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SatMonitorService/1.0)",
    "Accept": "application/json",
}


def dataset_download_options(m2m_session_id: str) -> dict:
    """{productName.lower(): product_id} -- нужно, чтобы скачивать
    отдельные каналы напрямую, минуя весь .tar-бандл."""
    if not m2m_session_id:
        return {}
    try:
        r = requests.post(
            _M2M_BASE + "dataset-download-options",
            json={"datasetName": _M2M_DATASET},
            headers={"X-Auth-Token": m2m_session_id, **_HTTP_HEADERS},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        return {
            p.get("productName", "").lower(): (p.get("id") or p.get("productId"))
            for p in data if p.get("productName") and (p.get("id") or p.get("productId"))
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("dataset-download-options недоступен (%s) -- каналы будем брать из бандла", exc)
        return {}


def _find_product_id(catalog: dict, must_contain: list) -> str:
    for name, pid in catalog.items():
        if all(s.lower() in name for s in must_contain):
            return pid
    return None


def ee_login() -> requests.Session:
    """Веб-сессия earthexplorer.usgs.gov -- используется тот же аккаунт,
    что и для M2M API (config.M2M_USERNAME/M2M_PASSWORD)."""
    session = requests.Session()
    session.headers.update(_HTTP_HEADERS)
    r = session.get(_EE_LOGIN_URL, timeout=60)
    r.raise_for_status()
    m = re.search(r'name="csrf" value="(.+?)"', r.text)
    if not m:
        raise RuntimeError("Не удалось получить csrf-токен со страницы входа EarthExplorer")
    csrf = m.group(1)

    r = session.post(
        _EE_LOGIN_URL,
        data={"username": config.M2M_USERNAME, "password": config.M2M_PASSWORD, "csrf": csrf},
        allow_redirects=True, timeout=60,
    )
    r.raise_for_status()
    if not session.cookies.get("EROS_SSO_production_secure"):
        raise RuntimeError("Вход на earthexplorer.usgs.gov не удался -- проверьте M2M_USERNAME/M2M_PASSWORD")
    logger.info("EarthExplorer: вход по веб-сессии успешен")
    return session


def ee_logout(session: requests.Session) -> None:
    if not session:
        return
    try:
        session.get(_EE_LOGOUT_URL, timeout=30)
    except Exception:  # noqa: BLE001
        pass


def _ee_download_by_product_id(session: requests.Session, product_id: str, entity_id: str,
                                out_dir: str, label: str) -> str:
    url = _EE_DOWNLOAD_URL.format(data_product_id=product_id, entity_id=entity_id)
    with session.get(url, allow_redirects=False, stream=True, timeout=120) as r:
        r.raise_for_status()
        data = r.json()
    if data.get("errorMessage"):
        raise RuntimeError(data["errorMessage"])
    download_url = data.get("url")
    if not download_url:
        raise RuntimeError("сервер не вернул прямую ссылку на файл")

    os.makedirs(out_dir, exist_ok=True)
    fp = os.path.join(out_dir, label)
    with session.get(download_url, stream=True, allow_redirects=True, timeout=600) as r:
        r.raise_for_status()
        with open(fp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return fp


def download_bundle(ee_session: requests.Session, entity_id: str, sid: str, zip_dir: str) -> str:
    """Скачивает .tar-бандл сцены в zip_dir/{sid}_bundle.tar. Пробует
    несколько известных product_id бандла по очереди. Бросает
    RuntimeError, если ни один вариант не сработал."""
    os.makedirs(zip_dir, exist_ok=True)
    bundle_path = os.path.join(zip_dir, f"{sid}_bundle.tar")

    last_error = None
    for pid in _EE_BUNDLE_PRODUCT_IDS:
        try:
            fp = _ee_download_by_product_id(ee_session, pid, entity_id, zip_dir, f"{sid}_bundle_tmp.tar")
            os.replace(fp, bundle_path)
            logger.info("Бандл скачан: %s", os.path.basename(bundle_path))
            return bundle_path
        except Exception as e:  # noqa: BLE001
            last_error = e
    raise RuntimeError(f"Не удалось скачать бандл {sid}: {last_error}")


def download_band_direct(ee_session: requests.Session, product_catalog: dict, entity_id: str,
                          band: str, out_dir: str, sid: str):
    """Пробует скачать ОДИН канал напрямую (без всего бандла) через
    каталог продуктов M2M. Возвращает локальный путь или None, если
    канал недоступен для прямого скачивания (тогда используйте бандл)."""
    band_num = band.replace("B", "")
    pid = _find_product_id(product_catalog, [f"band {band_num}"])
    if not pid:
        return None
    try:
        fp = _ee_download_by_product_id(ee_session, pid, entity_id, out_dir, f"{sid}_{band}.TIF")
        logger.info("Канал %s скачан отдельно", band)
        return fp
    except Exception as e:  # noqa: BLE001
        logger.warning("Канал %s не удалось скачать отдельно (%s)", band, e)
        return None


def extract_band_from_bundle(bundle_fp: str, band: str, out_dir: str):
    """Извлекает *_{band}.TIF из уже скачанного .tar-бандла. Возвращает
    локальный путь или None, если в бандле нет такого канала."""
    try:
        with tarfile.open(bundle_fp, "r:*") as t:
            member = next((m for m in t.getmembers() if m.name.upper().endswith(f"_{band}.TIF")), None)
            if member is None:
                logger.warning("Канал %s не найден внутри бандла (нет файла *_%s.TIF)", band, band)
                return None
            t.extract(member, out_dir)
            return os.path.join(out_dir, member.name)
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось извлечь %s из бандла: %s", band, e)
        return None

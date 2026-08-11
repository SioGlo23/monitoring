"""
Sentinel-2: поканальное скачивание (parsing manifest.safe -> точные
пути -> Nodes()/$value) + сборка композита из отдельных {band}.jp2.
Прямой перенос Блоков 6+7а исходного ноутбука, адаптированный на
локальные пути (без монтирования Google Drive).
"""
import logging
import os
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

import numpy as np
import rasterio
import requests
from rasterio.warp import Resampling, reproject

from providers import copernicus

logger = logging.getLogger("s2monitor.pipeline.s2_download")

_CDSE_ODATA = "https://download.dataspace.copernicus.eu/odata/v1"
_TOKEN_TTL = 540
_HTTP_RETRIES = 4


class _CdseSession:
    """Обёртка над requests.Session с автообновлением токена CDSE и
    повторами на 401/429/5xx -- нужна на время долгого поканального
    скачивания одной сцены (обычный get_access_token() тут не годится,
    т.к. токен CDSE живёт ~10 минут, а скачивание может занять дольше)."""

    def __init__(self):
        self.session = requests.Session()
        self._token = None
        self._token_ts = 0.0
        self._node_style_quoted = None

    def _get_valid_token(self, force: bool = False) -> str:
        now = time.time()
        if force or not self._token or (now - self._token_ts) > _TOKEN_TTL:
            self._token = copernicus.get_access_token()
            self._token_ts = now
        return self._token

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_valid_token()}"}

    def _http_get(self, url: str, stream: bool = False, timeout=(30, 900)):
        for attempt in range(1, _HTTP_RETRIES + 1):
            try:
                r = self.session.get(url, headers=self._auth_headers(), stream=stream, timeout=timeout)
            except requests.RequestException as e:
                logger.warning("Сетевая ошибка (%s/%s): %s", attempt, _HTTP_RETRIES, e)
                time.sleep(3 * attempt)
                continue

            if r.status_code == 401:
                r.close()
                self._get_valid_token(force=True)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                r.close()
                wait = 5 * attempt
                logger.warning("HTTP %s, повтор через %s с (%s/%s)", r.status_code, wait, attempt, _HTTP_RETRIES)
                time.sleep(wait)
                continue
            return r
        return None

    def _build_node_url(self, prod_id: str, parts, quoted: bool) -> str:
        url = f"{_CDSE_ODATA}/Products({prod_id})"
        for p in parts:
            name = quote(str(p), safe="._-")
            url += f"/Nodes('{name}')" if quoted else f"/Nodes({name})"
        return url

    def node_value_url(self, prod_id: str, parts) -> str:
        st = self._node_style_quoted if self._node_style_quoted is not None else False
        return self._build_node_url(prod_id, parts, quoted=st) + "/$value"

    def probe_node_style(self, prod_id: str, parts) -> None:
        """Определяет, нужны ли кавычки вокруг имени узла в URL -- CDSE
        принимает оба стиля в зависимости от коллекции, поэтому пробуем
        оба на первом запросе и запоминаем рабочий."""
        for st in (False, True):
            url = self._build_node_url(prod_id, parts, quoted=st) + "/Nodes"
            r = self._http_get(url)
            if r is not None and r.status_code == 200:
                self._node_style_quoted = st
                r.close()
                return
            if r is not None:
                r.close()

    def download_node_file(self, prod_id: str, parts, dst: str, expected_size: int = 0) -> bool:
        tmp = dst + ".part"
        url = self.node_value_url(prod_id, parts)

        for attempt in range(1, _HTTP_RETRIES + 1):
            r = self._http_get(url, stream=True)
            if r is None:
                break
            if r.status_code != 200:
                logger.error("HTTP %s для %s", r.status_code, os.path.basename(dst))
                r.close()
                break
            try:
                with r, open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)

                size = os.path.getsize(tmp)
                if expected_size and abs(size - expected_size) > 1024:
                    raise IOError(f"размер {size} != ожидаемого {expected_size}")
                if dst.lower().endswith(".jp2") and not _is_valid_jp2(tmp):
                    raise IOError("файл не похож на JP2 (битая загрузка)")

                os.replace(tmp, dst)
                logger.info("Скачано %s (%.1f МБ)", os.path.basename(dst), size / (1024 * 1024))
                return True
            except Exception as e:
                logger.warning("Ошибка загрузки (%s/%s): %s", attempt, _HTTP_RETRIES, e)
                if os.path.exists(tmp):
                    os.remove(tmp)
                time.sleep(3 * attempt)

        if os.path.exists(tmp):
            os.remove(tmp)
        return False


def _is_valid_jp2(path: str) -> bool:
    try:
        if os.path.getsize(path) < 1024:
            return False
        with open(path, "rb") as f:
            sig = f.read(12)
        return sig.startswith(b"\x00\x00\x00\x0cjP") or sig.startswith(b"\xff\x4f\xff\x51")
    except Exception:
        return False


def download_selected_bands(prod: dict, selected_bands: list, out_dir: str) -> str:
    """Скачивает только нужные каналы Sentinel-2 через parsing manifest.safe.
    Каналы сохраняются под каноническим именем {band}.jp2 в out_dir."""
    prod_name = prod["Name"]
    prod_id = prod["Id"]
    safe_name = prod_name if prod_name.endswith(".SAFE") else f"{prod_name}.SAFE"

    os.makedirs(out_dir, exist_ok=True)
    cdse = _CdseSession()

    manifest_path = os.path.join(out_dir, "manifest.safe")
    logger.info("Читаю manifest.safe для %s...", prod_name[:40])
    cdse.probe_node_style(prod_id, [safe_name, "manifest.safe"])
    if not cdse.download_node_file(prod_id, [safe_name, "manifest.safe"], manifest_path):
        raise RuntimeError(f"Не удалось скачать manifest.safe для {prod_name}")

    root = ET.parse(manifest_path).getroot()

    band_paths = {}
    for elem in root.iter():
        if elem.tag.endswith("fileLocation"):
            href = elem.get("href", "")
            for band in selected_bands:
                if f"_{band}.jp2" in href and band not in band_paths:
                    band_paths[band] = href.lstrip("./")

    if not band_paths:
        raise RuntimeError(f"В manifest.safe не найдены каналы: {selected_bands}")

    downloaded = []
    for band, rel_path in band_paths.items():
        local_path = os.path.join(out_dir, f"{band}.jp2")
        if _is_valid_jp2(local_path):
            downloaded.append(local_path)
            continue

        parts = [safe_name] + [s for s in rel_path.split("/") if s]
        if cdse.download_node_file(prod_id, parts, local_path):
            downloaded.append(local_path)
        else:
            logger.error("Не удалось скачать канал %s для %s", band, prod_name)

    if not downloaded:
        raise RuntimeError(f"Ни один канал не скачан для {prod_name}")
    if len(downloaded) < len(selected_bands):
        logger.warning("Скачано %s/%s каналов для %s -- продолжаем с тем, что есть", len(downloaded), len(selected_bands), prod_name)

    return out_dir


def download_quicklook(prod: dict, out_path: str) -> bool:
    """Скачивает загрубленное превью (quicklook) сцены -- обычно лежит в
    SAFE-пакете по пути preview/quick-look.png. Путь у разных baseline-
    версий Sentinel-2 может немного отличаться, поэтому пробуем несколько
    известных вариантов по очереди. Возвращает False (не бросает
    исключение), если ни один вариант не сработал -- квиклук это
    вспомогательная функция, её отсутствие не должно ронять детекцию."""
    prod_name = prod["Name"]
    prod_id = prod["Id"]
    safe_name = prod_name if prod_name.endswith(".SAFE") else f"{prod_name}.SAFE"

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    cdse = _CdseSession()

    candidates = [
        [safe_name, "preview", "quick-look.png"],
        [safe_name, "preview", "quicklook.png"],
        [safe_name, "preview", "L1C_PVI.jpg"],
    ]
    for parts in candidates:
        try:
            cdse.probe_node_style(prod_id, parts[:-1])
            if cdse.download_node_file(prod_id, parts, out_path):
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Квиклук %s: вариант %s не сработал (%s)", prod_name[:40], "/".join(parts), exc)
    return False


def create_composite_from_bands(bands_path: str, selected_bands: list, output_path: str) -> str:
    """Создаёт многоканальный GeoTIFF из отдельных {band}.jp2. Эталон
    разрешения -- канал с САМЫМ ВЫСОКИМ разрешением, остальные
    ресемплируются под его сетку."""
    band_infos = {}
    for band in selected_bands:
        band_path = os.path.join(bands_path, f"{band}.jp2")
        if not os.path.exists(band_path):
            logger.warning("Пропущен отсутствующий канал: %s", band)
            continue
        with rasterio.open(band_path) as src:
            band_infos[band] = {"width": src.width, "height": src.height, "crs": src.crs, "transform": src.transform}

    if not band_infos:
        raise RuntimeError("Нет данных для композита")

    ref_band = max(band_infos, key=lambda b: band_infos[b]["width"])
    ref = band_infos[ref_band]
    target_crs, target_transform = ref["crs"], ref["transform"]
    target_h, target_w = ref["height"], ref["width"]

    bands_data = []
    for band in selected_bands:
        if band not in band_infos:
            continue
        band_path = os.path.join(bands_path, f"{band}.jp2")
        with rasterio.open(band_path) as src:
            if src.height == target_h and src.width == target_w:
                data = src.read(1).astype(np.float32)
            else:
                data = np.empty((target_h, target_w), dtype=np.float32)
                reproject(
                    source=rasterio.band(src, 1), destination=data,
                    src_transform=src.transform, src_crs=src.crs,
                    dst_transform=target_transform, dst_crs=target_crs,
                    resampling=Resampling.bilinear,
                )
            bands_data.append(data)

    composite_array = np.stack(bands_data, axis=0)
    out_meta = {
        "driver": "GTiff", "height": target_h, "width": target_w,
        "count": len(bands_data), "dtype": "float32",
        "crs": target_crs, "transform": target_transform,
        "compress": "ZSTD", "predictor": 2, "tiled": True,
        "blockxsize": 256, "blockysize": 256, "nodata": 0,
    }

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with rasterio.open(output_path, "w", **out_meta) as dst:
        dst.write(composite_array)

    logger.info("Композит собран: %s", os.path.basename(output_path))
    return output_path

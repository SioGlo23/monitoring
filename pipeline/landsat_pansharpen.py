"""
Паншарпенинг Landsat методом High-Pass Filter, потоково блоками --
прямой перенос вычислительной части Блока 7б исходного ноутбука
(без сети, только растровая обработка уже скачанных файлов).
"""
import logging

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window
from scipy.ndimage import uniform_filter

import utils

logger = logging.getLogger("s2monitor.pipeline.pansharpen")


def _iter_windows(width: int, height: int, block_size: int):
    for row_off in range(0, height, block_size):
        h = min(block_size, height - row_off)
        for col_off in range(0, width, block_size):
            w = min(block_size, width - col_off)
            yield Window(col_off, row_off, w, h)


def _read_overview(path: str, out_shape) -> np.ndarray:
    with rasterio.open(path) as src:
        return src.read(1, out_shape=out_shape, resampling=Resampling.average).astype(np.float32)


def pansharpen_hpf(ms_paths: dict, band_order: tuple, pan_path: str, out_path: str,
                    block_size: int, pad: int = 16) -> str:
    """MS-каналы ресемплируются на сетку Pan "на лету" через WarpedVRT --
    без полноразмерного массива в памяти. NoData: 0 зарезервирован
    ТОЛЬКО под пиксели, которые были nodata в исходных данных."""
    with rasterio.open(pan_path) as pan_src:
        pan_profile = pan_src.profile.copy()
        width, height = pan_src.width, pan_src.height
        pan_crs, pan_transform = pan_src.crs, pan_src.transform

    scale = min(1.0, 1500 / max(width, height))
    out_shape = (max(1, int(height * scale)), max(1, int(width * scale)))
    pan_small = _read_overview(pan_path, out_shape)
    ms_small_mean = np.mean([_read_overview(ms_paths[b], out_shape) for b in band_order], axis=0)
    hpf_scale = float(ms_small_mean.mean() / (pan_small.mean() + 1e-6))

    out_profile = utils.compressed_profile(pan_profile, count=len(band_order), dtype="uint16")
    out_profile["nodata"] = 0

    ms_src = {b: rasterio.open(ms_paths[b]) for b in band_order}
    ms_vrt = {
        b: WarpedVRT(ms_src[b], crs=pan_crs, transform=pan_transform, width=width, height=height,
                     resampling=Resampling.bilinear)
        for b in band_order
    }
    ksize = 2 * pad - 1 if pad > 1 else 3

    try:
        with rasterio.open(pan_path) as pan_src, rasterio.open(out_path, "w", **out_profile) as dst:
            windows = list(_iter_windows(width, height, block_size))
            for wi, window in enumerate(windows, start=1):
                rw = Window(
                    max(0, window.col_off - pad),
                    max(0, window.row_off - pad),
                    min(width, window.col_off + window.width + pad) - max(0, window.col_off - pad),
                    min(height, window.row_off + window.height + pad) - max(0, window.row_off - pad),
                )
                pan_block = pan_src.read(1, window=rw).astype(np.float32)
                ms_blocks = {b: ms_vrt[b].read(1, window=rw).astype(np.float32) for b in band_order}

                invalid_mask = pan_block <= 0
                for b in band_order:
                    invalid_mask |= (ms_blocks[b] <= 0)

                pan_scaled = pan_block * hpf_scale
                lowpass = uniform_filter(pan_scaled, size=ksize)
                detail = pan_scaled - lowpass

                r0, c0 = window.row_off - rw.row_off, window.col_off - rw.col_off
                invalid_crop = invalid_mask[r0:r0 + window.height, c0:c0 + window.width]

                for i, b in enumerate(band_order):
                    sharp = ms_blocks[b] + detail
                    block = sharp[r0:r0 + window.height, c0:c0 + window.width]
                    block = np.clip(block, 1, 65535)
                    block[invalid_crop] = 0
                    dst.write(block.astype(np.uint16), i + 1, window=window)

                del pan_block, ms_blocks, pan_scaled, lowpass, detail, invalid_mask, invalid_crop
                if wi % 20 == 0 or wi == len(windows):
                    logger.info("Паншарп (hpf): обработано %s/%s блоков", wi, len(windows))
    finally:
        for v in ms_vrt.values():
            v.close()
        for s in ms_src.values():
            s.close()

    return out_path

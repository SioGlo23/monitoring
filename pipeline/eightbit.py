"""
Конвертация мозаики в 8-бит (ZSTD + JPEG варианта, с альфа-каналом по
маске валидности) -- прямой перенос Блока 10 исходного ноутбука.
"""
import logging
import os

import numpy as np
import rasterio

logger = logging.getLogger("s2monitor.pipeline.eightbit")


def convert_to_8bit(mosaic_path: str, output_dir: str, min_val: int = 1000, max_val: int = 5000) -> list:
    basename = os.path.basename(mosaic_path)
    os.makedirs(output_dir, exist_ok=True)

    eightbit_zstd = os.path.join(output_dir, basename.replace(".tif", "_8bit_ZSTD.tif"))
    eightbit_jpeg = os.path.join(output_dir, basename.replace(".tif", "_8bit_JPEG.tif"))

    with rasterio.open(mosaic_path) as src:
        channels_to_use = [1, 2, 3]
        nodata_val = src.nodata if src.nodata is not None else 0

        valid_mask = np.zeros((src.height, src.width), dtype=bool)
        raw_bands = {}
        for ch in channels_to_use:
            raw = src.read(ch)
            raw_bands[ch] = raw
            valid_mask |= (raw != nodata_val)

        data_list = []
        for ch in channels_to_use:
            band = np.clip(raw_bands[ch], min_val, max_val)
            band = ((band - min_val) / (max_val - min_val) * 255).astype("uint8")
            data_list.append(band)

        data = np.stack(data_list, axis=0)
        alpha = (valid_mask.astype("uint8")) * 255
        data_with_alpha = np.vstack([data, alpha[None, :, :]])

        meta = src.meta.copy()
        meta.update({"count": 4, "dtype": "uint8", "nodata": None})

        meta_zstd = meta.copy()
        meta_zstd.update({"compress": "ZSTD", "predictor": 2, "tiled": True, "blockxsize": 256, "blockysize": 256})
        with rasterio.open(eightbit_zstd, "w", **meta_zstd) as dst:
            dst.write(data_with_alpha)

        meta_jpeg = meta.copy()
        meta_jpeg.update({"compress": "JPEG", "quality": 75})
        with rasterio.open(eightbit_jpeg, "w", **meta_jpeg) as dst:
            dst.write(data_with_alpha)

    logger.info("8-бит готово (ZSTD + JPEG + Alpha, min=%s/max=%s): %s", min_val, max_val, basename)
    return [eightbit_zstd, eightbit_jpeg]

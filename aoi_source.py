"""Загрузка входных данных (зоны интереса + грид Landsat) с Google Drive.
Общий код для monitor.py и process.py, чтобы не дублироваться."""
import json
import tempfile

import geopandas as gpd

import config
import storage


def load_aoi_dict() -> dict:
    """{"2094": <geojson feature>, ...} -- ключ 'zakaz' из свойств фичи."""
    raw = json.loads(storage.download_text(config.AOI_GEOJSON_BLOB))
    aoi_dict = {}
    for feat in raw.get("features", []):
        zakaz = feat.get("properties", {}).get("zakaz")
        if zakaz is not None:
            aoi_dict[str(zakaz)] = feat
    return aoi_dict


def load_grid_gdf() -> "gpd.GeoDataFrame":
    text = storage.download_text(config.GRID_GEOJSON_BLOB)
    with tempfile.NamedTemporaryFile(suffix=".geojson", mode="w", delete=False, encoding="utf-8") as f:
        f.write(text)
        tmp_path = f.name
    grid_gdf = gpd.read_file(tmp_path)
    if grid_gdf.crs is None:
        grid_gdf = grid_gdf.set_crs("EPSG:4326")
    else:
        grid_gdf = grid_gdf.to_crs("EPSG:4326")
    grid_gdf["PR"] = grid_gdf["PR"].astype(str).str.strip()
    return grid_gdf

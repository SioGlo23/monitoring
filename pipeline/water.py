"""
Выделение водных объектов -- прямой перенос Блока 9 исходного
ноутбука. Пороги (porog1/porog2/ch1/ch2) подаются вызывающим кодом
(process.py берёт их из config.WATER_THRESHOLDS по спутнику).
"""
import logging
import os

import geopandas as gpd
import numpy as np
import rasterio
import rasterio.features
from shapely.geometry import MultiPolygon, Polygon, mapping, shape

try:
    from skimage.morphology import remove_small_holes
except ImportError:  # pragma: no cover
    import subprocess
    import sys
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "scikit-image"], check=True)
    from skimage.morphology import remove_small_holes

logger = logging.getLogger("s2monitor.pipeline.water")


def _pixel_area_m2(src) -> float:
    px_w = abs(src.transform.a)
    px_h = abs(src.transform.e)
    if src.crs and src.crs.is_geographic:
        lat = (src.bounds.top + src.bounds.bottom) / 2
        m_per_deg_lat = 111320.0
        m_per_deg_lon = 111320.0 * np.cos(np.radians(lat))
        return (px_w * m_per_deg_lon) * (px_h * m_per_deg_lat)
    return px_w * px_h


def _remove_small_holes_vector(geom, min_hole_area_m2: float):
    if geom.is_empty:
        return geom
    if geom.geom_type == "Polygon":
        polys = [geom]
    elif geom.geom_type == "MultiPolygon":
        polys = list(geom.geoms)
    else:
        return geom

    cleaned = []
    for poly in polys:
        kept_interiors = [ring for ring in poly.interiors if Polygon(ring).area >= min_hole_area_m2]
        cleaned.append(Polygon(poly.exterior.coords, [list(r.coords) for r in kept_interiors]))

    result = cleaned[0] if len(cleaned) == 1 else MultiPolygon(cleaned)
    return result if result.is_valid else result.buffer(0)


def _chaikin_smooth_ring(coords, iterations: int = 3):
    pts = list(coords)
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        return coords

    for _ in range(iterations):
        new_pts = []
        n = len(pts)
        for i in range(n):
            p0, p1 = pts[i], pts[(i + 1) % n]
            q = (0.75 * p0[0] + 0.25 * p1[0], 0.75 * p0[1] + 0.25 * p1[1])
            r = (0.25 * p0[0] + 0.75 * p1[0], 0.25 * p0[1] + 0.75 * p1[1])
            new_pts.extend([q, r])
        pts = new_pts

    pts.append(pts[0])
    return pts


def _smooth_geom(geom, simplify_tolerance_m: float, iterations: int = 3):
    if geom.is_empty:
        return geom

    def _smooth_polygon(poly):
        simplified = poly.simplify(simplify_tolerance_m, preserve_topology=True)
        if simplified.is_empty or simplified.geom_type != "Polygon":
            simplified = poly
        ext = _chaikin_smooth_ring(list(simplified.exterior.coords), iterations)
        ints = [_chaikin_smooth_ring(list(r.coords), iterations) for r in simplified.interiors]
        try:
            new_poly = Polygon(ext, ints)
            return new_poly if new_poly.is_valid else new_poly.buffer(0)
        except Exception:
            return poly

    if geom.geom_type == "Polygon":
        return _smooth_polygon(geom)
    elif geom.geom_type == "MultiPolygon":
        smoothed = [_smooth_polygon(p) for p in geom.geoms if not p.is_empty]
        smoothed = [g for g in smoothed if not g.is_empty]
        if not smoothed:
            return geom
        return smoothed[0] if len(smoothed) == 1 else MultiPolygon(smoothed)
    return geom


def extract_water_mask(raster_path: str, output_geojson_path: str, porog1: int, porog2: int, ch1: int, ch2: int,
                        min_area_m2: float = 20000, min_hole_area_m2: float = 30000,
                        smooth_iterations: int = 2, simplify_factor: float = 4.5) -> str:
    logger.info(
        "Выделяем водную поверхность: %s (пороги: %s/%s, каналы: %s/%s)",
        os.path.basename(raster_path), porog1, porog2, ch1, ch2,
    )

    with rasterio.open(raster_path) as src:
        band1 = src.read(ch1)
        band2 = src.read(ch2)
        crs = src.crs
        transform = src.transform

        water_bool = (band1 > 1) & (band1 < porog1) & (band2 < porog2)

        pixel_area = _pixel_area_m2(src)
        pixel_size = pixel_area ** 0.5
        simplify_tolerance_m = simplify_factor * pixel_size

        min_hole_px = max(1, int(round(min_hole_area_m2 / pixel_area)))
        water_bool = remove_small_holes(water_bool, area_threshold=min_hole_px)

        water_mask = water_bool.astype("uint8")
        shapes_iter = list(rasterio.features.shapes(water_mask, mask=water_mask > 0, transform=transform))

        features = []
        for geom_dict, _value in shapes_iter:
            geom = shape(geom_dict)
            geom_gdf = gpd.GeoDataFrame(geometry=[geom], crs=crs)
            geom_metric = geom_gdf.to_crs("EPSG:3857").geometry.iloc[0]

            if geom_metric.area <= min_area_m2:
                continue

            geom_metric = _remove_small_holes_vector(geom_metric, min_hole_area_m2)
            if geom_metric.is_empty:
                continue

            geom_metric = _smooth_geom(geom_metric, simplify_tolerance_m, smooth_iterations)
            if geom_metric.is_empty:
                continue

            geom_out = gpd.GeoSeries([geom_metric], crs="EPSG:3857").to_crs(crs).iloc[0]

            features.append({
                "type": "Feature",
                "geometry": mapping(geom_out),
                "properties": {"class": "water", "area_m2": round(geom_metric.area, 2)},
            })

        geojson_data = {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": str(crs)}},
            "features": features,
        }
        os.makedirs(os.path.dirname(output_geojson_path), exist_ok=True)
        import json
        with open(output_geojson_path, "w", encoding="utf-8") as f:
            json.dump(geojson_data, f, ensure_ascii=False, indent=2)

    logger.info("Водная маска сохранена (%s объектов)", len(features))
    return output_geojson_path

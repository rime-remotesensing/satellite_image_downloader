from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Tuple

from rasterio.features import bounds as geometry_bounds


def _load_aoi_geometry(geojson_path: Any) -> Dict[str, Any]:
    from pathlib import Path

    with Path(geojson_path).open("r", encoding="utf-8") as f:
        data = json.load(f)

    if data.get("type") == "FeatureCollection":
        features = data.get("features", [])
        if not features:
            raise ValueError("GeoJSON FeatureCollection has no features")
        geom = features[0].get("geometry")
    elif data.get("type") == "Feature":
        geom = data.get("geometry")
    else:
        geom = data

    if not geom or geom.get("type") not in {"Polygon", "MultiPolygon"}:
        raise ValueError("GeoJSON must contain Polygon or MultiPolygon geometry")

    return geom


def _bbox_from_geometry(geometry: Dict[str, Any]) -> Tuple[float, float, float, float]:
    bounds = geometry_bounds(geometry)
    return float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3])


def _expand_bbox_by_meters(
    bbox: Tuple[float, float, float, float],
    buffer_m: float,
) -> Tuple[float, float, float, float]:
    if buffer_m <= 0:
        return bbox

    west, south, east, north = bbox
    center_lat = max(-89.9999, min(89.9999, (south + north) / 2.0))

    meters_per_degree_lat = 111320.0
    meters_per_degree_lon = max(1.0, 111320.0 * math.cos(math.radians(center_lat)))

    dlat = buffer_m / meters_per_degree_lat
    dlon = buffer_m / meters_per_degree_lon

    expanded = (
        max(-180.0, west - dlon),
        max(-90.0, south - dlat),
        min(180.0, east + dlon),
        min(90.0, north + dlat),
    )
    return expanded


def _point_on_segment(
    x: float,
    y: float,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    eps: float = 1e-12,
) -> bool:
    cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
    if abs(cross) > eps:
        return False

    if x < min(x1, x2) - eps or x > max(x1, x2) + eps:
        return False
    if y < min(y1, y2) - eps or y > max(y1, y2) + eps:
        return False
    return True


def _point_in_ring(lon: float, lat: float, ring: List[List[float]]) -> bool:
    if len(ring) < 3:
        return False

    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]

        if _point_on_segment(lon, lat, x1, y1, x2, y2):
            return True

        intersects = ((y1 > lat) != (y2 > lat)) and (
            lon < (x2 - x1) * (lat - y1) / ((y2 - y1) + 1e-300) + x1
        )
        if intersects:
            inside = not inside
    return inside


def _point_in_geometry(lon: float, lat: float, geometry: Dict[str, Any]) -> bool:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return False

    def in_polygon(poly_coords: List[List[List[float]]]) -> bool:
        outer = poly_coords[0]
        holes = poly_coords[1:]
        if not _point_in_ring(lon, lat, outer):
            return False
        for hole in holes:
            if _point_in_ring(lon, lat, hole):
                return False
        return True

    if gtype == "Polygon":
        return in_polygon(coords)
    if gtype == "MultiPolygon":
        return any(in_polygon(poly) for poly in coords)
    return False

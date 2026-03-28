from __future__ import annotations

from shapely.geometry import box
from shapely.geometry.base import BaseGeometry

from height_fusion_pipeline.config import InputConfig
from height_fusion_pipeline.utils import load_geojson_geometry


def resolve_request_geometry(input_config: InputConfig) -> BaseGeometry:
    if input_config.boundary_geojson:
        return load_geojson_geometry(input_config.boundary_geojson)
    if input_config.bbox:
        return box(*input_config.bbox)
    raise ValueError("Either bbox or boundary_geojson must be provided.")

from __future__ import annotations

import logging
import os

import geopandas as gpd

from height_fusion_pipeline.config import JobConfig
from height_fusion_pipeline.geometry import resolve_request_geometry
from height_fusion_pipeline.logging_utils import log_timed_step
from height_fusion_pipeline.raster import (
    CanopySource,
    build_target_grid,
    fuse_canopy_and_buildings,
    reproject_buildings_to_grid,
    upload_geotiff_to_s3,
)
from height_fusion_pipeline.vector import OvertureBuildingSource


LOGGER = logging.getLogger(__name__)


def run_pipeline(config: JobConfig) -> str:
    request_geom_wgs84 = resolve_request_geometry(config.input)
    LOGGER.info("Request bounds (EPSG:4326): %s", request_geom_wgs84.bounds)

    vector_source = OvertureBuildingSource(config.overture)
    canopy_source = CanopySource(config.canopy)

    buildings_wgs84 = vector_source.fetch_buildings(request_geom_wgs84)
    canopy_headers = canopy_source.find_intersecting_tiles(request_geom_wgs84)
    grid = build_target_grid(canopy_headers, request_geom_wgs84)
    LOGGER.info(
        "Target raster grid: width=%s height=%s crs=%s bounds=%s",
        grid.width,
        grid.height,
        grid.crs,
        grid.bounds,
    )

    with log_timed_step(LOGGER, "reproject building geometries"):
        buildings_projected = reproject_buildings_to_grid(buildings_wgs84, grid)
        request_geom_projected = gpd.GeoSeries([request_geom_wgs84], crs="EPSG:4326").to_crs(grid.crs).iloc[0]

    local_output = fuse_canopy_and_buildings(
        headers=canopy_headers,
        buildings=buildings_projected,
        grid=grid,
        processing=config.processing,
        request_geometry=request_geom_projected,
        temp_dir=config.output.temp_dir,
    )

    with log_timed_step(LOGGER, "upload fused geotiff to s3"):
        upload_geotiff_to_s3(local_output, config.output.output_s3_uri)

    try:
        os.remove(local_output)
    except OSError:
        LOGGER.warning("Unable to remove temporary output file: %s", local_output)

    LOGGER.info("Pipeline completed successfully. Output uploaded to %s", config.output.output_s3_uri)
    return config.output.output_s3_uri

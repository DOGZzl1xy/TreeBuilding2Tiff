from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from dataclasses import fields, replace
from pathlib import Path

import geopandas as gpd
from shapely.geometry.base import BaseGeometry

from height_fusion_pipeline import __version__
from height_fusion_pipeline.config import (
    CanopyConfig,
    InputConfig,
    JobConfig,
    OutputConfig,
    OvertureConfig,
    ProcessingConfig,
)
from height_fusion_pipeline.logging_utils import log_timed_step
from height_fusion_pipeline.raster import (
    CanopySource,
    FusionGrid,
    RasterHeader,
    build_target_grid,
    empty_buildings,
    fuse_canopy_and_buildings,
    reproject_buildings_to_grid,
    upload_geotiff_to_s3,
)
from height_fusion_pipeline.utils import is_s3_uri, read_text, resolve_request_geometry
from height_fusion_pipeline.vector import OvertureBuildingSource

LOGGER = logging.getLogger(__name__)


def run_pipeline(config: JobConfig) -> str:
    """Fuse heights for the configured bbox or boundary and write one GeoTIFF."""
    request_geom_wgs84 = resolve_request_geometry(config.input.bbox, config.input.boundary_geojson)
    return fuse_geometry(
        request_geom_wgs84,
        config,
        output_uri=config.output.output_uri,
        debug_dir=config.output.debug_dir,
    )


def fuse_geometry(
    request_geom_wgs84: BaseGeometry,
    config: JobConfig,
    *,
    output_uri: str,
    debug_dir: str | None = None,
) -> str:
    LOGGER.info("Request bounds (EPSG:4326): %s", request_geom_wgs84.bounds)

    buildings_wgs84 = OvertureBuildingSource(config.overture).fetch_buildings(request_geom_wgs84)
    canopy_headers = CanopySource(config.canopy).find_intersecting_tiles(request_geom_wgs84)
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

    fused_path = fuse_canopy_and_buildings(
        headers=canopy_headers,
        buildings=buildings_projected,
        grid=grid,
        processing=config.processing,
        request_geometry=request_geom_projected,
        temp_dir=config.output.temp_dir,
    )

    if debug_dir:
        write_debug_artifacts(
            Path(debug_dir),
            config=config,
            request_geom_wgs84=request_geom_wgs84,
            request_geom_projected=request_geom_projected,
            buildings_wgs84=buildings_wgs84,
            headers=canopy_headers,
            grid=grid,
        )

    destination = publish_output(fused_path, output_uri)
    LOGGER.info("Output written to %s", destination)
    return destination


def safe_name(value: object, fallback: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._")
    return name or fallback


def load_features(boundary_geojson: str) -> gpd.GeoDataFrame:
    """Read a FeatureCollection from a local path, S3 URI, or inline GeoJSON."""
    text = boundary_geojson
    if is_s3_uri(boundary_geojson) or not boundary_geojson.lstrip().startswith("{"):
        text = read_text(boundary_geojson)
    payload = json.loads(text)
    features = payload.get("features") if payload.get("type") == "FeatureCollection" else [payload]
    frame = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
    return frame[frame.geometry.notna() & ~frame.geometry.is_empty].reset_index(drop=True)


def run_each_feature(config: JobConfig, *, name_field: str | None = None) -> list[dict[str, object]]:
    """Write one GeoTIFF per boundary feature into the output directory or S3 prefix.

    Failures are logged and recorded without stopping the batch. A
    ``batch_summary.json`` is written next to local outputs.
    """
    if not config.input.boundary_geojson:
        raise ValueError("--each-feature requires --boundary-geojson.")
    features = load_features(config.input.boundary_geojson)
    if name_field and name_field not in features.columns:
        raise ValueError(f"Field {name_field!r} not found; available: {sorted(features.columns)}")

    release = OvertureBuildingSource(config.overture).resolve_release()
    config = replace(config, overture=replace(config.overture, release=release))
    base = config.output.output_uri.rstrip("/")

    results: list[dict[str, object]] = []
    used: set[str] = set()
    for position, row in features.iterrows():
        name = safe_name(row[name_field] if name_field else position, f"feature_{position}")
        if name in used:
            name = f"{name}_{position}"
        used.add(name)
        output_uri = f"{base}/{name}.tif"
        debug_dir = str(Path(config.output.debug_dir) / name) if config.output.debug_dir else None
        started = time.perf_counter()
        LOGGER.info("[%s/%s] %s", position + 1, len(features), name)
        try:
            destination = fuse_geometry(row.geometry, config, output_uri=output_uri, debug_dir=debug_dir)
            status, error = "ok", None
        except Exception as exc:  # noqa: BLE001 - keep the batch going
            LOGGER.exception("Feature %s failed", name)
            destination, status, error = None, "failed", str(exc)
        results.append(
            {
                "name": name,
                "status": status,
                "output": destination,
                "error": error,
                "seconds": round(time.perf_counter() - started, 1),
            }
        )

    summary = {"overture_release": release, "features": results}
    if not is_s3_uri(base):
        Path(base).mkdir(parents=True, exist_ok=True)
        (Path(base) / "batch_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    failed = sum(item["status"] != "ok" for item in results)
    LOGGER.info("Batch finished: %s succeeded, %s failed.", len(results) - failed, failed)
    return results


def fuse_heights(
    output: str,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    boundary: str | None = None,
    debug_dir: str | None = None,
    **options: object,
) -> str:
    """Python entry point: ``fuse_heights("out.tif", bbox=(...), fusion_mode="max")``.

    ``options`` are field names from the config dataclasses, for example
    ``overture_release``/``release``, ``fusion_mode``, ``chunk_size``,
    ``meters_per_floor``, ``default_building_height_m``, ``tile_index_geojson``,
    ``cache_dir``, ``all_touched``, ``output_nodata``, ``temp_dir``.
    """
    sections = {
        "overture": OvertureConfig(),
        "canopy": CanopyConfig(),
        "processing": ProcessingConfig(),
        "output": OutputConfig(output_uri=output, debug_dir=debug_dir),
    }
    if "overture_release" in options:
        options["release"] = options.pop("overture_release")
    for key, value in options.items():
        section = next((name for name, obj in sections.items() if key in {f.name for f in fields(obj)}), None)
        if section is None:
            raise TypeError(f"Unknown option: {key}")
        sections[section] = replace(sections[section], **{key: value})
    config = JobConfig(input=InputConfig(bbox=bbox, boundary_geojson=boundary), **sections)
    return run_pipeline(config)


def publish_output(local_path: str, output_uri: str) -> str:
    """Move the finished GeoTIFF to a local path or upload it to S3.

    If an S3 upload fails the temporary file is kept and its path logged so the
    fused result is not lost.
    """
    if is_s3_uri(output_uri):
        with log_timed_step(LOGGER, "upload fused geotiff to s3"):
            try:
                upload_geotiff_to_s3(local_path, output_uri)
            except Exception:
                LOGGER.error("S3 upload failed; fused GeoTIFF kept at %s", local_path)
                raise
        try:
            os.remove(local_path)
        except OSError:
            LOGGER.warning("Unable to remove temporary output file: %s", local_path)
        return output_uri

    destination = Path(output_uri).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(local_path, destination)
    destination.chmod(0o644)  # mkstemp creates owner-only files
    return str(destination)


def write_debug_artifacts(
    debug_dir: Path,
    *,
    config: JobConfig,
    request_geom_wgs84: BaseGeometry,
    request_geom_projected: BaseGeometry,
    buildings_wgs84: gpd.GeoDataFrame,
    headers: list[RasterHeader],
    grid: FusionGrid,
) -> None:
    """Write QGIS comparison layers: boundary, buildings, canopy-only raster, metadata."""
    debug_dir.mkdir(parents=True, exist_ok=True)
    with log_timed_step(LOGGER, f"write debug artifacts to {debug_dir}"):
        gpd.GeoDataFrame(
            {"name": ["request_boundary"]},
            geometry=gpd.GeoSeries([request_geom_wgs84], crs="EPSG:4326"),
        ).to_file(debug_dir / "request_boundary.geojson", driver="GeoJSON")
        if not buildings_wgs84.empty:
            buildings_wgs84.to_file(debug_dir / "buildings.geojson", driver="GeoJSON")
        fuse_canopy_and_buildings(
            headers=headers,
            buildings=empty_buildings(grid.crs),
            grid=grid,
            processing=config.processing,
            request_geometry=request_geom_projected,
            output_path=str(debug_dir / "canopy_only.tif"),
        )
        metadata = {
            "package_version": __version__,
            "request_bounds_epsg4326": list(request_geom_wgs84.bounds),
            "buildings_count": len(buildings_wgs84),
            "fusion_mode": config.processing.fusion_mode,
            "selected_canopy_tiles": [
                {
                    "uri": header.uri,
                    "crs": str(header.crs),
                    "bounds": list(header.bounds),
                    "width": header.width,
                    "height": header.height,
                    "resolution": list(header.res),
                }
                for header in headers
            ],
            "target_grid": {
                "crs": str(grid.crs),
                "width": grid.width,
                "height": grid.height,
                "bounds": list(grid.bounds),
            },
        }
        (debug_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

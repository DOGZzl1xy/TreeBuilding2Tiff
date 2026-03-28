from __future__ import annotations

import argparse
import json
import logging
from dataclasses import replace
from pathlib import Path

import geopandas as gpd
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"

import sys

sys.path.insert(0, str(SRC_DIR))

from height_fusion_pipeline.config import (  # noqa: E402
    CanopyConfig,
    InputConfig,
    JobConfig,
    OvertureConfig,
    OutputConfig,
    ProcessingConfig,
)
from height_fusion_pipeline.geometry import resolve_request_geometry  # noqa: E402
from height_fusion_pipeline.logging_utils import setup_logging  # noqa: E402
from height_fusion_pipeline.raster import (  # noqa: E402
    CanopySource,
    RasterHeader,
    build_target_grid,
    fuse_canopy_and_buildings,
    reproject_buildings_to_grid,
)
from height_fusion_pipeline.utils import build_unsigned_s3_client, parse_s3_uri  # noqa: E402
from height_fusion_pipeline.vector import OvertureBuildingSource  # noqa: E402


LOGGER = logging.getLogger("local_validation")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export local validation artifacts for a small bbox: buildings.geojson, canopy_only.tif, fused_height.tif."
    )
    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        required=True,
        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
        help="Bounding box in EPSG:4326.",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        help="Directory where local validation artifacts will be written.",
    )
    parser.add_argument("--overture-release", default="latest")
    parser.add_argument("--overture-bucket", default="overturemaps-us-west-2")
    parser.add_argument("--overture-region", default="us-west-2")
    parser.add_argument(
        "--canopy-prefix",
        default="s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/",
    )
    parser.add_argument("--canopy-region", default="us-east-1")
    parser.add_argument("--canopy-tile-index")
    parser.add_argument("--max-scan-tiles", type=int)
    parser.add_argument("--disable-full-prefix-scan", action="store_true")
    parser.add_argument("--chunk-size", type=int, default=1024)
    parser.add_argument("--fusion-mode", choices=["max", "building_priority"], default="max")
    parser.add_argument("--all-touched", action="store_true")
    parser.add_argument("--default-building-height", type=float, default=4.0)
    parser.add_argument("--meters-per-floor", type=float, default=3.0)
    parser.add_argument("--output-nodata", type=float, default=0.0)
    parser.add_argument("--preserve-empty-as-nodata", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser


def build_job_config(args: argparse.Namespace) -> JobConfig:
    return JobConfig(
        input=InputConfig(bbox=tuple(args.bbox), boundary_geojson=None),
        overture=OvertureConfig(
            s3_bucket=args.overture_bucket,
            s3_region=args.overture_region,
            release=args.overture_release,
            default_building_height_m=args.default_building_height,
            meters_per_floor=args.meters_per_floor,
        ),
        canopy=CanopyConfig(
            s3_uri_prefix=args.canopy_prefix,
            s3_region=args.canopy_region,
            tile_index_geojson=args.canopy_tile_index,
            max_scan_tiles=args.max_scan_tiles,
            allow_full_prefix_scan=not args.disable_full_prefix_scan,
        ),
        processing=ProcessingConfig(
            chunk_size=args.chunk_size,
            all_touched=args.all_touched,
            fusion_mode=args.fusion_mode,
            output_nodata=args.output_nodata,
            preserve_empty_as_nodata=args.preserve_empty_as_nodata,
        ),
        output=OutputConfig(output_s3_uri="", temp_dir=None),
        log_level=args.log_level,
    )


def empty_buildings_for_grid(crs: object) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"height_m": pd.Series(dtype="float32")},
        geometry=gpd.GeoSeries([], crs=crs),
        crs=crs,
    )


def write_metadata(
    metadata_path: Path,
    bbox: tuple[float, float, float, float],
    headers,
    buildings_count: int,
    grid,
) -> None:
    payload = {
        "bbox_epsg4326": list(bbox),
        "buildings_count": buildings_count,
        "selected_canopy_tiles": [
            {
                "uri": header.uri,
                "crs": str(header.crs),
                "bounds": list(header.bounds),
                "width": header.width,
                "height": header.height,
                "resolution": list(header.res),
                "size_bytes": header.size_bytes,
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
    metadata_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def download_canopy_tiles(headers: list[RasterHeader], out_dir: Path, region_name: str) -> list[RasterHeader]:
    canopy_tile_dir = out_dir / "canopy_tiles"
    canopy_tile_dir.mkdir(parents=True, exist_ok=True)
    client = build_unsigned_s3_client(region_name)

    local_headers: list[RasterHeader] = []
    for header in headers:
        if not header.uri.startswith("s3://"):
            local_headers.append(header)
            continue

        bucket, key = parse_s3_uri(header.uri)
        local_path = canopy_tile_dir / Path(key).name
        if not local_path.exists():
            LOGGER.info("Downloading canopy tile %s to %s", header.uri, local_path)
            client.download_file(bucket, key, str(local_path))
        local_headers.append(replace(header, uri=str(local_path)))

    return local_headers


def main() -> None:
    args = build_parser().parse_args()
    config = build_job_config(args)
    setup_logging(config.log_level)

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    request_geom_wgs84 = resolve_request_geometry(config.input)
    vector_source = OvertureBuildingSource(config.overture)
    canopy_source = CanopySource(config.canopy)

    LOGGER.info("Fetching Overture buildings for %s", request_geom_wgs84.bounds)
    buildings_wgs84 = vector_source.fetch_buildings(request_geom_wgs84)

    LOGGER.info("Discovering canopy tiles")
    canopy_headers = canopy_source.find_intersecting_tiles(request_geom_wgs84)
    local_canopy_headers = download_canopy_tiles(canopy_headers, out_dir=out_dir, region_name=config.canopy.s3_region)
    grid = build_target_grid(local_canopy_headers, request_geom_wgs84)

    LOGGER.info("Reprojecting buildings to raster grid")
    buildings_projected = reproject_buildings_to_grid(buildings_wgs84, grid)
    request_geom_projected = gpd.GeoSeries([request_geom_wgs84], crs="EPSG:4326").to_crs(grid.crs).iloc[0]

    boundary_path = out_dir / "request_boundary.geojson"
    buildings_path = out_dir / "buildings.geojson"
    canopy_only_path = out_dir / "canopy_only.tif"
    fused_path = out_dir / "fused_height.tif"
    metadata_path = out_dir / "metadata.json"

    gpd.GeoDataFrame(
        {"name": ["request_bbox"]},
        geometry=gpd.GeoSeries([request_geom_wgs84], crs="EPSG:4326"),
        crs="EPSG:4326",
    ).to_file(boundary_path, driver="GeoJSON")
    buildings_wgs84.to_file(buildings_path, driver="GeoJSON")

    LOGGER.info("Writing canopy-only raster to %s", canopy_only_path)
    fuse_canopy_and_buildings(
        headers=local_canopy_headers,
        buildings=empty_buildings_for_grid(grid.crs),
        grid=grid,
        processing=config.processing,
        request_geometry=request_geom_projected,
        output_path=str(canopy_only_path),
    )

    LOGGER.info("Writing fused raster to %s", fused_path)
    fuse_canopy_and_buildings(
        headers=local_canopy_headers,
        buildings=buildings_projected,
        grid=grid,
        processing=config.processing,
        request_geometry=request_geom_projected,
        output_path=str(fused_path),
    )

    write_metadata(
        metadata_path=metadata_path,
        bbox=tuple(args.bbox),
        headers=canopy_headers,
        buildings_count=len(buildings_wgs84),
        grid=grid,
    )

    LOGGER.info("Local validation artifacts written to %s", out_dir)


if __name__ == "__main__":
    main()

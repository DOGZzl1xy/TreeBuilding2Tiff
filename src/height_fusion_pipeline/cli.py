from __future__ import annotations

import argparse
from pathlib import Path

from height_fusion_pipeline.config import (
    DEFAULT_CACHE_DIR,
    DEFAULT_CANOPY_PREFIX,
    DEFAULT_CANOPY_TILE_INDEX,
    CanopyConfig,
    InputConfig,
    JobConfig,
    OutputConfig,
    OvertureConfig,
    ProcessingConfig,
)
from height_fusion_pipeline.logging_utils import setup_logging
from height_fusion_pipeline.pipeline import run_each_feature, run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fuse Meta canopy height with Overture buildings into a GeoTIFF.")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
        help="Bounding box in EPSG:4326.",
    )
    source_group.add_argument(
        "--boundary-geojson",
        help="Local path, S3 URI, or inline GeoJSON geometry/feature/feature collection.",
    )

    parser.add_argument(
        "--output",
        "--output-s3",
        dest="output",
        required=True,
        help="Output GeoTIFF: a local path or an s3:// URI.",
    )
    parser.add_argument(
        "--each-feature",
        action="store_true",
        help="With --boundary-geojson: write one GeoTIFF per feature; --output is then a directory or S3 prefix.",
    )
    parser.add_argument("--name-field", help="Feature property used to name per-feature outputs (default: index).")
    parser.add_argument(
        "--debug-dir",
        help="Also write request_boundary.geojson, buildings.geojson, canopy_only.tif and metadata.json here.",
    )
    parser.add_argument("--overture-release", default="latest", help="Overture release or 'latest'.")
    parser.add_argument("--overture-bucket", default="overturemaps-us-west-2")
    parser.add_argument("--overture-region", default="us-west-2")
    parser.add_argument("--default-building-height", type=float, default=4.0)
    parser.add_argument("--meters-per-floor", type=float, default=3.0)

    parser.add_argument("--canopy-prefix", default=DEFAULT_CANOPY_PREFIX, help="Meta canopy tile S3 prefix.")
    parser.add_argument("--canopy-region", default="us-east-1")
    parser.add_argument(
        "--canopy-tile-index",
        default=DEFAULT_CANOPY_TILE_INDEX,
        help="Canopy tile index GeoJSON (local path or S3 URI).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help="Cache directory for the downloaded tile index.",
    )

    parser.add_argument("--chunk-size", type=int, default=2048)
    parser.add_argument("--fusion-mode", choices=["max", "building_priority"], default="max")
    parser.add_argument("--all-touched", action="store_true")
    parser.add_argument("--output-nodata", type=float, default=0.0)
    parser.add_argument("--preserve-empty-as-nodata", action="store_true")
    parser.add_argument("--temp-dir", help="Optional local temp directory for intermediate GeoTIFF.")
    parser.add_argument("--log-level", default="INFO")
    return parser


def config_from_args(args: argparse.Namespace) -> JobConfig:
    return JobConfig(
        input=InputConfig(
            bbox=tuple(args.bbox) if args.bbox else None,
            boundary_geojson=args.boundary_geojson,
        ),
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
            cache_dir=args.cache_dir,
        ),
        processing=ProcessingConfig(
            chunk_size=args.chunk_size,
            all_touched=args.all_touched,
            fusion_mode=args.fusion_mode,
            output_nodata=args.output_nodata,
            preserve_empty_as_nodata=args.preserve_empty_as_nodata,
        ),
        output=OutputConfig(output_uri=args.output, temp_dir=args.temp_dir, debug_dir=args.debug_dir),
        log_level=args.log_level,
    )


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.each_feature and not args.boundary_geojson:
        parser.error("--each-feature requires --boundary-geojson")
    config = config_from_args(args)
    setup_logging(config.log_level)
    if args.each_feature:
        results = run_each_feature(config, name_field=args.name_field)
        if any(item["status"] != "ok" for item in results):
            raise SystemExit(1)
        return
    run_pipeline(config)

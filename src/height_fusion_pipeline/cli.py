from __future__ import annotations

import argparse

from height_fusion_pipeline.config import (
    CanopyConfig,
    InputConfig,
    JobConfig,
    OvertureConfig,
    OutputConfig,
    ProcessingConfig,
)
from height_fusion_pipeline.logging_utils import setup_logging
from height_fusion_pipeline.pipeline import run_pipeline


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

    parser.add_argument("--output-s3", required=True, help="Target S3 URI for fused GeoTIFF.")
    parser.add_argument("--overture-release", default="latest", help="Overture release or 'latest'.")
    parser.add_argument("--overture-bucket", default="overturemaps-us-west-2")
    parser.add_argument("--overture-region", default="us-west-2")
    parser.add_argument("--default-building-height", type=float, default=4.0)
    parser.add_argument("--meters-per-floor", type=float, default=3.0)

    parser.add_argument(
        "--canopy-prefix",
        default="s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/",
        help="Meta canopy tile S3 prefix.",
    )
    parser.add_argument("--canopy-region", default="us-east-1")
    parser.add_argument("--canopy-tile-index", help="Optional GeoJSON tile index path or S3 URI.")
    parser.add_argument("--max-scan-tiles", type=int, help="Optional safety limit when scanning canopy prefix.")
    parser.add_argument("--disable-full-prefix-scan", action="store_true")

    parser.add_argument("--chunk-size", type=int, default=2048)
    parser.add_argument("--fusion-mode", choices=["max", "building_priority"], default="max")
    parser.add_argument("--all-touched", action="store_true")
    parser.add_argument("--output-nodata", type=float, default=0.0)
    parser.add_argument("--preserve-empty-as-nodata", action="store_true")
    parser.add_argument("--temp-dir", help="Optional local temp directory for intermediate GeoTIFF.")
    parser.add_argument("--log-level", default="INFO")
    return parser


def parse_args() -> JobConfig:
    args = build_parser().parse_args()
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
        output=OutputConfig(output_s3_uri=args.output_s3, temp_dir=args.temp_dir),
        log_level=args.log_level,
    )


def main() -> None:
    config = parse_args()
    setup_logging(config.log_level)
    run_pipeline(config)

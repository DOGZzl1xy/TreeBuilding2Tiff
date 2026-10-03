from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

FusionMode = Literal["max", "building_priority"]

DEFAULT_CANOPY_PREFIX = "s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/"
DEFAULT_CANOPY_TILE_INDEX = DEFAULT_CANOPY_PREFIX + "tiles.geojson"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "height_fusion"


@dataclass(slots=True)
class InputConfig:
    bbox: tuple[float, float, float, float] | None = None
    boundary_geojson: str | None = None


@dataclass(slots=True)
class OvertureConfig:
    s3_bucket: str = "overturemaps-us-west-2"
    s3_region: str = "us-west-2"
    release: str = "latest"
    theme_path: str = "theme=buildings/type=building/*.parquet"
    default_building_height_m: float = 4.0
    meters_per_floor: float = 3.0


@dataclass(slots=True)
class CanopyConfig:
    s3_uri_prefix: str = DEFAULT_CANOPY_PREFIX
    s3_region: str = "us-east-1"
    tile_index_geojson: str = DEFAULT_CANOPY_TILE_INDEX
    cache_dir: Path = DEFAULT_CACHE_DIR


@dataclass(slots=True)
class ProcessingConfig:
    chunk_size: int = 2048
    all_touched: bool = False
    fusion_mode: FusionMode = "max"
    output_dtype: str = "float32"
    output_nodata: float = 0.0
    preserve_empty_as_nodata: bool = False


@dataclass(slots=True)
class OutputConfig:
    output_uri: str = ""
    temp_dir: str | None = None
    debug_dir: str | None = None


@dataclass(slots=True)
class JobConfig:
    input: InputConfig
    overture: OvertureConfig
    canopy: CanopyConfig
    processing: ProcessingConfig
    output: OutputConfig
    log_level: str = "INFO"

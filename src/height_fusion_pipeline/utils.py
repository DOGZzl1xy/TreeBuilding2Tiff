from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore import UNSIGNED
from botocore.client import Config
from shapely import union_all
from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry


def is_s3_uri(uri: str) -> bool:
    return uri.startswith("s3://")


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError(f"Expected s3:// URI, got: {uri}")
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    return bucket, key


def build_unsigned_s3_client(region_name: str) -> boto3.client:
    return boto3.client("s3", region_name=region_name, config=Config(signature_version=UNSIGNED))


def read_text(path_or_uri: str, s3_region: str | None = None) -> str:
    if is_s3_uri(path_or_uri):
        bucket, key = parse_s3_uri(path_or_uri)
        client = build_unsigned_s3_client(s3_region or "us-east-1")
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        return body.decode("utf-8")
    return Path(path_or_uri).read_text(encoding="utf-8")


def cached_s3_download(uri: str, region_name: str, cache_dir: Path) -> Path:
    """Download a public S3 object once into ``cache_dir/<bucket>/<key>``.

    The object is written to a ``.part`` file and renamed when complete so an
    interrupted transfer is never mistaken for a valid cached copy.
    """
    bucket, key = parse_s3_uri(uri)
    destination = cache_dir / bucket / key
    if destination.exists() and destination.stat().st_size > 0:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    try:
        build_unsigned_s3_client(region_name).download_file(bucket, key, str(partial))
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)
    return destination


def load_geojson_geometry(path_or_geojson: str, s3_region: str | None = None) -> BaseGeometry:
    """Load a request geometry from a local path, S3 URI, or inline GeoJSON.

    Feature collections are dissolved into one geometry so every feature in a
    multi-part boundary is covered.
    """
    if is_s3_uri(path_or_geojson):
        payload = json.loads(read_text(path_or_geojson, s3_region=s3_region))
    elif path_or_geojson.lstrip().startswith("{"):
        payload = json.loads(path_or_geojson)
    else:
        payload = json.loads(Path(path_or_geojson).read_text(encoding="utf-8"))

    if payload.get("type") == "FeatureCollection":
        geometries = [shape(feature["geometry"]) for feature in payload.get("features", []) if feature.get("geometry")]
        if not geometries:
            raise ValueError("GeoJSON FeatureCollection is empty.")
        return union_all(geometries)
    if payload.get("type") == "Feature":
        return shape(payload["geometry"])
    return shape(payload)


def first_present(mapping: dict[str, Any], keys: list[str]) -> Any | None:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def resolve_request_geometry(
    bbox: tuple[float, float, float, float] | None = None, boundary_geojson: str | None = None
) -> BaseGeometry:
    """Request area in EPSG:4326 from a bbox or a GeoJSON source."""
    if boundary_geojson:
        return load_geojson_geometry(boundary_geojson)
    if bbox:
        return box(*bbox)
    raise ValueError("Either bbox or boundary_geojson must be provided.")

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore import UNSIGNED
from botocore.client import Config
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry


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
    if path_or_uri.startswith("s3://"):
        bucket, key = parse_s3_uri(path_or_uri)
        client = build_unsigned_s3_client(s3_region or "us-east-1")
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        return body.decode("utf-8")
    return Path(path_or_uri).read_text(encoding="utf-8")


def download_s3_to_tempfile(uri: str, region_name: str) -> str:
    bucket, key = parse_s3_uri(uri)
    client = build_unsigned_s3_client(region_name)
    suffix = Path(key).suffix or ".tmp"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    client.download_file(bucket, key, tmp_path)
    return tmp_path


def load_geojson_geometry(path_or_geojson: str, s3_region: str | None = None) -> BaseGeometry:
    maybe_path = Path(path_or_geojson)
    if path_or_geojson.startswith("s3://"):
        raw = read_text(path_or_geojson, s3_region=s3_region)
        payload = json.loads(raw)
    elif maybe_path.exists():
        payload = json.loads(maybe_path.read_text(encoding="utf-8"))
    else:
        payload = json.loads(path_or_geojson)

    if payload.get("type") == "FeatureCollection":
        features = payload.get("features", [])
        if not features:
            raise ValueError("GeoJSON FeatureCollection is empty.")
        return shape(features[0]["geometry"])
    if payload.get("type") == "Feature":
        return shape(payload["geometry"])
    return shape(payload)


def safe_int_ceil(value: float) -> int:
    return int(math.ceil(value))


def bytes_to_human(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0 or unit == "TB":
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"


def ensure_parent_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def first_present(mapping: dict[str, Any], keys: list[str]) -> Any | None:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None

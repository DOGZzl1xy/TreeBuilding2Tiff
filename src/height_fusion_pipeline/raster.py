from __future__ import annotations

import logging
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import boto3
import geopandas as gpd
import numpy as np
import rasterio
from pyproj import Transformer
from rasterio import windows
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.io import DatasetReader
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry

from height_fusion_pipeline.config import CanopyConfig, ProcessingConfig
from height_fusion_pipeline.logging_utils import log_timed_step
from height_fusion_pipeline.utils import (
    build_unsigned_s3_client,
    bytes_to_human,
    download_s3_to_tempfile,
    first_present,
    parse_s3_uri,
)


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RasterHeader:
    uri: str
    crs: object
    bounds: tuple[float, float, float, float]
    transform: Affine
    width: int
    height: int
    res: tuple[float, float]
    size_bytes: int


@dataclass(slots=True)
class CanopySource:
    config: CanopyConfig

    def find_intersecting_tiles(self, request_geom_wgs84: BaseGeometry) -> list[RasterHeader]:
        with log_timed_step(LOGGER, "discover canopy tiles"):
            if self.config.tile_index_geojson:
                headers = self._tiles_from_index(request_geom_wgs84)
            else:
                headers = self._tiles_from_prefix_scan(request_geom_wgs84)

        if not headers:
            raise RuntimeError("No canopy GeoTIFF tile intersects the requested geometry.")

        total_size = sum(item.size_bytes for item in headers)
        LOGGER.info(
            "Selected %s canopy tile(s), estimated source size %s.",
            len(headers),
            bytes_to_human(total_size),
        )
        self._validate_headers(headers)
        return headers

    def _tiles_from_index(self, request_geom_wgs84: BaseGeometry) -> list[RasterHeader]:
        temp_path = None
        path = self.config.tile_index_geojson
        try:
            if path is None:
                return []
            if path.startswith("s3://"):
                temp_path = download_s3_to_tempfile(path, self.config.s3_region)
                source_path = temp_path
            else:
                source_path = path
            index_gdf = gpd.read_file(source_path)
            if index_gdf.crs is None:
                index_gdf = index_gdf.set_crs("EPSG:4326")
            request_series = gpd.GeoSeries([request_geom_wgs84], crs="EPSG:4326").to_crs(index_gdf.crs)
            matches = index_gdf[index_gdf.intersects(request_series.iloc[0])].copy()
            if matches.empty:
                return []

            headers: list[RasterHeader] = []
            for _, row in matches.iterrows():
                uri = first_present(row.to_dict(), ["href", "uri", "path", "tile", "location", "asset"])
                if not uri:
                    continue
                uri = str(uri)
                if not uri.startswith("s3://"):
                    prefix_bucket, prefix_key = parse_s3_uri(self.config.s3_uri_prefix)
                    relative_uri = uri.lstrip("/")
                    if not Path(relative_uri).suffix:
                        relative_uri = f"chm/{relative_uri}.tif"
                    uri = f"s3://{prefix_bucket}/{prefix_key.rstrip('/')}/{relative_uri}"
                header = self._read_raster_header(uri)
                if self._intersects_header(header, request_geom_wgs84):
                    headers.append(header)
            return headers
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

    def _tiles_from_prefix_scan(self, request_geom_wgs84: BaseGeometry) -> list[RasterHeader]:
        if not self.config.allow_full_prefix_scan:
            raise RuntimeError("canopy.tile_index_geojson is not set and full prefix scan is disabled.")

        bucket, prefix = parse_s3_uri(self.config.s3_uri_prefix)
        client = build_unsigned_s3_client(self.config.s3_region)
        paginator = client.get_paginator("list_objects_v2")

        headers: list[RasterHeader] = []
        scanned = 0
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.lower().endswith((".tif", ".tiff")):
                    continue
                scanned += 1
                if self.config.max_scan_tiles and scanned > self.config.max_scan_tiles:
                    LOGGER.warning(
                        "Reached canopy scan limit (%s). Provide a tile index for large datasets.",
                        self.config.max_scan_tiles,
                    )
                    return headers
                uri = f"s3://{bucket}/{key}"
                header = self._read_raster_header(uri, size_bytes=obj.get("Size", 0))
                if self._intersects_header(header, request_geom_wgs84):
                    headers.append(header)

        LOGGER.info("Scanned %s canopy object(s) under %s.", scanned, self.config.s3_uri_prefix)
        return headers

    def _read_raster_header(self, uri: str, size_bytes: int = 0) -> RasterHeader:
        with rasterio.Env(AWS_NO_SIGN_REQUEST="YES", AWS_REGION=self.config.s3_region):
            with rasterio.open(_to_rasterio_path(uri)) as src:
                return RasterHeader(
                    uri=uri,
                    crs=src.crs,
                    bounds=src.bounds,
                    transform=src.transform,
                    width=src.width,
                    height=src.height,
                    res=src.res,
                    size_bytes=size_bytes,
                )

    def _intersects_header(self, header: RasterHeader, request_geom_wgs84: BaseGeometry) -> bool:
        transformer = Transformer.from_crs("EPSG:4326", header.crs, always_xy=True)
        request_in_tile = _transform_bounds(request_geom_wgs84.bounds, transformer)
        return box(*header.bounds).intersects(box(*request_in_tile))

    def _validate_headers(self, headers: list[RasterHeader]) -> None:
        base = headers[0]
        for header in headers[1:]:
            if str(header.crs) != str(base.crs):
                raise RuntimeError(
                    "Selected canopy tiles do not share the same CRS. "
                    "Split the job into smaller regions with a single tile CRS."
                )
            if not np.allclose(header.res, base.res):
                raise RuntimeError("Selected canopy tiles do not share the same resolution.")


@dataclass(slots=True)
class FusionGrid:
    crs: object
    transform: Affine
    width: int
    height: int
    bounds: tuple[float, float, float, float]


def build_target_grid(headers: list[RasterHeader], request_geom_wgs84: BaseGeometry) -> FusionGrid:
    reference = headers[0]
    transformer = Transformer.from_crs("EPSG:4326", reference.crs, always_xy=True)
    request_bounds = _transform_bounds(request_geom_wgs84.bounds, transformer)

    union_bounds = None
    request_box = box(*request_bounds)
    for header in headers:
        overlap = box(*header.bounds).intersection(request_box)
        if overlap.is_empty:
            continue
        if union_bounds is None:
            union_bounds = overlap.bounds
        else:
            union_bounds = (
                min(union_bounds[0], overlap.bounds[0]),
                min(union_bounds[1], overlap.bounds[1]),
                max(union_bounds[2], overlap.bounds[2]),
                max(union_bounds[3], overlap.bounds[3]),
            )

    if union_bounds is None:
        raise RuntimeError("No overlap between request geometry and canopy tiles after reprojection.")

    raw_window = windows.from_bounds(*union_bounds, transform=reference.transform)
    col_start = math.floor(raw_window.col_off)
    row_start = math.floor(raw_window.row_off)
    col_stop = math.ceil(raw_window.col_off + raw_window.width)
    row_stop = math.ceil(raw_window.row_off + raw_window.height)
    aligned_window = windows.Window(
        col_off=col_start,
        row_off=row_start,
        width=col_stop - col_start,
        height=row_stop - row_start,
    )
    aligned_transform = windows.transform(aligned_window, reference.transform)
    aligned_bounds = windows.bounds(aligned_window, reference.transform)

    return FusionGrid(
        crs=reference.crs,
        transform=aligned_transform,
        width=int(aligned_window.width),
        height=int(aligned_window.height),
        bounds=aligned_bounds,
    )


def reproject_buildings_to_grid(buildings_wgs84: gpd.GeoDataFrame, grid: FusionGrid) -> gpd.GeoDataFrame:
    if buildings_wgs84.empty:
        return gpd.GeoDataFrame(buildings_wgs84.copy(), geometry="geometry", crs=grid.crs)
    projected = buildings_wgs84.to_crs(grid.crs)
    projected = projected[projected.geometry.notnull() & ~projected.geometry.is_empty].copy()
    return projected


def fuse_canopy_and_buildings(
    headers: list[RasterHeader],
    buildings: gpd.GeoDataFrame,
    grid: FusionGrid,
    processing: ProcessingConfig,
    request_geometry: BaseGeometry | None = None,
    output_path: str | None = None,
    temp_dir: str | None = None,
) -> str:
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".tif", dir=temp_dir)
        os.close(fd)

    profile = {
        "driver": "GTiff",
        "width": grid.width,
        "height": grid.height,
        "count": 1,
        "dtype": processing.output_dtype,
        "crs": grid.crs,
        "transform": grid.transform,
        "nodata": processing.output_nodata,
        "compress": "deflate",
        "tiled": True,
        "predictor": 3,
        "BIGTIFF": "IF_SAFER",
    }

    spatial_index = None
    if not buildings.empty:
        try:
            spatial_index = buildings.sindex
        except Exception:
            spatial_index = None

    with rasterio.Env(AWS_NO_SIGN_REQUEST="YES"):
        with rasterio.open(output_path, "w", **profile) as dst:
            with _OpenWarpedVRTs(headers, grid) as vrts:
                for window in _iter_windows(grid.width, grid.height, processing.chunk_size):
                    canopy_chunk, canopy_valid = _read_canopy_chunk(vrts, window, processing.output_nodata)
                    building_chunk = _rasterize_buildings_chunk(
                        buildings=buildings,
                        spatial_index=spatial_index,
                        window=window,
                        base_transform=grid.transform,
                        fill_value=0.0,
                        all_touched=processing.all_touched,
                    )
                    fused_chunk = _fuse_arrays(
                        canopy_chunk=canopy_chunk,
                        canopy_valid=canopy_valid,
                        building_chunk=building_chunk,
                        processing=processing,
                    )
                    if request_geometry is not None:
                        fused_chunk = _apply_geometry_mask(
                            fused_chunk=fused_chunk,
                            geometry=request_geometry,
                            window=window,
                            base_transform=grid.transform,
                            nodata=processing.output_nodata,
                        )
                    dst.write(fused_chunk.astype(processing.output_dtype), 1, window=window)

    return output_path


def upload_geotiff_to_s3(local_path: str, s3_uri: str) -> None:
    bucket, key = parse_s3_uri(s3_uri)
    client = boto3.client("s3")
    client.upload_file(local_path, bucket, key, ExtraArgs={"ContentType": "image/tiff"})


class _OpenWarpedVRTs:
    def __init__(self, headers: list[RasterHeader], grid: FusionGrid) -> None:
        self.headers = headers
        self.grid = grid
        self._datasets: list[DatasetReader] = []
        self._vrts: list[WarpedVRT] = []

    def __enter__(self) -> list[WarpedVRT]:
        for header in self.headers:
            ds = rasterio.open(_to_rasterio_path(header.uri))
            vrt = WarpedVRT(
                ds,
                crs=self.grid.crs,
                transform=self.grid.transform,
                width=self.grid.width,
                height=self.grid.height,
                resampling=Resampling.nearest,
            )
            self._datasets.append(ds)
            self._vrts.append(vrt)
        return self._vrts

    def __exit__(self, exc_type, exc, tb) -> None:
        for vrt in self._vrts:
            vrt.close()
        for ds in self._datasets:
            ds.close()


def _read_canopy_chunk(vrts: list[WarpedVRT], window: windows.Window, nodata: float) -> tuple[np.ndarray, np.ndarray]:
    out = None
    valid = None
    for vrt in vrts:
        data = vrt.read(1, window=window, masked=True)
        chunk = np.asarray(data.filled(nodata), dtype="float32")
        chunk_valid = ~np.asarray(data.mask)

        if out is None:
            out = np.full(chunk.shape, nodata, dtype="float32")
            valid = np.zeros(chunk.shape, dtype=bool)

        out = np.where(chunk_valid, np.maximum(out, chunk), out)
        valid |= chunk_valid

    if out is None or valid is None:
        height = int(window.height)
        width = int(window.width)
        return np.full((height, width), nodata, dtype="float32"), np.zeros((height, width), dtype=bool)

    return out, valid


def _rasterize_buildings_chunk(
    buildings: gpd.GeoDataFrame,
    spatial_index,
    window: windows.Window,
    base_transform: Affine,
    fill_value: float,
    all_touched: bool,
) -> np.ndarray:
    height = int(window.height)
    width = int(window.width)
    if buildings.empty:
        return np.full((height, width), fill_value, dtype="float32")

    win_bounds = windows.bounds(window, base_transform)
    query_geom = box(*win_bounds)

    if spatial_index is not None:
        candidate_idx = list(spatial_index.intersection(query_geom.bounds))
        subset = buildings.iloc[candidate_idx]
    else:
        subset = buildings

    subset = subset[subset.intersects(query_geom)].copy()
    if subset.empty:
        return np.full((height, width), fill_value, dtype="float32")

    subset = subset.sort_values("height_m")
    shapes = ((geom, value) for geom, value in zip(subset.geometry, subset["height_m"]))
    return rasterize(
        shapes=shapes,
        out_shape=(height, width),
        transform=windows.transform(window, base_transform),
        fill=fill_value,
        dtype="float32",
        all_touched=all_touched,
    )


def _fuse_arrays(
    canopy_chunk: np.ndarray,
    canopy_valid: np.ndarray,
    building_chunk: np.ndarray,
    processing: ProcessingConfig,
) -> np.ndarray:
    if processing.fusion_mode == "building_priority":
        fused = np.where(building_chunk > 0, building_chunk, canopy_chunk)
    elif processing.fusion_mode == "max":
        fused = np.maximum(canopy_chunk, building_chunk)
    else:
        raise ValueError(f"Unsupported fusion mode: {processing.fusion_mode}")

    if processing.preserve_empty_as_nodata:
        empty_mask = (~canopy_valid) & (building_chunk <= 0)
        fused = fused.astype("float32", copy=False)
        fused[empty_mask] = processing.output_nodata

    return fused


def _apply_geometry_mask(
    fused_chunk: np.ndarray,
    geometry: BaseGeometry,
    window: windows.Window,
    base_transform: Affine,
    nodata: float,
) -> np.ndarray:
    mask = rasterize(
        shapes=[(geometry, 1)],
        out_shape=(int(window.height), int(window.width)),
        transform=windows.transform(window, base_transform),
        fill=0,
        dtype="uint8",
    )
    out = fused_chunk.astype("float32", copy=True)
    out[mask == 0] = nodata
    return out


def _iter_windows(width: int, height: int, chunk_size: int) -> Iterable[windows.Window]:
    for row_off in range(0, height, chunk_size):
        for col_off in range(0, width, chunk_size):
            win_width = min(chunk_size, width - col_off)
            win_height = min(chunk_size, height - row_off)
            yield windows.Window(col_off=col_off, row_off=row_off, width=win_width, height=win_height)


def _transform_bounds(bounds: tuple[float, float, float, float], transformer: Transformer) -> tuple[float, float, float, float]:
    xs = [bounds[0], bounds[2], bounds[0], bounds[2]]
    ys = [bounds[1], bounds[1], bounds[3], bounds[3]]
    tx, ty = transformer.transform(xs, ys)
    return min(tx), min(ty), max(tx), max(ty)


def _to_rasterio_path(uri: str) -> str:
    if not uri.startswith("s3://"):
        return uri
    bucket, key = parse_s3_uri(uri)
    return f"/vsis3/{bucket}/{key}"

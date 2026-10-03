from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import duckdb
import geopandas as gpd
import pandas as pd
from shapely.geometry.base import BaseGeometry

from height_fusion_pipeline.config import OvertureConfig
from height_fusion_pipeline.logging_utils import log_timed_step
from height_fusion_pipeline.utils import build_unsigned_s3_client

LOGGER = logging.getLogger(__name__)

RELEASE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}(\.\d+)?$")
BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
QUERY_SQL = """
    SELECT id, height, num_floors, class, subtype, ST_AsWKB(geometry) AS geom_wkb
    FROM read_parquet(?, filename=true, hive_partitioning=1)
    WHERE bbox.xmin < ? AND bbox.xmax > ? AND bbox.ymin < ? AND bbox.ymax > ?
"""


def overture_buildings_path(bucket: str, release: str, theme_path: str) -> str:
    """Build the Overture parquet glob, rejecting values that are not plain identifiers."""
    if not BUCKET_PATTERN.match(bucket):
        raise ValueError(f"Invalid Overture bucket name: {bucket!r}")
    if not RELEASE_PATTERN.match(release):
        raise ValueError(f"Invalid Overture release {release!r}; expected e.g. 2026-03-18.0")
    return f"s3://{bucket}/release/{release}/{theme_path}"


@dataclass(slots=True)
class OvertureBuildingSource:
    config: OvertureConfig

    def fetch_buildings(self, request_geom_wgs84: BaseGeometry) -> gpd.GeoDataFrame:
        bounds = request_geom_wgs84.bounds
        with log_timed_step(LOGGER, "query overture buildings"):
            with duckdb.connect() as conn:
                self._prepare_duckdb(conn)
                release = self._resolve_release(conn)
                s3_path = overture_buildings_path(self.config.s3_bucket, release, self.config.theme_path)
                min_x, min_y, max_x, max_y = bounds
                df = conn.execute(QUERY_SQL, [s3_path, max_x, min_x, max_y, min_y]).fetch_df()

        if df.empty:
            LOGGER.warning("No Overture buildings found inside request bounds.")
            empty = gpd.GeoDataFrame(
                df.drop(columns=["geom_wkb"], errors="ignore"),
                geometry=gpd.GeoSeries([], crs="EPSG:4326"),
                crs="EPSG:4326",
            )
            empty["height_m"] = pd.Series(dtype="float32")
            return empty

        gdf = gpd.GeoDataFrame(
            df.drop(columns=["geom_wkb"]),
            geometry=gpd.GeoSeries.from_wkb(df["geom_wkb"].map(bytes)),
            crs="EPSG:4326",
        )
        gdf = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty].copy()
        gdf = gdf[gdf.intersects(request_geom_wgs84)].copy()
        gdf["height_m"] = self._estimate_height(gdf)
        gdf = gdf[gdf["height_m"] > 0].copy()

        LOGGER.info("Fetched %s building footprints from Overture.", f"{len(gdf):,}")
        null_heights = int(pd.to_numeric(gdf.get("height"), errors="coerce").isna().sum())
        LOGGER.info("Buildings requiring imputed height: %s", f"{null_heights:,}")
        return gdf

    def resolve_release(self) -> str:
        """Concrete Overture release name (resolves ``latest`` once)."""
        with duckdb.connect() as conn:
            self._prepare_duckdb(conn)
            return self._resolve_release(conn)

    def _prepare_duckdb(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._install_or_load_extension(conn, "httpfs")
        self._install_or_load_extension(conn, "spatial")
        conn.execute("SET s3_region = ?;", [self.config.s3_region])

    def _resolve_release(self, conn: duckdb.DuckDBPyConnection) -> str:
        if self.config.release != "latest":
            return self.config.release
        try:
            client = build_unsigned_s3_client(self.config.s3_region)
            response = client.list_objects_v2(Bucket=self.config.s3_bucket, Prefix="release/", Delimiter="/")
            prefixes = [
                item["Prefix"].removeprefix("release/").removesuffix("/") for item in response.get("CommonPrefixes", [])
            ]
            prefixes = [prefix for prefix in prefixes if RELEASE_PATTERN.match(prefix)]
            if prefixes:
                release = max(prefixes)
                LOGGER.info("Resolved latest Overture release from S3: %s", release)
                return release
        except Exception:
            LOGGER.debug("Failed to resolve latest Overture release from S3; falling back to STAC.", exc_info=True)
        try:
            query = "SELECT latest FROM read_json_auto('https://stac.overturemaps.org/catalog.json');"
            release = conn.execute(query).fetchone()[0]
            LOGGER.info("Resolved latest Overture release from STAC: %s", release)
            return release
        except Exception as exc:
            raise RuntimeError(
                "Failed to resolve latest Overture release from STAC. "
                "Pass a fixed --overture-release if your runtime has no outbound HTTPS access."
            ) from exc

    def _estimate_height(self, gdf: gpd.GeoDataFrame) -> pd.Series:
        height = pd.to_numeric(gdf.get("height"), errors="coerce")
        floors = pd.to_numeric(gdf.get("num_floors"), errors="coerce")
        estimated = floors * self.config.meters_per_floor
        resolved = height.where(height.notna() & (height > 0), estimated)
        resolved = resolved.fillna(self.config.default_building_height_m)
        return resolved.astype("float32")

    def _install_or_load_extension(self, conn: duckdb.DuckDBPyConnection, extension: str) -> None:
        try:
            conn.execute(f"INSTALL {extension};")
        except Exception:
            LOGGER.debug("DuckDB INSTALL %s failed; attempting LOAD directly.", extension)
        conn.execute(f"LOAD {extension};")

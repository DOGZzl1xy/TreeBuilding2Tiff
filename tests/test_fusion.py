"""Offline tests with synthetic canopy rasters and building footprints."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from height_fusion_pipeline import fuse_heights
from height_fusion_pipeline.cli import build_parser, config_from_args, main
from height_fusion_pipeline.config import CanopyConfig, OvertureConfig, ProcessingConfig
from height_fusion_pipeline.pipeline import load_features, publish_output, run_each_feature, safe_name
from height_fusion_pipeline.raster import (
    CanopySource,
    _fuse_arrays,
    _iter_windows,
    build_target_grid,
    empty_buildings,
    fuse_canopy_and_buildings,
)
from height_fusion_pipeline.utils import load_geojson_geometry
from height_fusion_pipeline.vector import OvertureBuildingSource, overture_buildings_path

CRS = "EPSG:4326"
RES = 0.001  # degrees; synthetic tile spans lon 0-0.1, lat 0-0.1
CANOPY_HEIGHT = 10.0


def write_canopy_tile(path: Path, *, nodata_block: bool = False) -> None:
    data = np.full((100, 100), CANOPY_HEIGHT, dtype="float32")
    if nodata_block:
        data[:10, :10] = -9999.0
    profile = {
        "driver": "GTiff",
        "width": 100,
        "height": 100,
        "count": 1,
        "dtype": "float32",
        "crs": CRS,
        "transform": from_origin(0.0, 0.1, RES, RES),
        "nodata": -9999.0,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)


class FusionArrayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.canopy = np.array([[5.0, 5.0], [0.0, 20.0]], dtype="float32")
        self.valid = np.array([[True, True], [False, True]])
        self.buildings = np.array([[12.0, 0.0], [0.0, 8.0]], dtype="float32")

    def test_max_mode_takes_taller_surface(self) -> None:
        fused = _fuse_arrays(self.canopy, self.valid, self.buildings, ProcessingConfig(fusion_mode="max"))
        np.testing.assert_array_equal(fused, [[12.0, 5.0], [0.0, 20.0]])

    def test_building_priority_mode_overrides_canopy(self) -> None:
        processing = ProcessingConfig(fusion_mode="building_priority")
        fused = _fuse_arrays(self.canopy, self.valid, self.buildings, processing)
        np.testing.assert_array_equal(fused, [[12.0, 5.0], [0.0, 8.0]])

    def test_preserve_empty_as_nodata_marks_cells_without_any_source(self) -> None:
        processing = ProcessingConfig(preserve_empty_as_nodata=True, output_nodata=-1.0)
        fused = _fuse_arrays(self.canopy, self.valid, self.buildings, processing)
        self.assertEqual(fused[1, 0], -1.0)

    def test_windows_cover_grid_exactly_once(self) -> None:
        coverage = np.zeros((7, 11), dtype=int)
        for window in _iter_windows(width=11, height=7, chunk_size=4):
            rows = slice(window.row_off, window.row_off + window.height)
            cols = slice(window.col_off, window.col_off + window.width)
            coverage[rows, cols] += 1
        self.assertTrue((coverage == 1).all())


class RasterPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.tile = self.root / "tile.tif"
        write_canopy_tile(self.tile)
        self.source = CanopySource(CanopyConfig())

    def tearDown(self) -> None:
        self._directory.cleanup()

    def test_target_grid_is_pixel_aligned_to_canopy_tile(self) -> None:
        header = self.source._read_raster_header(str(self.tile))
        grid = build_target_grid([header], box(0.0105, 0.0205, 0.0295, 0.0395))

        self.assertAlmostEqual(grid.transform.c, 0.010)
        self.assertAlmostEqual(grid.transform.f, 0.040)
        self.assertEqual((grid.width, grid.height), (20, 20))

    def test_fused_raster_combines_canopy_and_buildings(self) -> None:
        header = self.source._read_raster_header(str(self.tile))
        request = box(0.0, 0.0, 0.05, 0.05)
        grid = build_target_grid([header], request)
        buildings = gpd.GeoDataFrame(
            {"height_m": np.array([30.0, 4.0], dtype="float32")},
            geometry=[box(0.010, 0.010, 0.020, 0.020), box(0.030, 0.030, 0.040, 0.040)],
            crs=CRS,
        )
        output = self.root / "fused.tif"
        fuse_canopy_and_buildings(
            [header],
            buildings,
            grid,
            ProcessingConfig(chunk_size=16),
            request_geometry=request,
            output_path=str(output),
        )

        with rasterio.open(output) as src:
            data = src.read(1)
            row, col = src.index(0.015, 0.015)
            self.assertEqual(data[row, col], 30.0)  # building taller than canopy
            row, col = src.index(0.035, 0.035)
            self.assertEqual(data[row, col], CANOPY_HEIGHT)  # canopy taller than building
            self.assertEqual(src.crs.to_string(), CRS)

    def test_canopy_only_raster_and_local_publish(self) -> None:
        header = self.source._read_raster_header(str(self.tile))
        request = box(0.0, 0.0, 0.02, 0.02)
        grid = build_target_grid([header], request)
        temp_path = fuse_canopy_and_buildings([header], empty_buildings(grid.crs), grid, ProcessingConfig())
        destination = publish_output(temp_path, str(self.root / "nested" / "canopy.tif"))

        self.assertFalse(Path(temp_path).exists())
        with rasterio.open(destination) as src:
            self.assertTrue(np.allclose(src.read(1), CANOPY_HEIGHT))

    def test_tile_index_entries_resolve_to_chm_tiles(self) -> None:
        source = CanopySource(CanopyConfig(s3_uri_prefix="s3://bucket/forests/v1/"))
        self.assertEqual(source._resolve_tile_uri("132122232"), "s3://bucket/forests/v1/chm/132122232.tif")
        self.assertEqual(source._resolve_tile_uri("s3://other/x.tif"), "s3://other/x.tif")

    def test_local_tile_index_selects_intersecting_tiles(self) -> None:
        index = self.root / "tiles.geojson"
        gpd.GeoDataFrame(
            {"href": [str(self.tile), "/missing.tif"]}, geometry=[box(0, 0, 0.1, 0.1), box(5, 5, 6, 6)], crs=CRS
        ).to_file(index, driver="GeoJSON")
        source = CanopySource(CanopyConfig(tile_index_geojson=str(index)))
        headers = source.find_intersecting_tiles(box(0.01, 0.01, 0.02, 0.02))
        self.assertEqual([header.uri for header in headers], [str(self.tile)])


class InputAndConfigTest(unittest.TestCase):
    def test_feature_collection_unions_all_features(self) -> None:
        collection = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "properties": {}, "geometry": box(0, 0, 1, 1).__geo_interface__},
                {"type": "Feature", "properties": {}, "geometry": box(2, 0, 3, 1).__geo_interface__},
            ],
        }
        geometry = load_geojson_geometry(json.dumps(collection))
        self.assertAlmostEqual(geometry.area, 2.0)
        self.assertEqual(geometry.bounds, (0.0, 0.0, 3.0, 1.0))

    def test_geojson_loaded_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "boundary.geojson"
            path.write_text(json.dumps(box(0, 0, 2, 2).__geo_interface__), encoding="utf-8")
            self.assertAlmostEqual(load_geojson_geometry(str(path)).area, 4.0)

    def test_height_estimation_prefers_height_then_floors_then_default(self) -> None:
        source = OvertureBuildingSource(OvertureConfig(meters_per_floor=3.0, default_building_height_m=4.0))
        frame = gpd.GeoDataFrame(
            {"height": [25.0, None, None, 0.0], "num_floors": [None, 5, None, None]},
            geometry=[box(0, 0, 1, 1)] * 4,
        )
        np.testing.assert_allclose(source._estimate_height(frame), [25.0, 15.0, 4.0, 4.0])

    def test_overture_path_rejects_unsafe_release(self) -> None:
        self.assertEqual(
            overture_buildings_path("overturemaps-us-west-2", "2026-03-18.0", "theme=buildings/*.parquet"),
            "s3://overturemaps-us-west-2/release/2026-03-18.0/theme=buildings/*.parquet",
        )
        with self.assertRaises(ValueError):
            overture_buildings_path("overturemaps-us-west-2", "x'; DROP TABLE t; --", "theme")

    def test_cli_accepts_local_output_and_legacy_s3_flag(self) -> None:
        parser = build_parser()
        local = config_from_args(parser.parse_args(["--bbox", "0", "0", "1", "1", "--output", "out.tif"]))
        legacy = config_from_args(parser.parse_args(["--bbox", "0", "0", "1", "1", "--output-s3", "s3://b/k.tif"]))
        self.assertEqual(local.output.output_uri, "out.tif")
        self.assertTrue(local.canopy.tile_index_geojson.endswith("tiles.geojson"))
        self.assertEqual(legacy.output.output_uri, "s3://b/k.tif")

    def test_each_feature_requires_boundary(self) -> None:
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            main(["--bbox", "0", "0", "1", "1", "--output", "out", "--each-feature"])


class BatchAndApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.boundary = self.root / "towns.geojson"
        gpd.GeoDataFrame(
            {"GEOID": ["001", "002", "001"], "name": ["A town", "B/town", "dup"]},
            geometry=[box(0, 0, 1, 1), box(2, 0, 3, 1), box(4, 0, 5, 1)],
            crs=CRS,
        ).to_file(self.boundary, driver="GeoJSON")

    def tearDown(self) -> None:
        self._directory.cleanup()

    def test_safe_name_strips_path_characters(self) -> None:
        self.assertEqual(safe_name("B/town", "x"), "B_town")
        self.assertEqual(safe_name("..", "fallback"), "fallback")

    def test_load_features_reads_every_feature(self) -> None:
        self.assertEqual(len(load_features(str(self.boundary))), 3)

    def test_each_feature_names_outputs_and_records_failures(self) -> None:
        def fake_fuse(geometry, config, *, output_uri, debug_dir=None):
            if geometry.bounds[0] == 2:
                raise RuntimeError("no canopy tile")
            self.assertEqual(config.overture.release, "2026-09-23.1")
            return output_uri

        args = build_parser().parse_args(
            ["--boundary-geojson", str(self.boundary), "--output", str(self.root / "out"), "--each-feature"]
        )
        with (
            mock.patch.object(OvertureBuildingSource, "resolve_release", return_value="2026-09-23.1"),
            mock.patch("height_fusion_pipeline.pipeline.fuse_geometry", side_effect=fake_fuse),
            self.assertLogs("height_fusion_pipeline.pipeline", level="INFO"),
        ):
            results = run_each_feature(config_from_args(args), name_field="GEOID")

        self.assertEqual([item["name"] for item in results], ["001", "002", "001_2"])
        self.assertEqual([item["status"] for item in results], ["ok", "failed", "ok"])
        summary = json.loads((self.root / "out" / "batch_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["overture_release"], "2026-09-23.1")

    def test_fuse_heights_maps_keyword_options_to_config(self) -> None:
        with mock.patch("height_fusion_pipeline.pipeline.run_pipeline", return_value="out.tif") as run:
            fuse_heights("out.tif", bbox=(0, 0, 1, 1), overture_release="2026-09-23.1", fusion_mode="building_priority")
        config = run.call_args.args[0]
        self.assertEqual(config.overture.release, "2026-09-23.1")
        self.assertEqual(config.processing.fusion_mode, "building_priority")
        self.assertEqual(config.input.bbox, (0, 0, 1, 1))
        with self.assertRaises(TypeError):
            fuse_heights("out.tif", bbox=(0, 0, 1, 1), not_an_option=1)


if __name__ == "__main__":
    unittest.main()

# TreeBuilding2Tiff (height-fusion)

[中文](#中文) · [English](#english) · [Status memo](https://dogzzl1xy.github.io/TreeBuilding2Tiff/)

---

## 中文

把 Meta 全球树冠高度栅格和 Overture 建筑高度合成为一张单波段高度 GeoTIFF。适合在缺少 LiDAR 的地区（例如美国部分 county subdivision 和 Global South 城市）快速得到地表高度底图。

### 安装

需要 Python 3.10 及以上和 [uv](https://docs.astral.sh/uv/)。数据源都是公开 AWS 数据，读取时不需要凭证；只有把结果写到 S3 时才需要 AWS 写权限。

作为命令行工具安装（推荐）：

```bash
uv tool install git+https://github.com/DOGZzl1xy/TreeBuilding2Tiff
```

作为依赖加入其他项目：

```bash
uv add git+https://github.com/DOGZzl1xy/TreeBuilding2Tiff
```

参与开发：

```bash
git clone https://github.com/DOGZzl1xy/TreeBuilding2Tiff && cd TreeBuilding2Tiff && uv sync
```

### 命令行用法

每次运行为一个范围输出一张 GeoTIFF，`--output` 可以是本地路径或 `s3://` 地址：

```bash
height-fusion --bbox -85.8889 42.3538 -85.8626 42.3683 --output outputs/gobles_mi/fused_height.tif
```

```bash
height-fusion --boundary-geojson town.geojson --output s3://my-bucket/town/fused_height.tif
```

`--boundary-geojson` 可以是本地文件、S3 地址或内联 GeoJSON。FeatureCollection 中的所有要素会合并成一个范围。

批量处理时，GeoJSON 里每个要素输出一张 GeoTIFF，`--output` 是输出目录（或 S3 前缀）：

```bash
height-fusion --boundary-geojson towns.geojson --each-feature --name-field GEOID --output outputs/towns
```

文件按 `--name-field` 的值命名（不指定时用要素序号）。某个要素失败不会中断其他要素，结果汇总在 `outputs/towns/batch_summary.json`。

### Python 用法

```python
from height_fusion_pipeline import fuse_heights

fuse_heights(
    "outputs/gobles_mi/fused_height.tif",
    bbox=(-85.8889, 42.3538, -85.8626, 42.3683),
    fusion_mode="max",
    overture_release="2026-09-23.1",
)
```

其他参数和配置字段同名，例如 `chunk_size`、`meters_per_floor`、`default_building_height_m`、`tile_index_geojson`、`cache_dir`。

### 在 QGIS 中核对

加上 `--debug-dir DIR` 会额外写出：

| 文件 | 内容 |
| --- | --- |
| `request_boundary.geojson` | 请求范围 |
| `buildings.geojson` | Overture 建筑及使用的高度 `height_m` |
| `canopy_only.tif` | 只有树冠、与结果同一网格的栅格 |
| `metadata.json` | 使用的 tile、目标网格、建筑数量 |

检查要点：结果与 `canopy_only.tif` 范围一致；建筑处高于周边树冠；没有建筑的地方保持树冠高度。

### 常用参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--fusion-mode` | `max` | `max` 取树冠和建筑中较高者；`building_priority` 在有建筑的位置使用建筑高度 |
| `--overture-release` | `latest` | 可以固定版本，例如 `2026-09-23.1`，便于复现 |
| `--default-building-height` | `4.0` | 既没有高度也没有层数时使用的建筑高度（米） |
| `--meters-per-floor` | `3.0` | 由层数估算高度时每层的米数 |
| `--chunk-size` | `2048` | 分块大小，内存紧张时改为 `1024` |
| `--all-touched` | 关闭 | 栅格化时包含建筑边线接触到的像元 |
| `--output-nodata` / `--preserve-empty-as-nodata` | `0.0` / 关闭 | 树冠和建筑都没有时写 nodata |
| `--canopy-tile-index` | Meta 官方 index | 本地或 S3 上的 tile index |
| `--cache-dir` | `~/.cache/height_fusion` | tile index 缓存位置 |

完整列表见 `height-fusion --help`。

### 工作流程

1. 读取请求范围（bbox 或 GeoJSON）。
2. 用 DuckDB 按范围查询 Overture 建筑。建筑高度依次取 `height`、`num_floors × meters_per_floor`、默认高度。
3. 通过 Meta 的 tile index 找到相交的树冠 tile，以树冠原生网格和坐标系建立对齐的目标网格。
4. 分块读取树冠、栅格化建筑并融合，再按请求范围裁掉范围外的像元。
5. 写出 deflate 压缩的分块 GeoTIFF，保存到本地或上传到 S3。上传失败时会保留本地临时文件并在日志中给出路径。

数据来源：Overture Buildings（`s3://overturemaps-us-west-2/release/`），Meta Global Canopy Height（`s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/`）。

### 已知限制

- Overture 查询每个范围约需 60 到 80 秒，批量处理时会逐个查询。
- 请求范围跨越坐标系不同的树冠 tile 时会报错，需要拆分范围。
- 城市级以上的范围建议拆分成多块运行。

### 开发

```bash
uv run ruff check src tests
uv run python -m unittest discover -s tests
uv build
```

单元测试使用合成数据，不需要联网。联网测试（2026-10-03，Overture `2026-09-23.1`）选用 LiDAR 覆盖分析中两个数据源都显示为 0% 现代覆盖的密歇根州 Van Buren 县小城：Gobles city 的 bbox `-85.8889 42.3538 -85.8626 42.3683` 命中 1 个树冠 tile，生成 2452 × 1830 网格（约 1.2 米像元），500 栋建筑中 35 栋使用估算高度；Gobles 和 Hartford 两个镇界的批量运行各约 75 到 85 秒，分别包含 468 和 1,172 栋建筑。

---

## English

Combines Meta's Global Canopy Height rasters and Overture building heights into one single-band height GeoTIFF. Use it to get a surface-height layer quickly in places without good LiDAR, such as some U.S. county subdivisions and cities in the Global South.

### Install

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/). All inputs are public AWS data, so reading them needs no credentials. You only need AWS write access if you send output to S3.

As a command-line tool (recommended):

```bash
uv tool install git+https://github.com/DOGZzl1xy/TreeBuilding2Tiff
```

As a dependency of another project:

```bash
uv add git+https://github.com/DOGZzl1xy/TreeBuilding2Tiff
```

For development:

```bash
git clone https://github.com/DOGZzl1xy/TreeBuilding2Tiff && cd TreeBuilding2Tiff && uv sync
```

### Command line

Each run writes one GeoTIFF for one area. `--output` can be a local path or an `s3://` URI:

```bash
height-fusion --bbox -85.8889 42.3538 -85.8626 42.3683 --output outputs/gobles_mi/fused_height.tif
```

```bash
height-fusion --boundary-geojson town.geojson --output s3://my-bucket/town/fused_height.tif
```

`--boundary-geojson` accepts a local file, an S3 URI, or inline GeoJSON. All features in a FeatureCollection are merged into one area.

For batches, `--each-feature` writes one GeoTIFF per feature, and `--output` becomes a directory (or S3 prefix):

```bash
height-fusion --boundary-geojson towns.geojson --each-feature --name-field GEOID --output outputs/towns
```

Each file is named after the feature's `--name-field` value, or its index if no field is given. If one feature fails, the rest still run, and `outputs/towns/batch_summary.json` lists the result for each.

### Python

```python
from height_fusion_pipeline import fuse_heights

fuse_heights(
    "outputs/gobles_mi/fused_height.tif",
    bbox=(-85.8889, 42.3538, -85.8626, 42.3683),
    fusion_mode="max",
    overture_release="2026-09-23.1",
)
```

Other options take the same names as the config fields, for example `chunk_size`, `meters_per_floor`, `default_building_height_m`, `tile_index_geojson`, and `cache_dir`.

### Checking results in QGIS

`--debug-dir DIR` also writes:

| File | Content |
| --- | --- |
| `request_boundary.geojson` | Requested area |
| `buildings.geojson` | Overture buildings with the `height_m` used |
| `canopy_only.tif` | Canopy only, on the same grid as the output |
| `metadata.json` | Tiles used, target grid, building count |

Check that the output and `canopy_only.tif` share the same extent, that buildings stand above the surrounding canopy, and that areas without buildings keep the canopy height.

### Common options

| Option | Default | Meaning |
| --- | --- | --- |
| `--fusion-mode` | `max` | `max` keeps the taller of canopy and building; `building_priority` uses the building height wherever there is a building |
| `--overture-release` | `latest` | Pin a release such as `2026-09-23.1` for reproducible runs |
| `--default-building-height` | `4.0` | Height in meters when a building has neither height nor floor count |
| `--meters-per-floor` | `3.0` | Meters per floor when estimating height from floor count |
| `--chunk-size` | `2048` | Processing block size; use `1024` if memory is tight |
| `--all-touched` | off | Rasterize every pixel a building outline touches |
| `--output-nodata` / `--preserve-empty-as-nodata` | `0.0` / off | Write nodata where there is neither canopy nor building |
| `--canopy-tile-index` | Meta's index | Local or S3 tile index |
| `--cache-dir` | `~/.cache/height_fusion` | Where the tile index is cached |

Run `height-fusion --help` for the full list.

### How it works

1. Read the requested area (bbox or GeoJSON).
2. Query Overture buildings for the area with DuckDB. Building height comes from `height`, then `num_floors × meters_per_floor`, then the default height.
3. Find intersecting canopy tiles through Meta's tile index and build a target grid aligned to the canopy's native grid and CRS.
4. Read canopy in blocks, rasterize buildings onto the same grid, fuse, and mask pixels outside the requested area.
5. Write a deflate-compressed, tiled GeoTIFF locally or to S3. If an S3 upload fails, the local file is kept and its path is logged.

Sources: Overture Buildings (`s3://overturemaps-us-west-2/release/`) and Meta Global Canopy Height (`s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/`).

### Limitations

- Each Overture query takes about 60 to 80 seconds, and batches query one feature at a time.
- An area that spans canopy tiles with different CRSs fails and has to be split.
- Split areas larger than a city into several runs.

### Development

```bash
uv run ruff check src tests
uv run python -m unittest discover -s tests
uv build
```

The unit tests use synthetic data and run offline. The live tests (2026-10-03, Overture `2026-09-23.1`) use two small cities in Van Buren County, Michigan, which have 0% modern LiDAR coverage in both sources of the LiDAR coverage analysis. The Gobles city bbox `-85.8889 42.3538 -85.8626 42.3683` hit 1 canopy tile and produced a 2452 × 1830 grid (about 1.2 m pixels); 35 of its 500 buildings needed an estimated height. A batch over the Gobles and Hartford town boundaries took about 75 to 85 seconds per town and covered 468 and 1,172 buildings.

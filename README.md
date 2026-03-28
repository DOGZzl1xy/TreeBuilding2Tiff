# Global Height Fusion Pipeline

将 Meta Global Canopy Height 栅格与 Overture Buildings 矢量融合为统一高度 GeoTIFF 的 Python 工具集。

当前仓库包含两条使用路径：

- 主流程 CLI：直接从公开 AWS 数据源读取，输出上传到 S3
- 本地验证脚本：下载小范围样本到本地，导出 `buildings.geojson`、`canopy_only.tif`、`fused_height.tif`，便于在 QGIS 中核对

## 0. 目录结构

```text
.
├─ src/height_fusion_pipeline/
├─ scripts/
├─ requirements.txt
├─ pyproject.toml
└─ README.md
```

## 1. 仓库内容

核心代码位于 `src/height_fusion_pipeline/`：

- `cli.py`：命令行入口
- `vector.py`：从 Overture Buildings 读取并估算建筑高度
- `raster.py`：树冠瓦片发现、网格构建、栅格融合
- `pipeline.py`：主流程编排
- `config.py`：配置数据结构

本地验证脚本位于 `scripts/local_validation.py`。

## 2. 数据来源

- Overture Buildings：`s3://overturemaps-us-west-2/`
- Meta Canopy Height：`s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/`
- Meta canopy 索引：`s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/tiles.geojson`

实现细节：

- `latest` 的 Overture release 会优先从 Overture S3 的 `release/` 前缀解析
- Meta `tiles.geojson` 中的 `tile` 字段会映射为 `chm/<tile>.tif`
- 缺失建筑高度时，优先使用 `num_floors * meters_per_floor`，再回退到 `default_building_height`

## 3. 环境要求

- Python `>= 3.10`
- 推荐使用 Conda 环境，尤其是在 Windows 上安装 `rasterio / geopandas / pyproj`
- 需要能访问公开 AWS 数据源
- 如果运行主流程，需要具备写入目标 S3 的 AWS 凭证

推荐安装方式：

```powershell
conda activate TreeBuilding2Tiff
pip install -r requirements.txt
pip install -e .
```

如果 `rasterio` 等地理库在 Windows 下安装不稳定，优先使用 `conda-forge`。

DuckDB 首次运行会尝试安装并加载：

- `httpfs`
- `spatial`

## 4. 主流程 CLI 用法

主流程命令会：

1. 解析输入范围
2. 查询 Overture Buildings
3. 查找相交的 Meta canopy tile
4. 以 canopy 栅格为基准构建目标网格
5. 将建筑栅格化到完全一致的网格
6. 融合为单波段高度 GeoTIFF
7. 将结果上传到 S3

### 4.1 BBox 输入

```powershell
height-fusion ^
  --bbox 114.10 22.24 114.25 22.36 ^
  --output-s3 s3://my-output-bucket/hk/fused_height.tif ^
  --overture-release latest ^
  --canopy-prefix s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/ ^
  --canopy-tile-index s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/tiles.geojson ^
  --fusion-mode max ^
  --chunk-size 2048
```

### 4.2 GeoJSON 边界输入

```powershell
height-fusion ^
  --boundary-geojson C:\data\boundary.geojson ^
  --output-s3 s3://my-output-bucket/job/fused_height.tif ^
  --canopy-tile-index s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/tiles.geojson
```

### 4.3 常用参数

- `--bbox`: 输入范围，顺序为 `min_lon min_lat max_lon max_lat`
- `--boundary-geojson`: 本地路径、S3 URI 或内联 GeoJSON
- `--output-s3`: 输出 GeoTIFF 的 S3 URI
- `--overture-release`: `latest` 或固定 release，例如 `2026-03-18.0`
- `--overture-bucket`: Overture bucket，默认 `overturemaps-us-west-2`
- `--overture-region`: Overture 区域，默认 `us-west-2`
- `--canopy-prefix`: Meta canopy 根前缀
- `--canopy-region`: Meta canopy 区域，默认 `us-east-1`
- `--canopy-tile-index`: canopy 索引 GeoJSON；强烈建议提供
- `--max-scan-tiles`: 不使用索引时的扫描安全上限
- `--disable-full-prefix-scan`: 禁止在未提供索引时全桶扫描
- `--fusion-mode`: `max` 或 `building_priority`
- `--chunk-size`: 分块大小，默认 `2048`
- `--all-touched`: 建筑栅格化时启用 `all_touched`
- `--default-building-height`: 建筑默认高度，默认 `4.0`
- `--meters-per-floor`: `num_floors` 转米数倍率，默认 `3.0`
- `--output-nodata`: 输出 nodata 值，默认 `0.0`
- `--preserve-empty-as-nodata`: canopy 和 building 都为空时写出 nodata
- `--temp-dir`: 中间文件目录
- `--log-level`: 日志级别

## 5. 本地验证脚本用法

本地验证脚本适合做小范围 smoke test，或生成 QGIS 对照材料。

脚本会输出：

- `request_boundary.geojson`
- `buildings.geojson`
- `canopy_only.tif`
- `fused_height.tif`
- `metadata.json`
- `canopy_tiles/` 下的原始 canopy tile

### 5.1 命令示例

```powershell
conda activate TreeBuilding2Tiff
python scripts\local_validation.py ^
  --bbox 114.154 22.281 114.164 22.291 ^
  --out-dir artifacts\hk_local_test ^
  --overture-release latest ^
  --canopy-tile-index s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/tiles.geojson ^
  --log-level INFO
```

### 5.2 输出目录说明

运行本地验证脚本后，输出会写到你指定的 `artifacts/<name>/` 目录，例如 `artifacts/hk_local_test_v5/`：

- `request_boundary.geojson`：请求边界
- `buildings.geojson`：查询到的建筑物
- `canopy_only.tif`：仅 canopy 的裁剪结果
- `fused_height.tif`：融合结果
- `metadata.json`：命中 tile、栅格范围、建筑数量
- `canopy_tiles/132122232.tif`：下载到本地的原始 canopy tile

### 5.3 QGIS 对照建议

在 QGIS 中加载以下图层进行核对：

1. `request_boundary.geojson`
2. `buildings.geojson`
3. `canopy_only.tif`
4. `fused_height.tif`

建议检查：

- `fused_height.tif` 与 `canopy_only.tif` 的范围是否一致
- 建筑覆盖区域是否明显高于树冠底图
- 未命中建筑的位置是否保持 canopy 高度

## 6. 当前已验证样本

已完成一次本地样本验证，参数如下：

- BBox：`114.154, 22.281, 114.164, 22.291`
- Overture release：`2026-03-18.0`
- canopy tile 数量：`1`
- 目标输出网格：`934 x 1008`
- building 数量：`448`

该样本是在本地环境生成的验证结果，不应作为仓库内容提交。

## 7. 已知限制

- 主流程当前只支持输出到 S3，不支持直接从 CLI 写本地 GeoTIFF
- Overture 查询基于公开 parquet 全量文件，虽然带 bbox 过滤，但首次查询仍可能较慢
- 大区域任务如果不提供 `--canopy-tile-index`，扫描成本会明显上升
- 如果请求范围跨多个不兼容 CRS 的 canopy tile，当前实现会直接报错，需拆分任务

## 8. 生产建议

- 大范围任务必须提供 `--canopy-tile-index`
- 在 AWS Batch 上建议将 `chunk-size` 调整到 `1024` 或 `1536` 控制内存峰值
- 城市级以上任务建议分块运行，而不是一次性跨大范围处理
- 将本地验证脚本作为数据接入和参数验证的前置步骤，再上线主流程

# MODIS 研究側インターフェース仕様（Biomass_Burning_Sentinel-2 連携）

このダウンローダーの責務は **衛星原データ（HDF）の取得** と、研究側が使う **反射率・QA画像の生成** まで。
教師生成・AF4・patch生成・学習は研究側の責務で、このリポジトリでは実装しない。

コード内に研究側の絶対パスは固定していない。すべてのデータルートは引数（または環境変数）で渡す。

## 1. 入力：地域・日付・プロダクト

| 項目 | 値 |
|---|---|
| 取得計画の正本 | `src/modis_plan.py` の `RUN2_DOWNLOAD_PLAN`（218取得 = Aqua 111 + Terra 107、7地域 region01/03/05/06/07/08/09、2024-02-08〜04-10）。`tests/test_run2.py` が SHA-256 で固定 |
| プロダクト | Terra `MOD09GA`、Aqua `MYD09GA`、Collection `061`、日次 L2G 500 m タイル |
| タイル | `h28v05`、`h29v05`（地域・日付により1〜2枚） |
| 取得単位 | 1取得 = (region, platform, date)。1取得が複数granuleを使う場合あり（88 unique granule） |

研究側の正式画像（2026-10-26 に `authoritative_halo_v2/.../surface_reflectance_halo/` から `Aso/MODIS/<region>/2024/surface_reflectance/` へ内容そのままで移行）の 218取得は `RUN2_DOWNLOAD_PLAN` と**完全に同じ集合**（2026-10-26照合）。

取得ごとの source granule（処理時刻込みの完全なファイル名）と native 格子（transform・shape）、
granule ごとの完全一致 URL は `config/modis_run2_acquisitions.json` に固定してある
（正式画像のサイドカーと NASA CMR の完全名検索から1回だけ生成。研究側台帳
`ACQUISITIONS_AND_GRANULES.json` の 218取得×granule と一致、研究側 `MISSING_HDF_DOWNLOAD_MANIFEST.csv` の 25件の URL とも一致）。
読み込み時に `RUN2_DOWNLOAD_PLAN` と完全一致しなければエラーになる。

## 2. 出力パス構造（研究側正式画像と同一規約）

```
<root>/<region>/<YYYY>/surface_reflectance/<platform>/500m/<P>_<platform>_<YYYYMMDD>_500m.tif (+ .json)
<root>/<region>/<YYYY>/surface_reflectance/<platform>/qa/<P>_<platform>_<YYYYMMDD>_QC_500m.tif (+ .json)
<root>/<region>/<YYYY>/surface_reflectance/<platform>/qa/<P>_<platform>_<YYYYMMDD>_state_1km.tif (+ .json)
```

`<P>` は `MOD09GA`（terra）/ `MYD09GA`（aqua）。研究側 `source_discovery.discover_observations()` は
`.../surface_reflectance/<platform>/500m/*.tif` を読む（`surface_reflectance_halo/` は旧配置）。

`run2.py --reproduce` はこの構造で `--output-root` 直下に書く。

## 3. バンド構成・物理量・スケール

| ファイル | バンド | dtype | 値 | NoData |
|---|---|---|---|---|
| `_500m.tif` | 7: `sur_refl_b01`〜`b07` | float32 | 反射率 = DN × `scale_factor`(0.0001) + `add_offset`(0)。HDF属性から読む | NaN（HDF `_FillValue` −28672 の画素のみ） |
| `_QC_500m.tif` | 1: `QC_500m` | uint32 | HDF の生値（scale なし） | 未設定 |
| `_state_1km.tif` | 1: `state_1km` | uint16 | HDF の生値（scale なし）、native 1 km 格子 | 未設定 |

QA のビット解釈（clear 判定など）は研究側の既存 reader の責務。ダウンローダーは生値を保持するのみ。

Aqua の `sur_refl_b06` は MODIS Aqua Band 6 検出器の既知の故障により欠測（NaN）が多い。これはセンサー由来の欠測で、AOI マスクによるものではない。

## 4. CRS・transform・格子

- CRS：MODIS Sinusoidal（球 R = 6371007.181 m、`+proj=sinu +lon_0=0`）
- 画素サイズ：500 m 系 463.3127165 m、1 km 系 926.6254331 m
- グローバル原点：x = −20015109.354、y = 10007554.677、タイル 1111950.5197 m
- 再投影・リサンプリングなし。複数タイルは native 格子上で mosaic のみ
- 正式画像の窓（2026-10-27 から）は **AOI 全体 + halo**：AOI bbox（辺を密に投影）+ halo（通常 10 km、region01 は 13 km）を
  native 500 m 格子へ外側にスナップした矩形（例 region05 は 84×209）。地域ごとに日付・Terra/Aqua によらず 1 つの固定窓。
  `config/modis_run2_full_scene_manifest.json`（研究側が採用した manifest のバイトコピー、SHA-256 `95100815…a861`）に固定。
  `state_1km` は 500 m 窓を 1 km 格子へ外側に snap した窓（例 42×105）
- 旧正式画像（〜2026-10-26）の窓は学習パッチ群の外接矩形（`config/modis_run2_acquisitions.json` の grids、例 32×33）。
  `--legacy-patch-extent` でのみ再現する

## 5. 生成方法（旧 run2.py との違い）

| | 旧 run2.py（削除済み） | 現 run2.py ＝ 正式画像 `Aso/MODIS/<region>/2024/surface_reflectance/` |
|---|---|---|
| 切り出し範囲 | AOI bbox + 10 km（region01 は 13 km）を native に外側 snap（例 63×138） | 同じ AOI + halo 窓（固定 manifest）を整数オフセットで切り出し。旧パッチ範囲は `--legacy-patch-extent` |
| AOI ポリゴンマスク | **あり**：AOI 外を NaN 化 | **なし**：矩形内の原観測値をすべて保持 |
| QA nodata タグ | QC_500m=787410671, state_1km=65535 | 未設定 |
| 原HDF | 処理後に削除 | 永続保存・削除しない |

再投影・リサンプリング・padding なし。窓が mosaic 外やサブピクセルずれになる場合はエラー（代用しない）。

## 6. 出力画像と原HDFの対応

- 各 `_500m.json` サイドカーの `source_granules` に、使った granule の **完全なファイル名**（処理時刻込み）を記録
- 研究側台帳：`Aso/MODIS/data_management/ACQUISITIONS_AND_GRANULES.json`（218取得 → granule）
- 再取得対象：研究側 `MISSING_HDF_DOWNLOAD_MANIFEST.csv`（25 granule）。`--research-manifest` で渡すと、全レコードが固定 manifest と一致することを確認したうえで取得範囲をその25件に絞る
- `--reproduce` の出力 TIFF タグに `source_granules` と `source_granule_urls` を記録

## 7. 原HDFの保存

- 保存先：`<RAW_HDF_ROOT>/recovered_modis_061/<exact-granule-filename>`（研究側 `SETTINGS.example.json` と同じ）
- `RAW_HDF_ROOT` は `--raw-root` または環境変数 `MODIS_RAW_HDF_ROOT` で指定（既定値なし）
- 既存ファイルは上書き・削除しない。検証に失敗した既存ファイルは `PRESENT_INVALID` として報告し残す
- `run2.py` はどのモードでも raw HDF を削除しない。`--existing-root` の既存HDFは読み取りのみ（`--download --copy-existing` で raw root へ複製可能、移動はしない）

## 8. Python 実行入口（すべて `run2.py`）

```bash
# (a) dry-run（既定）：88 granule と 218取得の状態を表示。ネットワーク・書き込みなし
python run2.py --raw-root <RAW_HDF_ROOT> [--existing-root <既存HDFの場所>]...

# (b) 不足HDFだけを完全一致URLから取得（.part → HDF4検証 → atomic rename、上書き・削除なし）
python run2.py --download --raw-root <RAW_HDF_ROOT> [--existing-root ...]     [--research-manifest <MISSING_HDF_DOWNLOAD_MANIFEST.csv>] [--copy-existing]

# (c) 原HDFが揃った取得の 500m / QC_500m / state_1km を正式窓（AOI + halo）で生成。
#     窓は <出力root>/full_scene_manifest.json に記録（既存出力・既存 manifest は上書きしない。ネットワークは使わない）
python run2.py --reproduce --raw-root <RAW_HDF_ROOT> [--existing-root ...] --output-root <出力root>

# (d) 出力を検査：仕様、AOI と halo の包含、地域ごとの固定格子。
#     --reference-root：正式画像と画素・SHA-256 の一致。--legacy-reference-root：旧パッチ範囲画像が新画像の内側にあり、
#     その全範囲で一致（transform / 全球画素位置で対応付け。shape や SHA の一致は要求しない）
python run2.py --verify --output-root <出力root> [--reference-root <Aso/MODIS>] [--legacy-reference-root <旧パッチ範囲画像>]

# (e) 旧パッチ範囲版（2026-10-26 までの正式画像）の再現と検証（明示的な旧仕様モード）
python run2.py --reproduce --legacy-patch-extent --raw-root <RAW_HDF_ROOT> --output-root <別の出力root>
python run2.py --verify --legacy-patch-extent --output-root <同上> [--reference-root <旧パッチ範囲画像>]
```

`--halo-m <m>` は正式窓ではなく、指定 halo で窓を再計算する非公式の試験用です（レポートの `extent` に明記されます）。

共通：`--regions region01 ...` で地域を限定。`--raw-root` は環境変数 `MODIS_RAW_HDF_ROOT`、
`--output-root` は `MODIS_OUTPUT_ROOT` でも指定可（既定値なし）。
Docker では `docker compose run --rm -v <host dir>:<container dir>:ro downloader python3 run2.py ...`。
Git Bash から絶対パス引数を渡す場合は `MSYS_NO_PATHCONV=1` を付ける。

manifest の再生成（保守用、通常不要）：`python -m src.modis_plan --build --reference-root <authoritative> --output <新規json>`

## 9. 研究側へ渡す manifest

`--download` は `<RAW_HDF_ROOT>/recovered_modis_061/_run2_reports/run2_download_<UTC>.json`、
`--reproduce` / `--verify` は `<出力root>/_run2_reports/run2_<mode>_<UTC>.json` を新規作成する。内容：

- granule ごと：status、保存先、サイズ、SHA-256、HDF 内部 identity、完全一致URL
- 取得ごと：status（READY / BLOCKED_MISSING_HDF / GENERATED / ALREADY_PRESENT / IDENTICAL …）、
  使用 granule と HDF パス、出力3ファイルのパスと SHA-256、（verify時）transform・dtype・nodata・画素一致

研究側はこれで「原HDF取得済み」「原HDFから生成済み」「正式画像と一致確認済み」を区別して記録できる。

## 10. 回収時の検証項目

1. manifest：granule ID が完全なファイル名、product/版061/日付/tile/platform/URL のファイル名と整合、重複なし
2. HDF：HDF4 として開ける、`CoreMetadata.0` の `LOCALGRANULEID` = ファイル名、`SHORTNAME`、`RANGEBEGINNINGDATE`、タイル番号が一致
3. SDS：`sur_refl_b01_1`〜`b07_1`・`QC_500m_1`（2400×2400）、`state_1km_1`（1200×1200）が存在
4. サイズ・SHA-256 を記録（元HDFの SHA は研究側に保存がないため、byte 一致の主張はしない）
5. 正式画像との一致は (d) で確認する

## 11. 正式窓（AOI 全体 + halo）の詳細

| 項目 | 内容 |
|---|---|
| 窓 | AOI bbox（辺を密に投影）を native 500 m 格子へ外側にスナップ + halo（通常 10 km、region01 は 13 km）。各辺 18 画素以上（実際は南北 21〜28、東西 48〜63 画素） |
| 固定性 | 地域ごとに 1 つの窓。日付・Terra/Aqua・学習パッチに依存しない |
| state_1km | 500 m 窓を 1 km 格子へ外側にスナップ（500 m 窓を包含） |
| 処理 | granule・タイル mosaic・整数オフセット切り出し、DN × scale、fill → NaN、QA の raw 値、NoData はすべて従来どおり。再投影・リサンプリング・AOI マスクなし |
| 固定 manifest | `config/modis_run2_full_scene_manifest.json`（取得ごとの 3 ファイルの transform・shape・bounds・全球画素窓、AOI 窓、確保した halo、旧パッチ窓の位置） |
| 出力 manifest | `--reproduce` が `<出力root>/full_scene_manifest.json` に同じ内容を書く（既存があれば窓が一致することを確認するだけで書き換えない） |
| TIFF / サイドカー | `spatial_export_mode=full_scene_aoi_halo`、region / platform / acquisition_date、product / product_version、source_granules（+ URL）、native_transform、`aoi_window_in_image`、`halo_achieved_px`、`global_pixel_origin_500m`、`aoi_polygon_mask_applied=False`、`raster_reprojected=False`、`resampling=none` |

**画素の対応付けは必ず transform で行ってください。** 画像左上からの相対位置は窓が変われば変わります。
画素 (row, col) の中心は x = c + (col + 0.5)·a、y = f + (row + 0.5)·e です。全球 MODIS Sinusoidal 500 m 格子での位置は
col_g = (x0 − (−20015109.354)) / 463.3127…、row_g = (10007554.677 − y0) / 463.3127… で、タグ `global_pixel_origin_500m` と manifest に記録しています。

## 12. 外部 TEST（`--scope test`）

2024 年 TRAIN/DEV の計画（`RUN2_DOWNLOAD_PLAN` 218 取得）とは別に、研究で評価済みの外部 TEST 3 ケースを同じ方法で生成します。

| 項目 | 内容 |
|---|---|
| 計画の正本 | `src/modis_plan.py` の `RUN2_TEST_CASES`（ケース・PRE・POST）と `RUN2_TEST_PLAN`（`{region: {aqua, terra: [日付]}}`、6 取得 = Aqua 2 + Terra 4） |
| ケース | region05 2023 Terra 2023-01-11→2023-03-08、region05 2025 Aqua 2025-02-14→2025-03-22、人吉 2025 Terra 2025-02-25→2025-03-20 |
| 根拠 | 研究側 `results/final_trainval_retraining/modis_native_residual_experiment/evaluation/external_eval.py` の CASES（`modis_burning/scripts/g5_external_contract_preflight.py` でも同じ組を確認）と既存 TEST 画像 `Aso/MODIS/{region05/2023,region05/2025,hitoyoshi/2025}` |
| granule・URL | `config/modis_run2_test_acquisitions.json`（既存 TEST 画像の sidecar `source_granules` から作成し、CMR で完全一致する URL を固定。10 granule、人吉は h29v05 のみ） |
| 窓 | `config/modis_run2_test_full_scene_manifest.json`。2024 年と同じ規則（AOI 全体 + 10 km halo）。region05 の窓は 2024 年の region05 と同一 |
| AOI | region05 = `config/no5.geojson`、人吉 = `config/hitoyoshi.geojson`（研究側 `Aso/PlanetScope/<region>/` と同一ファイル） |

既存の TEST 画像との関係：

- 既存 `Aso/MODIS/<region>/<2023|2025>/` は旧方式（`clip_to_aoi`、最小 bbox で切り抜き、AOI 外は NaN / fill）です。`--verify --scope test --legacy-reference-root <Aso/MODIS>` は transform で位置を合わせ、既存画像に値がある画素だけを比べます（`legacy_test_comparison`）。
- 人吉の既存画像は AOI に約 18〜20 km の buffer を付けて切り抜いたもので（89 × 196 px）、新しい窓（55 × 120 px）より大きいため、新しい窓の外の値は「比較範囲外」として件数だけ記録します。
- 研究側の Halo v2 再出力（`results/modis_test_halo_v2_reevaluation_20261017/data/`、AOI マスクなし、学習パッチ範囲）とも、重なり部分は NaN を含めて完全一致することを確認しています。人吉の Halo v2（90 × 196）は新しい窓を含む大きさなので、旧パッチ範囲のまま推論する場合は Halo v2 か、`--halo-m` による正式外の広い窓が必要です。

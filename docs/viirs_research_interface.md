# VIIRS 取得・出力仕様（run3.py、Biomass_Burning_Sentinel-2 連携）

`run3.py` は、研究で使う VIIRS 入力を作る正式な入口です（MODIS は `run2.py`、SGLI は `run4.py`）。
このリポジトリが受け持つのは **原データ（L2 swath HDF4 と geolocation NetCDF4）の取得・永続保存と、375 m / 750 m 解析 grid 画像の生成** までです。
Basic9 の指数、教師データ、AF4、patch の作成は研究側の仕事です。
コードには研究側の絶対パスを書いていません。データの置き場所はすべて引数か環境変数で渡します。

> 旧 Daily L2G（VNP09GA / VJ109GA / VJ209GA、約 463 m / 926 m 格子、AOI 外を NaN にする方式）は **研究の 375 m 入力とは別物** です。
> `run3.py` からは削除しました。`run.py` の `surface_reflectance.viirs_product: daily_l2g_legacy` では引き続き使えます。

## 1. 製品・版・platform

| platform | L2 SR（swath） | IMG geolocation（375 m） | MOD geolocation（750 m） | CMR collection |
|---|---|---|---|---|
| Suomi-NPP（`snpp`） | `VNP09` | `VNP03IMG` | `VNP03MOD` | SR 002 / geo 002 |
| NOAA-20（`noaa20`） | `VJ109` | `VJ103IMG` | `VJ103MOD` | SR 002 / geo 021 |
| NOAA-21（`noaa21`） | `VJ209` | `VJ203IMG` | `VJ203MOD` | SR 002 / geo 021 |

- SR は HDF4（HDF-EOS2）、geolocation は NetCDF4 です。
- SR と geolocation の対応は、SR の `InputPointer` に書かれた完全なファイル名で決めます。そのうえで orbit・開始時刻・終了時刻・shape の一致を確認します。版番号の一致では判定しません。
- `DayNightFlag=DAY` の granule だけを使います。夜間の granule は使わず、別の日や別の platform の granule で代用することもしません。

## 2. バンド・解像度・格子

| グループ（ファイル） | 格子 | 内容 | dtype | NoData |
|---|---|---|---|---|
| `375m` | 375 m | I1, I2, I3 | float32 | NaN（mask 0） |
| `750m` | 750 m | M1, M2, M3, M4, M5, M7, M8, M10, M11 | float32 | NaN（mask 0） |
| `qa` | 750 m | QF1–QF7, land_water_mask（raw byte） | uint8 | mask 0 |
| `qa_375m` | 375 m | geolocation の land_water_mask | uint8 | mask 0 |
| `geometry_375m` / `_750m` | 375 / 750 m | sensor/solar zenith/azimuth（度） | float32 | NaN |
| `provenance_375m` / `_750m` | 375 / 750 m | source_granule, source_row, source_col, scan_time_unix, source_distance_m（daily のみ selected_orbit を追加） | float64 | NaN |

- **CRS**: `+proj=sinu +lon_0=0 +R=6371007.181`（MODIS Land と同じ CRS）。セルは独自の 375 m / 750 m で、NASA の 500 m / 1 km L2G grid ではありません。
- **原点**: x0 = −20015109.354、y0 = 10007554.677 に固定します。窓の端はすべて 750 m の整数倍です。750 m の 1 cell は 375 m の 2×2 cell と厳密に重なります。
- **窓**: AOI（`config/noN.geojson`。研究側の `PlanetScope/regionNN/noN.geojson` とバイト単位で同一）の bbox に **halo 13 km** を足し、外側へ 750 m 単位でスナップします。
  13 km は研究側 `configs/sensors/viirs_pipeline*.json` の `halo_m` と同じ値で、全リージョン共通です。`--halo-m` で変更できます。
- **配置方法**: swath の画素中心から grid のセル中心へ、WGS84 地表距離で最近傍を取ります（3 次元 KD-tree）。
  - 採用するのは、距離がその画素の局所 footprint の半対角以内のときだけです。
  - bow-tie で削除された画素は source から除外します。
  - 同じ orbit の granule は 1 本にまとめて（mosaic して）から配置します。
- **750 m → 375 m の 2×2 複製はしません**（研究側の前処理で行います）。I と M を別の格子に分けて保持します。

## 3. 反射率・QA

- 反射率 = DN × scale_factor（9.9999997e-05）+ add_offset（0）です。HDF の属性値と監査済みの値が一致することを確認します。`_FillValue` −28672 は NaN、valid_range は [−100, 16000] です。
- QF1–QF7 と land_water_mask は raw の整数のまま保持します。雲の画素も NaN にはしません。bit の定義は `docs/viirs_l2_swath_smoke_test.md` 3.2 節にあります。
- QF6 の bit 定義には原文の誤記があり、使用しません（研究側も同じ扱いです）。

## 4. overpass と日次

- `overpass/`：同じ日の platform ごとに、AOI を観測した orbit ごとに 1 組を出力します（`AOI_OUTSIDE_SWATH` の orbit は出力しません）。
- `daily/`：overpass から日次で最良の観測を選んだ派生物です。平均や合成はせず、750 m cell ごとに次の順で 1 つの overpass を選びます。
  1. 全 M バンドが有効
  2. QF1 の雲の信頼度が低い
  3. QF2 で影がない
  4. センサー天頂角が小さい
  5. 時刻が早い

  375 m cell は親の 750 m cell と同じ overpass を使います。
- 観測時刻: cell ごとに `scan_time_unix` を、ファイルごとにタグ `acquisition_start_utc` / `acquisition_end_utc` を記録します。出典 granule はタグ `source_granules` と `geolocation_granules` に記録します。

## 5. 取得計画（PRE/POST 対象）

- 取得計画の正本は MODIS（`src/modis_plan.py` の `RUN2_DOWNLOAD_PLAN`）と同じ方式で、`src/viirs_plan.py` の `RUN3_DOWNLOAD_PLAN`（`{region: {snpp, noaa20, noaa21: [日付…]}}`）です。141 region-date を 3 platform すべてで取得するので計 423 タスクです。範囲や統計から日付を推定することはしません。
  - 日付は `config/viirs_required_dates_daily.csv`（研究側 `viirs_burning/deep_learning/data/manifests/viirs_required_dates_daily.csv` とバイト単位で同一）からそのまま移植しました。CSV は正本ではなく照合用の記録として変更せずに残し、`tests/test_run3.py`・`tests/test_required_dates_csv.py` が Python の計画と完全一致することを確認します。
  - `config/viirs_run3_acquisitions.json`（固定 manifest）は読み込み時に `RUN3_DOWNLOAD_PLAN` の 423 タスクと順序まで完全一致しなければエラーになります。config.yaml には依存しません。
- 研究側との照合（2026-10-26）:
  - `viirs_burning/results/manifests/daily_pair_manifest.csv` の PRE/POST 日付 127 件（7 リージョン、3 platform、9,248 ペア）はすべて計画に含まれます。
  - 残りの 14 件は region04 で、学習では使っていません。
  - L2 swath の検証ペア（attempt02、4 ペア 7 シーン）もすべて計画内です。
- MODIS の 218 取得は流用していません。
- 凍結 manifest は `config/viirs_run3_acquisitions.json` です。タスクごとに、DAY の SR granule、orbit、IMG/MOD のペアを記録しています。
  - 416 タスクは granule あり、7 タスクは `NO_GRANULE` です（NOAA-21 2024-02-27 × 7 リージョン。取得時の CMR に SR granule がありませんでした）。
  - 重複を除いた原ファイルは 330 本です。
  - 各タスクの granule 集合は、raw ディレクトリにある DAY SR の集合と一致することを、build 時に検査しています。研究側の `raw_day()` が読むのも同じ集合です。

## 6. 原データの保存と再利用

- 保存先: `<VIIRS_L2_RAW_ROOT>/<product>/<YYYY>/<DOY>/<file>`（`src/viirs_l2.raw_path`）。研究側 `raw_day()` が読む構成と同じです。
- 既存の原データ（`data/aoi_rectangle/output_viirs_l2_swath_selective/<region>/viirs/_raw_l2/`、リージョンごとに重複を含む 1,827 本・287 GB）は、`--existing-root '<...>/{region}/viirs/_raw_l2'` を指定すれば読み取り専用で再利用できます。
  `--download --copy-existing` を付けると raw root へ複製します（移動はしません）。
- `--download` で取得するのは不足しているファイルだけです。CMR で完全なファイル名を検索し、`.part` に保存 → Content-Length と HDF4/HDF5 シグネチャを確認 → rename、という手順を踏みます。取得後には header（DayNight・pairing・orbit/時刻）を検証します。
  上書きも削除もしません。

## 7. 出力構造

正式配置（2026-10-27〜。研究側 `VIIRS/regionXX/2024/surface_reflectance/` と同じ構造）:

```
<out>/<region>/<YYYY>/surface_reflectance/<platform>/
    {375m,750m,qa,geometry,provenance}/<SR>_<platform>_<YYYYMMDD>_daily_<group>.tif   (日次 best observation のみ)
    summary/<SR>_<platform>_<YYYYMMDD>.json      (全 overpass の granule・geolocation・時刻・AOI 状態・統計)
<out>/_run3_reports/run3_<mode>_<UTC>.json        (タスクごとの状態・原ファイル・出力の対応)
```

- overpass ごとの画像は中間生成物として既定では書き出しません（daily の provenance 層 `selected_orbit`・`scan_time_unix` と summary に、使った overpass・時刻・granule が残ります）。必要なら `--keep-overpass` で `<platform>/overpass/...` に出力します。
- 窓は全リージョン共通で AOI bbox + 13 km（研究側 `halo_m`）、地域ごとに日付・platform によらず固定です。
- 取得対象は既定で研究の PRE/POST に使われる 344 タスク（`src/viirs_plan.py` の `RUN3_RESEARCH_TASKS`。照合用の記録 `config/viirs_research_required_tasks.csv` と完全一致。研究側 `daily_pair_manifest.csv` 9,248 ペアの PRE/POST 日付 × platform、region04 は含まない）。`--all-plan-tasks` で計画の 423 タスク全部。

## 8. コマンド

```bash
python run3.py --raw-root <RAW> [--existing-root '<old>/{region}/viirs/_raw_l2'] [--verify-raw]   # dry-run
python run3.py --download --raw-root <RAW> [--existing-root ...] [--copy-existing]
python run3.py --reproduce --raw-root <RAW> [--existing-root ...] --output-root <OUT> [--regions/--platforms/--dates]
python run3.py --verify --output-root <OUT> [--reference-root <別の出力>] [--research-scenes <dataset/scenes>]
```

計画の保守（通常は不要）: `python -m src.viirs_plan --build --acquisition-root <取得root> --output <新規json>`

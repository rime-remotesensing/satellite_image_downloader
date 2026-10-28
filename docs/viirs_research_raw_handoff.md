# 研究側 Codex への引き継ぎ：VIIRS 原データ保存先の切替と旧データ削除の条件（2026-10-27）

宛先：Biomass_Burning_Sentinel-2 の VIIRS L2 swath パイプライン（`src/burning/sensors/viirs/swath.py` / `pipeline.py`）の保守担当。
目的：原データの正規保存先を `satellite_image_downloader/data/viirs_l2_raw/` に一本化し、その後に旧 287 GB（地域別の重複原データ + 旧中間画像）と旧 L2G 画像を削除できる状態にすること。
downloader 側では、旧 287 GB と旧 L2G は**まだ変更していません**。

## 1. 新しい正規の原データ保存先

| 項目 | 値 |
|---|---|
| パス（ホスト） | `D:/sugimoto/satellite_image_downloader/data/viirs_l2_raw/` |
| 構造 | `<product>/<YYYY>/<DOY>/<granule>`（研究側 `swath.raw_path` と同じ） |
| 内容 | 330 ファイル（53 GB）。計画 423 タスクのうち granule がある 416 タスクの SR + IMG/MOD 全件（研究の 344 タスクは 309 ファイル） |
| product | SR（HDF4）：`VNP09` / `VJ109` / `VJ209`（collection 002）。geolocation（NetCDF4）：`VNP03IMG` / `VNP03MOD`（002）、`VJ103IMG` / `VJ103MOD` / `VJ203IMG` / `VJ203MOD`（021） |
| 完全性 | 旧ツリーの原データ 1,827 本（330 種）はすべて新保存先の同名ファイルと SHA-256 一致。研究側 `docs/viirs_pipeline_20261026/VIIRS_SOURCE_DATA_MANIFEST.csv` の原データ 27 行は SHA-256 が全件一致 |

旧パス `.../output_viirs_l2_swath_selective/<region>/viirs/_raw_l2/<product>/<YYYY>/<DOY>/<granule>` は、
ファイル名を保ったまま `data/viirs_l2_raw/<product>/<YYYY>/<DOY>/<granule>` に対応します（SHA-256 同一）。

## 2. 注意：granule の選び方（日付フォルダの全ファイルを読まないこと）

現在の `pipeline.raw_day()` は `raw_root/<SR>/<YYYY>/<DOY>/*.hdf` を**すべて**読み、`DayNightFlag=DAY` で絞り込みます。
旧保存先は地域ごとに分かれていたため、この方法で「その地域・日・platform の granule」だけを読めていました。
新保存先は全地域で共有なので、同じ日・platform の**他地域用 granule も同じフォルダにあります**。

実測では、研究の 344 タスク中 **6 タスク**で、日付フォルダに自タスク以外の SR granule が 1 本ずつ混ざります
（例：region01 noaa20 2024-03-08 の `VJ109.A2024068.0512…`、region01/03/05 snpp 2024-03-27 の `VNP09.A2024087.0506…`、
region03 noaa21 2024-04-10、region09 noaa21 2024-03-04）。
これが AOI を覆わない別 orbit なら除外されますが、同じ orbit の隣接 granule なら mosaic に加わり、窓の端の値が変わる可能性があります。

**正しい選び方**：granule は、凍結した取得表のタスク単位の一覧から取ってください。

- `satellite_image_downloader/config/viirs_run3_acquisitions.json` の `tasks[]`
  - `region`, `platform`, `date`, `status`（`GRANULES` / `NO_GRANULE`）
  - `overpasses[]`：`orbit`、`granules`（SR ファイル名、開始時刻順）、`geolocation`（各 SR の `[IMG, MOD]`、SR の InputPointer と同じ名前）
- 研究で使う 344 タスク：`satellite_image_downloader/config/viirs_research_required_tasks.csv`（`region,platform,date`。研究側 `daily_pair_manifest.csv` の PRE/POST × platform）
  - downloader 側の正本は `src/viirs_plan.py` の `RUN3_RESEARCH_TASKS`（取得計画全体は `RUN3_DOWNLOAD_PLAN`）。この CSV は内容を変えずに照合用として残しており、テストで正本と完全一致することを確認しています
- 選んだ SR だけについて、従来どおり DayNight 判定 → `pair_geolocation` → `verify_pair` → orbit ごとの mosaic → `AOI_OUTSIDE_SWATH` の除外 → 日次選択を行えば、旧保存先と同じ結果になります
  （downloader の `run3.py --reproduce` はこの方法で生成しており、attempt02 の 7 scene と画素単位で一致）

最小の改修案：`raw_day()` の `files=sorted(...glob('*.hdf'))` を、取得表の `granules` を `raw_root` 上のパスに直したリストに置き換え、
取得表にない SR があっても読まないようにする（`NO_GRANULE` のタスクは「観測なし」とし、別日・別 platform で代用しない）。

## 3. 代替：正式画像を直接読む

`Aso/VIIRS/regionXX/2024/surface_reflectance/<platform>/` の日次画像は `raw_day()` の結果と同一です。
pipeline が原データから毎回計算し直す必要がなければ、画像を直接読む方法もあります。

| ファイル | 内容 |
|---|---|
| `375m/<SR>_<platform>_<YYYYMMDD>_daily_375m.tif` | I1, I2, I3（float32、反射率、NaN = 観測なし） |
| `750m/…_daily_750m.tif` | M1, M2, M3, M4, M5, M7, M8, M10, M11 |
| `qa/…_daily_qa.tif` | QF1〜QF7, land_water_mask（750 m、uint8 生値）。`qa/…_daily_qa_375m.tif` は geolocation の land_water_mask（375 m） |
| `geometry/…`, `provenance/…` | 天頂角・方位角。source_granule・行列・`scan_time_unix`・`source_distance_m`・`selected_orbit` |
| `summary/<SR>_<platform>_<YYYYMMDD>.json` | 全 overpass の granule・geolocation・時刻・AOI 状態 |

- 格子：MODIS Sinusoidal（R = 6371007.181）上の固定 375/750 m 格子。原点は x0 = −20015109.354、y0 = 10007554.677。窓は AOI bbox + 13 km（地域ごとに固定）
- Basic9 にするには、研究側で M と QF1/QF2 を 2×2 複製（`repeat_1km_to_500m` と同じ座標包含）し、NDVI_01 と NBR_01 を計算する
- 導入記録：`Aso/VIIRS/data_management/VIIRS_L2_SWATH_OFFICIAL_INSTALL_20261027.json`（全ファイルの SHA-256・タスク・granule）

## 4. 研究側で必要な変更

1. pair 設定の `raw_root`（`configs/sensors/viirs_pipeline*.json` と local 設定。attempt02 では `/inputs/viirs_exports/<region>/viirs/_raw_l2`）を、
   新保存先（例：`/inputs/viirs_l2_raw`）に変える。
2. 2 節のとおり、granule を取得表から選ぶようにする（6 タスクの混入を防ぐため必須）。
3. `VIIRS_SOURCE_DATA_MANIFEST.csv` 等の出所記録の旧パスは、同名・同 SHA-256 の新パスに対応づけて更新する。
4. 旧 L2G を読むコード（`viirs_burning/src/data/flex_scene_reader.py`、`viirs_burning/src/data/source.py` の `surface_reflectance/snpp/500m/VNP09GA_snpp_*_500m.tif` 検索、
   `surface_reflectance_halo/` を読む `source.py` / `manifests.py`）は、375 m L2 swath へ移行するか、廃止する。
   旧 L2G は `Aso/VIIRS/regionXX/2024/surface_reflectance_l2g_legacy/` に退避済み（内容は変更なし、記録は `VIIRS_L2G_LEGACY_MOVE_20261027.json`）。

## 5. 旧データを削除する条件（研究側で確認後、downloader 側で削除）

| 削除対象 | 容量 | 削除してよい条件 |
|---|---|---|
| `satellite_image_downloader/data/aoi_rectangle/output_viirs_l2_swath_selective/`（原データ 1,827 本 + 旧中間画像 7,832 TIFF / 417 JSON） | 287 GB | 4 節の 1〜3 が完了し、新 `raw_root` で attempt02 の 7 scene（または現行の確認用 scene）が画素一致すること |
| `Aso/VIIRS/regionXX/2024/surface_reflectance_l2g_legacy/`（10 地域 4,200 ファイル） | — | 4 節の 4 が完了し、旧 L2G を読むコードがなくなること |
| `Aso/VIIRS/regionXX/2024/surface_reflectance_halo/`（旧 L2G halo） | — | 同上（`viirs_burning` の L2G 系 pipeline を廃止した場合） |

再生成手段は残ります。原データ（`data/viirs_l2_raw/`）、取得表（`config/viirs_run3_acquisitions.json`）、
研究タスク一覧（`src/viirs_plan.py` の `RUN3_RESEARCH_TASKS`、照合用 `config/viirs_research_required_tasks.csv`）から、`python run3.py --reproduce --raw-root <raw> --output-root <新規>` で
正式画像 344 タスクを再生成できます（`--all-plan-tasks` で 423 タスク）。

## 6. downloader 側での読み取り専用検証結果（2026-10-27、研究側コードは無変更）

検証データ：`satellite_image_downloader/data/viirs_research_migration_check/`
（`GRANULE_SELECTION_344.json`、`raw_day_runs.json`、`RAW_DAY_COMPARISON.json`、実行スクリプト 2 本）

**344 タスクの granule 選択**
- 取得表で選んだ SR granule は、旧地域別フォルダの中身と **344/344 で一致**。新保存先での不足は 0 件（SR 507 本、geolocation 1,014 本、重複を除いて 309 ファイル）
- 新保存先の日付フォルダを単純に glob すると、**6 タスクで SR granule が 1 本ずつ余分**に入る（2 節の 6 タスク）。足りなくなるタスクは 0 件

**研究側 `pipeline.raw_day()`（無変更）を新保存先で実行**（7 scene + 6 混入タスク = 12 ケース、halo 13000 m）
| 比較 | 結果 |
|---|---|
| 研究 scene.npz（attempt02 の 7 scene）との一致：Basic9 の全 9 チャネル（I1, I2, M3, M4, M8, I3, M11, NDVI_01, NBR_01）、valid、clear、aoi、transform、CRS、orbits、日付、shape | **7/7 完全一致**（granule 選択ビュー、単純 glob の両方） |
| 正式画像（`Aso/VIIRS/.../surface_reflectance`）の日次 375/750 m との一致（物理 7 バンド。M は 2×2 複製）と valid mask | **12/12 完全一致** |
| 単純 glob と granule 選択の比較 | 12/12 で画素・orbit とも同一 |

6 タスクで余分に入る granule は、今回の窓では画素に影響しませんでした（AOI を覆わず除外されるか、窓内に寄与しない）。
ただし、`raw_day()` の dependencies には **使っていない granule が SR_HDF4 として記録されます**
（例：region03 snpp 2024-03-27 の `VNP09.A2024087.0506…`）。出所記録を正しく保つため、また halo・地域を変えたときの混入を防ぐため、
2 節の取得表に基づく選択に変えることを推奨します。

**旧 L2G への依存（読み取り調査）**
- 旧 L2G（`surface_reflectance_l2g_legacy/`）と `surface_reflectance_halo/` を読むのは `viirs_burning/` 系だけです：
  `src/data/source.py`・`manifests.py`・`flex_scene_reader.py`・`contracts.py`、`deep_learning/src/data/build_dataset.py`・`qa_decode.py`、
  `scripts/preflight.py`・`run_independent_test_inference.py`・`region05_*`・`modis_hitoyoshi_*`・`modis_region05_*` の診断スクリプト。
  これらは旧 L2G の学習・推論と、`viirs_burning/results/test_final_models/` などの過去結果を再現するためのものです
- 現行の正式コード `src/burning/`（VIIRS は `sensors/viirs/`）は旧 L2G を読みません。研究 README でも VIIRS の 4 モデル学習は未完成
- 研究側テストの VIIRS 回帰（`tests/integration_migration.py` の「実 export 2 日」）は、保存済みの npz サンプルを読み、L2G 画像は読みません

## 7. 削除可否の判定（downloader 側の調査に基づく）

| 対象 | 代替手段 | 削除の前に必要なこと |
|---|---|---|
| 旧地域別原データ（約 287 GB） | 新保存先 330 ファイルで完全に代替できる（SHA 一致 1,827/1,827、研究の出所記録 27/27、7 scene 再現） | 研究側の設定・出所記録の `raw_root` を新保存先へ切り替えること（推奨：取得表に基づく granule 選択）。独自のデータは残らない（他は旧方式の中間出力のみ） |
| `surface_reflectance_l2g_legacy/` | なし（L2G は別製品） | `viirs_burning/` の旧 L2G 系の学習・推論・診断と過去結果の再現を廃止すると研究側が決めること |
| `surface_reflectance_halo/`（VIIRS） | なし（同上） | 同上 |

このディレクトリで作業をするうえでのガイダンスです．
## 言語対応

**重要:** このリポジトリで作業する際：
- **ユーザーへの応答はすべて日本語で行うこと**
- **思考・推論は英語で行うこと**

## 概要
1. Azure planetary computer stac APIを利用して衛星画像をダウンロードし，反射率の変換やリサンプリング，雲マスク等の前処理を施す．
対象とする衛星プラットフォームは以下の通りです．
    - Sentinel-2 Level-2A
    - Landsat 8 / 9 Collection 2 Level-2

2. NASA FIRMS APIを利用して，MODISとVIIRSのactivefireのデータをshpファイルでダウンロードする．

## ディレクトリの構成
```
/workspace/
├── output/                  # ダウンロード画像の出力先
├── config/                  # 設定ファイル
├── scripts/                 # 実行スクリプト
├── src/                     # ソースコードモジュール
└── env/                     # 環境設定
```

## 前処理
1. DN値から反射率に変換する必要がある．
2. リサンプリングをする必要があります．Sentinel-2は10m，Landsatは30mにすべてのバンドでリサンプリングする必要があります．
3. Sentinel-2, Landsat8,9の雲マスクは，リポジトリ内のクラウドマスク処理（omnicloudmaskパッケージ利用）を活用してください．
4. 同日の画像を最小値合成でコンポジットしてください．

## 入力
- ダウンロードしたい領域が格納されている```.geojson```を読み込んで，その範囲で画像やactivefireを検索する．
    - ```config/config.yml```で```.geojson```のパスを指定させる．
- ダウンロード期間も同様，```config/config.yml```で```startday:yyyymmdd, endday:yyyymmdd```を指定させ，その期間内で検索をかけすべてダウンロードする．
- ダウンロードする衛星プラットフォームを```config.yml```で```satellite:sentinel2,landsat89,modis,viirs````を指定させる．
- ```config.yml```でダウンロードするバンドを指定させる．```band:all```で全バンド（Sentinel2なら01-12, Landsatなら01-11(熱赤外も)）
- ```band:at```の場合は，ダウンロードするバンドを直接指定```num: [1,2,3]```みたいな，ただし，雲処理用にomnicloudmaskで必要なバンドだけはデフォルトでダウンロードするようにしてください．
- omnicloudmaskのマスクバンドを指定させ，```cloudmask: [1,3]```そのバンドだけでマスク処理を行ってコンポジットする．

## 出力
衛星画像は.tifにしてください．出力の形式は以下の通りです．
- Sentinel-2: ```output/sentinel2/raw/S2C_yyyymmdd_B01.tif```に生データ，前処理済みデータを```output/sentinel2/img/S2C_yyyymmdd_B01.tif```を格納．
omnicloudmask.tifも生データのところに出力してください．
- Landsat-8/9: ```output/landsat89/raw/L8C_yyyymmdd_B01.tif, L9C_yyyymmdd_B01.tif```に生データ，前処理済みデータを```output/landsat89/img/L8C_yyyymmdd_B01.tif,L9C_yyyymmdd_B01.tif```を格納．
omnicloudmask.tifも生データのところに出力してください．
- activefire: ```output/modis/activefire/ACFR_yyyymmdd_tttt.shp```，```output/viirs/activefire/ACFR_yyyymmdd_tttt.shp```
```ttt```はUTCで保存してください．

## このパイプラインの使い方
- userによってダウンロードする領域の種類（まとめて），複数の期間まとめてダウンロードしたりできるように基盤だけ構築し，運用が容易なようにしてください．出力先もカスタマイズしたりできるように，関数のようなものを組んで，そのpythonスクリプトをインポートすればループ処理など簡単にできるようにしたいです．
```python
satellite_image_downloader(satellite type, geojson Path, Sdate, Edate, output Path)
```
みたいな感じですかね．これを```run.py```内で編集できるようにして，```docker compose```で実行すれば，指定したダウンロード構成で，環境も立ち上げてくれる感じになると思います．
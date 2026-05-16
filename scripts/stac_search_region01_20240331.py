import json
from pathlib import Path
from pystac_client import Client
import planetary_computer

pc = Client.open('https://planetarycomputer.microsoft.com/api/stac/v1', modifier=planetary_computer.sign_inplace)
geojson_path = Path('config/no1.geojson')
geom = json.loads(geojson_path.read_text())
# If the file is a FeatureCollection, extract the first feature's geometry
if isinstance(geom, dict) and geom.get('type') == 'FeatureCollection':
    features = geom.get('features', [])
    if not features:
        raise SystemExit('GeoJSON has no features')
    geom = features[0].get('geometry')

date = '20240331'
# Convert YYYYMMDD -> YYYY-MM-DD for pystac_client
iso = f"{date[:4]}-{date[4:6]}-{date[6:]}"
search = pc.search(collections=['sentinel-2-l2a'], intersects=geom, datetime=f"{iso}/{iso}")
items = list(search.get_all_items())
print('Found', len(items), 'items')
for it in items:
    props = it.properties
    cloud = props.get('eo:cloud_cover')
    pb = props.get('s2:processing_baseline') or props.get('processing_baseline')
    print(it.id, 'cloud=', cloud, 'processing_baseline=', pb)

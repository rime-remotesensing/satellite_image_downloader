"""Study regions shared by the VIIRS (run3.py), SGLI (run4.py) and Active Fire (run_activefire.py) entry points.

The MODIS plan (src/modis_plan.py) keeps its own 7-region literal plan; region04
appears only here (VIIRS/SGLI/AF acquisition), not in the MODIS training plan.
The GeoJSONs are byte-identical to the research side's PlanetScope AOIs.
"""

from __future__ import annotations

from typing import Dict

REGION_GEOJSONS: Dict[str, str] = {
    "region01": "config/no1.geojson",
    "region03": "config/no3.geojson",
    "region04": "config/no4.geojson",
    "region05": "config/no5.geojson",
    "region06": "config/no6.geojson",
    "region07": "config/no7.geojson",
    "region08": "config/no8.geojson",
    "region09": "config/no9.geojson",
}
# No 2024 burn events; must never appear in an acquisition plan.
FORBIDDEN_REGIONS = ("region02", "region10")

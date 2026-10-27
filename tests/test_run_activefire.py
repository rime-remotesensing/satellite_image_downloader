"""run_activefire.py: the MODIS (old run2.py) and VIIRS (old run4.py) FIRMS acquisition, unchanged in behaviour."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

import run_activefire as af
from src.activefire import _write_activefire_shapefile
from src.halo_context import build_halo_context, load_geojson_geometry

RESEARCH_ASO = Path("/aso")  # read-only mount of the research Aso tree (MODIS/VIIRS activefire_halo shapefiles)


def _modis_row(d="2024-03-10", sat="Aqua", lon=131.06, lat=33.17):
    return {"latitude": str(lat), "longitude": str(lon), "brightness": "301.4", "scan": "1.1", "track": "1", "acq_date": d,
            "acq_time": "505", "satellite": sat, "instrument": "MODIS", "confidence": "47", "version": "61.03",
            "bright_t31": "288.8", "frp": "6", "daynight": "D", "type": "0"}


def _viirs_row(d="2024-03-10", sat="N21", lon=131.06, lat=33.17):
    """FIRMS VIIRS row; SP archives (N, N20) carry 'type' and version '2', the NOAA-21 NRT feed neither."""
    row = {"latitude": str(lat), "longitude": str(lon), "bright_ti4": "325.1", "scan": "0.56", "track": "0.43",
           "acq_date": d, "acq_time": "440", "satellite": sat, "instrument": "VIIRS", "confidence": "n",
           "version": "2.0NRT" if sat == "N21" else "2", "bright_ti5": "286.8", "frp": "3.5", "daynight": "D"}
    return row if sat == "N21" else {**row, "type": "0"}


def test_sources_and_processing_levels():
    assert af.MODIS_PRODUCT == "MODIS_SP"
    assert af.VIIRS_PLATFORM_PRODUCT == {"snpp": "VIIRS_SNPP_SP", "noaa20": "VIIRS_NOAA20_SP", "noaa21": "VIIRS_NOAA21_NRT"}
    assert af.PROCESSING_LEVEL["VIIRS_NOAA21_NRT"] == "NRT" and af.PROCESSING_LEVEL["VIIRS_SNPP_SP"] == "SP"
    assert (af.DATE_START, af.DATE_END) == ("2024-02-08", "2024-04-10")


def test_modis_request_is_the_old_run2_halo_bbox_without_extra_buffer():
    req = af.modis_request("region01")
    geom = load_geojson_geometry(Path("config/no1.geojson"))
    halo = build_halo_context(geom, 13_000.0)
    assert req["bbox"] == halo["halo_bbox_wgs84"]
    assert req["config"] == {"firms": {"bbox_buffer_m": 0, "clip_to_aoi": False}}
    assert req["product_map_override"] == {"modis": ["MODIS_SP"], "viirs": []} and req["satellites"] == ["modis"]
    assert af.modis_request("region07")["bbox"] == build_halo_context(load_geojson_geometry(Path("config/no7.geojson")), 10_000.0)["halo_bbox_wgs84"]


def test_viirs_request_is_the_old_run4_region_buffer_per_platform():
    assert af.viirs_request("region01", "snpp")["config"]["firms"]["bbox_buffer_m"] == 13_000.0
    assert af.viirs_request("region04", "noaa21")["config"]["firms"]["bbox_buffer_m"] == 10_000.0
    assert af.viirs_request("region04", "noaa21")["product_map_override"] == {"modis": [], "viirs": ["VIIRS_NOAA21_NRT"]}


def test_job_set_covers_7_modis_and_8x3_viirs_sources():
    jobs = af.jobs(list(af.MODIS_REGION_GEOJSONS), list(af.VIIRS_REGION_GEOJSONS))
    assert sum(j[1] == "modis" for j in jobs) == 7 and sum(j[1] == "viirs" for j in jobs) == 24
    assert all(not r.startswith(("region02", "region10")) for r, *_ in jobs)


def test_download_calls_the_unchanged_worker_and_skips_existing(tmp_path, monkeypatch):
    calls = []

    def worker(**kw):
        calls.append(kw)
        out = kw["output_root"] / kw["satellites"][0] / "activefire"
        out.mkdir(parents=True, exist_ok=True)
        rows = [_modis_row()] if kw["satellites"] == ["modis"] else [_viirs_row(sat={"VIIRS_SNPP_SP": "N", "VIIRS_NOAA20_SP": "N20",
                                                                                     "VIIRS_NOAA21_NRT": "N21"}[kw["product_map_override"]["viirs"][0]])]
        _write_activefire_shapefile(out / "ACFR_20240310_0000.shp", rows, output_crs="EPSG:4326")
        return {"modis": 1, "viirs": 1}

    monkeypatch.setattr(af, "_process_activefire", worker)
    monkeypatch.setattr(af, "_load_worker", lambda: None)
    assert af.main(["--download", "--output-root", str(tmp_path), "--regions", "region01"]) == 0
    assert len(calls) == 4
    assert all(c["start_date"] == date(2024, 2, 8) and c["end_date"] == date(2024, 4, 10) for c in calls)
    assert calls[0]["output_root"] == tmp_path / "region01"
    assert {c["output_root"] for c in calls[1:]} == {tmp_path / "region01" / p for p in af.VIIRS_PLATFORMS}
    assert (tmp_path / "MODIS" / "region01" / "2024" / "activefire" / "ACFR_20240310_0000.shp").exists()  # C2 contract copy
    assert (tmp_path / "region01" / "combined" / "activefire_combined.csv").exists()
    calls.clear()
    assert af.main(["--download", "--output-root", str(tmp_path), "--regions", "region01"]) == 0
    assert calls == []  # existing shapefiles: no re-fetch, no overwrite
    assert af.main(["--verify", "--output-root", str(tmp_path), "--regions", "region01"]) == 0
    rep = json.loads(sorted((tmp_path / af.REPORT_DIR).glob("activefire_verify_*.json"))[-1].read_text(encoding="utf-8"))
    assert rep["status_counts"] == {"SPEC_OK": 4}


def test_verify_flags_wrong_platform_and_out_of_range_date(tmp_path):
    shp = tmp_path / "ACFR_20240310_0000.shp"
    _write_activefire_shapefile(shp, [_viirs_row(sat="N20"), _viirs_row(d="2024-05-01", sat="N21")], output_crs="EPSG:4326")
    probs = af.verify_shapefile(shp, "VIIRS_NOAA21_NRT", (130, 32, 132, 34), date(2024, 2, 8), date(2024, 4, 10))
    assert any("fields" in p for p in probs)  # an SP-schema file (with 'type') is not accepted as NRT
    _write_activefire_shapefile(shp, [_viirs_row(sat="N20"), _viirs_row(d="2024-05-01")], output_crs="EPSG:4326")
    probs = af.verify_shapefile(shp, "VIIRS_NOAA21_NRT", (130, 32, 132, 34), date(2024, 2, 8), date(2024, 4, 10))
    assert any("fields" in p for p in probs)
    _write_activefire_shapefile(shp, [_viirs_row(d="2024-05-01"), {**_viirs_row(), "satellite": "N20"}], output_crs="EPSG:4326")
    probs = af.verify_shapefile(shp, "VIIRS_NOAA21_NRT", (130, 32, 132, 34), date(2024, 2, 8), date(2024, 4, 10))
    assert any("satellite N20" in p for p in probs) and any("outside" in p for p in probs)


def test_dry_run_needs_no_network(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(af, "_load_worker", lambda: pytest.fail("dry-run must not load the FIRMS worker"))
    assert af.main(["--output-root", str(tmp_path)]) == 0
    assert '"network_used": false' in capsys.readouterr().out and not any(tmp_path.iterdir())


@pytest.mark.skipif(not (RESEARCH_ASO / "MODIS").is_dir(), reason="research Aso tree not mounted at /aso")
def test_research_af4_source_shapefiles_satisfy_the_contract():
    """The shapefiles the research AF4 catalogue was built from pass the same checks (read-only)."""
    start, end = date(2024, 2, 1), date(2024, 4, 10)
    n = 0
    for region in af.MODIS_REGION_GEOJSONS:
        for shp in (RESEARCH_ASO / "MODIS" / region / "2024" / "activefire_halo").glob("ACFR_*.shp"):
            assert af.verify_shapefile(shp, "MODIS_SP", af.request_bbox(region, "modis"), start, end) == []
            n += 1
        for p, product in af.VIIRS_PLATFORM_PRODUCT.items():
            d = RESEARCH_ASO / "VIIRS" / region / "2024" / "activefire_halo" / p / "viirs" / "activefire"
            for shp in d.glob("ACFR_*.shp"):
                assert af.verify_shapefile(shp, product, af.request_bbox(region, "viirs"), start, end) == []
                n += 1
    assert n > 0

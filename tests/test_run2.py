from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_origin

import run2


def test_literal_plan_is_unchanged_and_has_expected_scene_totals():
    canonical_plan = json.dumps(run2.RUN2_DOWNLOAD_PLAN, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical_plan.encode()).hexdigest() == "b5b1eec3bce5914a4d527af01e06344fea91e646bee9ecb6ec316ba011ad8fdb"
    assert run2.validate_download_plan() == {"aqua": 111, "terra": 107}
    assert sum(len(days) for platforms in run2.RUN2_DOWNLOAD_PLAN.values() for days in platforms.values()) == 218


def test_every_planned_region_has_a_geojson():
    for region, relative_path in run2.REGION_GEOJSONS.items():
        assert region in run2.RUN2_DOWNLOAD_PLAN
        assert (Path.cwd() / relative_path).is_file()


def test_region_halo_overrides_and_native_halo_context():
    assert run2.DEFAULT_HALO_BUFFER_M == 10_000.0
    assert run2.REGION_HALO_BUFFER_M == {"region01": 13_000.0}
    assert run2.halo_buffer_m_for_region("region01") == 13_000.0
    for region in ("region03", "region05", "region06", "region07", "region08", "region09"):
        assert run2.halo_buffer_m_for_region(region) == 10_000.0

    geometry = run2._load_geojson_geometry(Path.cwd() / run2.REGION_GEOJSONS["region07"])
    halo = run2.build_halo_context(geometry, run2.halo_buffer_m_for_region("region07"))
    original_wgs84 = halo["original_bbox_wgs84"]
    expanded_wgs84 = halo["expanded_bbox_wgs84"]
    original_native = halo["original_native_bounds"]
    expanded_native = halo["halo_native_bounds"]

    assert halo["buffer_m"] == 10_000.0
    assert expanded_wgs84[0] < original_wgs84[0]
    assert expanded_wgs84[1] < original_wgs84[1]
    assert expanded_wgs84[2] > original_wgs84[2]
    assert expanded_wgs84[3] > original_wgs84[3]
    assert (original_native[0] - expanded_native[0]) / run2.MODIS_500M_PIXEL_SIZE_M >= 18
    assert (original_native[1] - expanded_native[1]) / run2.MODIS_500M_PIXEL_SIZE_M >= 18
    assert (expanded_native[2] - original_native[2]) / run2.MODIS_500M_PIXEL_SIZE_M >= 18
    assert (expanded_native[3] - original_native[3]) / run2.MODIS_500M_PIXEL_SIZE_M >= 18


def test_platform_product_activefire_contract_and_output_root(monkeypatch):
    monkeypatch.setenv("SATDL_HOST_DATA_PATH", "D:/example-host")
    assert run2.PRODUCT_BY_PLATFORM == {"terra": "MOD09GA", "aqua": "MYD09GA"}
    assert run2.ACTIVEFIRE_SATELLITES == ("modis",)
    assert run2.ACTIVEFIRE_PRODUCT_MAP == {"modis": ["MODIS_SP"], "viirs": []}
    assert (run2.RUN2_START_DATE, run2.RUN2_END_DATE) == ("2024-02-08", "2024-04-10")
    assert run2.default_output_root("region07") == Path("D:/example-host/aoi_rectangle/output_halo10km")
    assert run2.default_output_root("region01") == Path("D:/example-host/aoi_rectangle/output_halo13km_region01")
    assert run2.default_output_root("region01") != run2.default_output_root("region07")


def test_region01_literal_plan_has_31_modis_scenes():
    region01 = run2.RUN2_DOWNLOAD_PLAN["region01"]
    assert len(region01["aqua"]) == 16
    assert len(region01["terra"]) == 15
    assert sum(map(len, region01.values())) == 31


def test_synthetic_geotiff_halo_validation(tmp_path):
    pixel = run2.MODIS_500M_PIXEL_SIZE_M
    original = (0.0, 0.0, 2 * pixel, 2 * pixel)
    path = tmp_path / "synthetic_halo.tif"
    transform = from_origin(-20 * pixel, 22 * pixel, pixel, pixel)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=42,
        width=42,
        count=1,
        dtype="uint8",
        crs=CRS.from_proj4(run2.MODIS_SINUSOIDAL_PROJ4),
        transform=transform,
    ) as dataset:
        dataset.write(np.zeros((1, 42, 42), dtype=np.uint8))

    context = run2.validate_halo_raster(path, original)
    assert context == pytest.approx({"left": 20, "bottom": 20, "right": 20, "top": 20})


def test_modis_activefire_files_are_published_to_the_c2_contract_without_overwrite(tmp_path):
    source_dir = tmp_path / "region07" / "modis" / "activefire"
    source_dir.mkdir(parents=True)
    for suffix, content in ((".shp", b"shape"), (".dbf", b"table"), (".shx", b"index"), (".prj", b"crs")):
        (source_dir / f"ACFR_20240208_1200{suffix}").write_bytes(content)

    published = run2._publish_activefire_contract(tmp_path, "region07")
    contract_dir = tmp_path / "MODIS" / "region07" / "2024" / "activefire"
    assert len(published) == 4
    assert (contract_dir / "ACFR_20240208_1200.shp").read_bytes() == b"shape"
    assert run2._publish_activefire_contract(tmp_path, "region07") == []


def test_dry_run_never_starts_workers_and_honours_region_subset(capsys):
    with patch.object(run2, "_process_platform") as worker, patch.object(run2, "_process_activefire") as fire:
        assert run2.main(["--dry-run", "--regions", "region07", "region08"]) == 0
    assert not worker.called
    assert not fire.called
    output = capsys.readouterr().out
    assert "Aqua=111, Terra=107, total=218" in output
    assert "region07" in output
    assert "region08" in output
    assert "region01:" not in output
    assert "output_halo10km" in output


def test_region01_dry_run_uses_13km_separate_output_without_workers(capsys):
    with patch.object(run2, "_process_platform") as worker, patch.object(run2, "_process_activefire") as fire:
        assert run2.main(["--dry-run", "--regions", "region01"]) == 0
    assert not worker.called
    assert not fire.called
    output = capsys.readouterr().out
    assert "halo buffer: 13000 m" in output
    assert "Aqua=16, Terra=15, Total=31" in output
    assert "MODIS_SP only" in output
    assert "output_halo13km_region01" in output
    assert "output_halo10km\\region01" not in output

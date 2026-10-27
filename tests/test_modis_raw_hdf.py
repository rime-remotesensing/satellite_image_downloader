from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src import modis_raw_hdf as m

G1 = "MOD09GA.A2024039.h28v05.061.2024041034305.hdf"
G2 = "MYD09GA.A2024067.h28v05.061.2024069030914.hdf"
URL = "https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/{p}.061/{s}/{g}"

FIELDS = ["granule_id", "regions", "date", "platform", "product", "version", "tile", "destination", "https_url"]

RESEARCH_CSV = Path("/manifest/MISSING_HDF_DOWNLOAD_MANIFEST.csv")


def _row(g, platform, date, regions="region01"):
    stem = g[:-4]
    return {
        "granule_id": g, "regions": regions, "date": date, "platform": platform,
        "product": g.split(".")[0], "version": "061", "tile": g.split(".")[2],
        "destination": f"${{RAW_HDF_ROOT}}/recovered_modis_061/{g}",
        "https_url": URL.format(p=g.split(".")[0], s=stem, g=g),
    }


def _write_csv(path, rows, bom=True):
    with path.open("w", newline="", encoding="utf-8-sig" if bom else "utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    return path


@pytest.fixture
def manifest(tmp_path):
    return _write_csv(tmp_path / "m.csv", [_row(G1, "terra", "2024-02-08", "region01;region08"),
                                           _row(G2, "aqua", "2024-03-07")])


def _fake_verify(ok_paths):
    def verify(path, record, check_filename=True):
        ok = Path(path).exists() and str(path) in ok_paths
        return {"ok": ok, "path": str(path), "problems": [] if ok else ["fake invalid"]}
    return verify


def test_manifest_with_bom_is_parsed(manifest):
    recs = m.load_manifest(manifest)
    assert [r.granule_id for r in recs] == [G1, G2]
    assert recs[0].regions == ("region01", "region08")
    assert recs[0].platform == "terra" and recs[1].platform == "aqua"


@pytest.mark.parametrize("field,value", [
    ("version", "006"), ("platform", "aqua"), ("tile", "h29v05"), ("date", "2024-02-09"),
    ("https_url", URL.format(p="MOD09GA", s="x", g="MOD09GA.A2024039.h28v05.061.2099999999999.hdf")),
])
def test_manifest_inconsistency_is_rejected(tmp_path, field, value):
    row = _row(G1, "terra", "2024-02-08")
    row[field] = value
    with pytest.raises(m.ManifestError):
        m.load_manifest(_write_csv(tmp_path / "bad.csv", [row]))


def test_duplicate_granule_rejected(tmp_path):
    with pytest.raises(m.ManifestError):
        m.load_manifest(_write_csv(tmp_path / "d.csv", [_row(G1, "terra", "2024-02-08")] * 2))


def test_non_exact_granule_name_rejected(tmp_path):
    row = _row(G1, "terra", "2024-02-08")
    row["granule_id"] = "MOD09GA.A2024039.h28v05.061.*.hdf"
    with pytest.raises(m.ManifestError):
        m.load_manifest(_write_csv(tmp_path / "w.csv", [row]))


def test_plan_classifies_missing_present_invalid_and_elsewhere(tmp_path, manifest):
    recs = m.load_manifest(manifest)
    raw = tmp_path / "raw"
    elsewhere = tmp_path / "old_cache"
    d1 = recs[0].destination(raw)
    d1.parent.mkdir(parents=True)
    d1.write_bytes(b"corrupt")
    elsewhere.mkdir()
    (elsewhere / G2).write_bytes(b"good")
    st = m.plan_recovery(recs, raw, existing_roots=[elsewhere], verify=_fake_verify({str(elsewhere / G2)}))
    assert [s.status for s in st] == [m.STATUS_PRESENT_INVALID, m.STATUS_PRESENT_ELSEWHERE]
    assert d1.read_bytes() == b"corrupt"  # invalid existing file left untouched


def test_recovery_ignores_processed_geotiffs(tmp_path, manifest):
    """HDF recovery is decided only by the HDF itself -- a fully processed
    GeoTIFF set existing elsewhere must not make it skip the raw file."""
    recs = m.load_manifest(manifest)
    tif = tmp_path / "out" / "region01" / "2024" / "surface_reflectance" / "terra" / "500m" / "MOD09GA_terra_20240208_500m.tif"
    tif.parent.mkdir(parents=True)
    tif.write_bytes(b"processed image")
    st = m.plan_recovery(recs, tmp_path / "raw", verify=_fake_verify(set()))
    assert all(s.status == m.STATUS_MISSING for s in st)


def test_download_writes_atomically_and_only_missing(tmp_path, manifest):
    recs = m.load_manifest(manifest)
    raw = tmp_path / "raw"
    present = recs[1].destination(raw)
    present.parent.mkdir(parents=True)
    present.write_bytes(b"already here")
    final = recs[0].destination(raw)
    called = []

    def downloader(url, part, session):
        called.append(url)
        assert part.name.endswith(".part") and not final.exists()
        part.write_bytes(b"hdf-bytes")

    def verify(path, record, check_filename=True):
        return {"ok": True, "path": str(path), "problems": []}

    st = m.plan_recovery(recs, raw, verify=verify)
    st = m.download_missing(st, recs, session_factory=lambda: object(), downloader=downloader,
                            verify=verify, retry=lambda fn, service=None: fn())
    assert called == [recs[0].https_url]          # exact manifest URL, only the missing one
    assert final.read_bytes() == b"hdf-bytes"
    assert not final.with_name(final.name + ".part").exists()
    assert present.read_bytes() == b"already here"  # never overwritten
    assert [s.status for s in st] == [m.STATUS_DOWNLOADED_VALID, m.STATUS_PRESENT_VALID]


def test_invalid_download_leaves_no_final_file(tmp_path, manifest):
    recs = m.load_manifest(manifest)[:1]
    raw = tmp_path / "raw"
    st = m.plan_recovery(recs, raw, verify=_fake_verify(set()))
    st = m.download_missing(st, recs, session_factory=lambda: object(),
                            downloader=lambda u, p, s: p.write_bytes(b"html error page"),
                            verify=_fake_verify(set()), retry=lambda fn, service=None: fn())
    dest = recs[0].destination(raw)
    assert st[0].status == m.STATUS_DOWNLOADED_INVALID
    assert not dest.exists() and not dest.with_name(dest.name + ".part").exists()


def test_failed_download_is_reported_and_rerunnable(tmp_path, manifest):
    recs = m.load_manifest(manifest)[:1]
    raw = tmp_path / "raw"

    def boom(u, p, s):
        p.write_bytes(b"partial")
        raise RuntimeError("connection reset")

    st = m.download_missing(m.plan_recovery(recs, raw, verify=_fake_verify(set())), recs,
                            session_factory=lambda: object(), downloader=boom,
                            verify=_fake_verify(set()), retry=lambda fn, service=None: fn())
    assert st[0].status == m.STATUS_DOWNLOAD_FAILED
    assert not any(recs[0].destination(raw).parent.glob("*"))
    # re-planning afterwards sees it as MISSING again
    assert m.plan_recovery(recs, raw, verify=_fake_verify(set()))[0].status == m.STATUS_MISSING


@pytest.mark.skipif(not RESEARCH_CSV.is_file(), reason="research manifest not mounted at /manifest")
def test_research_manifest_25_records_agree_with_frozen_run2_manifest():
    import run2
    from src.modis_plan import load_acquisition_manifest

    recs = m.load_manifest(RESEARCH_CSV)
    assert len(recs) == 25
    assert sum(r.platform == "terra" for r in recs) == 12
    assert sum(r.platform == "aqua" for r in recs) == 13
    _, frozen = load_acquisition_manifest()
    assert sorted(run2._check_research_manifest(RESEARCH_CSV, frozen)) == sorted(r.granule_id for r in recs)

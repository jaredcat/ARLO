"""Tests for map marker queries and loot-area intersection."""

from __future__ import annotations

import sqlite3

import pytest

from arc_helper.map_data import MapCatalog
from arc_helper.map_data import MapMeta
from arc_helper.map_data import ensure_map_schema
from arc_helper.map_data import latlng_to_norm
from arc_helper.map_data import norm_to_latlng
from arc_helper.map_data import parse_loot_areas


def _seed_db(path) -> MapCatalog:
    conn = sqlite3.connect(path)
    ensure_map_schema(conn)
    conn.execute(
        """
        INSERT INTO map_meta (
            map_id, display_name, lat_min, lat_max, lng_min, lng_max,
            marker_count, overview_rel, reference_rel, ref_width, ref_height
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("dam", "Dam", 0.0, 100.0, 0.0, 200.0, 3, "", "", 100, 100),
    )
    rows = [
        ("a", "dam", 10.0, 20.0, "containers", "locker", "Locker", "Industrial, Security", 0, 0),
        ("b", "dam", 50.0, 80.0, "containers", "med_crate", "Med", "Medical", 0, 0),
        ("c", "dam", 90.0, 180.0, "nature", "moss", "", "", 0, 0),
        ("d", "dam", 11.0, 21.0, "containers", "computer", "Computer", "Technological, Electrical", 1, 0),
    ]
    conn.executemany(
        """
        INSERT INTO map_markers (
            id, map_id, lat, lng, category, subcategory,
            instance_name, loot_areas, behind_locked_door, zlayers
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.execute(
        "INSERT INTO item_loot_areas (name, loot_areas) VALUES (?, ?)",
        ("Steel Spring", "Industrial"),
    )
    conn.execute(
        "INSERT INTO item_loot_areas (name, loot_areas) VALUES (?, ?)",
        ("Moss", "Nature"),
    )
    conn.commit()
    conn.close()
    return MapCatalog(path)


def test_parse_loot_areas():
    assert parse_loot_areas("Industrial, Security") == ["Industrial", "Security"]
    assert parse_loot_areas(["Medical", " "]) == ["Medical"]
    assert parse_loot_areas(None) == []


def test_query_by_loot_area(tmp_path):
    cat = _seed_db(tmp_path / "t.db")
    hits = cat.query_markers("dam", categories={"containers"}, loot_areas={"Industrial"})
    assert len(hits) == 1
    assert hits[0].instance_name == "Locker"


def test_markers_for_item_and_nature(tmp_path):
    cat = _seed_db(tmp_path / "t.db")
    springs = cat.markers_for_item("dam", "Steel Spring")
    assert len(springs) == 1
    assert springs[0].loot_areas.startswith("Industrial")

    moss = cat.markers_for_item("dam", "Moss")
    assert any(m.subcategory == "moss" for m in moss)


def test_item_without_loot_tags_still_resolves_loose(tmp_path):
    """Insulation Roll-style items: no loot_area, but precise spawn clusters."""
    path = tmp_path / "t.db"
    cat = _seed_db(path)
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO item_loot_areas (name, loot_areas, item_id) VALUES (?, ?, ?)",
        ("Insulation Roll", "", "insulation-roll"),
    )
    conn.execute(
        """
        INSERT INTO map_item_spot_clusters
            (map_id, item_id, item_name, lat, lng, count)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("dam", "insulation-roll", "Insulation Roll", 12.0, 34.0, 5),
    )
    conn.commit()
    conn.close()

    ref = cat.resolve_item_ref("insulation roll")
    assert ref is not None
    assert ref[0] == "insulation-roll"
    assert ref[1] == "Insulation Roll"
    assert cat.loot_areas_for_item("Insulation Roll") == []

    hits = cat.markers_for_item("dam", "insulation roll")
    assert len(hits) == 1
    assert hits[0].category == "loose"
    assert "×5" in hits[0].instance_name


def test_nearby_sort(tmp_path):
    cat = _seed_db(tmp_path / "t.db")
    hits = cat.query_markers(
        "dam",
        categories={"containers"},
        origin=(10.0, 20.0),
    )
    assert hits[0].id == "a"
    assert hits[0].distance is not None
    assert hits[0].distance <= hits[1].distance


def test_locked_filter(tmp_path):
    cat = _seed_db(tmp_path / "t.db")
    locked = cat.query_markers("dam", behind_locked_only=True)
    assert len(locked) == 1
    assert locked[0].id == "d"


def test_norm_roundtrip():
    meta = MapMeta(
        map_id="dam",
        lat_min=0,
        lat_max=100,
        lng_min=0,
        lng_max=200,
    )
    lat, lng = 25.0, 50.0
    nx, ny = latlng_to_norm(lat, lng, meta)
    lat2, lng2 = norm_to_latlng(nx, ny, meta)
    assert lat2 == pytest.approx(lat)
    assert lng2 == pytest.approx(lng)


def test_latlng_y_increases_south_on_metaforge_crs():
    """Metaforge lat grows southward; north-up PNG needs low lat → low y."""
    meta = MapMeta(
        map_id="pendola-pass",
        lat_min=2048.0,
        lat_max=14336.0,
        lng_min=0.0,
        lng_max=16384.0,
    )
    # Observatory (north) vs Railyard (south) — matching live POI ordering.
    _nx_n, ny_n = latlng_to_norm(7078.0, 10081.0, meta)
    _nx_s, ny_s = latlng_to_norm(9174.0, 9774.0, meta)
    assert ny_n < ny_s
    assert ny_n == pytest.approx((7078.0 - 2048.0) / (14336.0 - 2048.0))


def test_world_extent_bounds_and_tile_fmt_order():
    from arc_helper.map_ingest import MAP_TILE_CONFIGS
    from arc_helper.map_ingest import TILE_NAME_FMTS
    from arc_helper.map_ingest import tile_grid_crs_bounds
    from arc_helper.map_ingest import world_extent_bounds

    # Prefer nested z/x/y before underscore z/x_y
    assert TILE_NAME_FMTS[0] == "{x}/{y}.webp"
    assert TILE_NAME_FMTS[1] == "{x}_{y}.webp"

    # Blue Gate worldExtent corners → lat/lng min/max
    lat_min, lat_max, lng_min, lng_max = world_extent_bounds(
        (7416.0, 6553.45, 3439.27, 9597.99)
    )
    assert lat_min == pytest.approx(3439.27)
    assert lat_max == pytest.approx(7416.0)
    assert lng_min == pytest.approx(6553.45)
    assert lng_max == pytest.approx(9597.99)

    # Pendola stitched grid x0-7 y1-6 over 0..16384 @ z3
    cfg = MAP_TILE_CONFIGS["pendola-pass"]
    lat_min, lat_max, lng_min, lng_max = tile_grid_crs_bounds(cfg, 0, 7, 1, 6)
    assert lat_min == pytest.approx(2048.0)
    assert lat_max == pytest.approx(14336.0)
    assert lng_min == pytest.approx(0.0)
    assert lng_max == pytest.approx(16384.0)


def test_maps_manifest_outdated(tmp_path, monkeypatch):
    from arc_helper import map_ingest
    from arc_helper.map_ingest import MAP_TILE_CONFIGS
    from arc_helper.map_ingest import assets_outdated
    from arc_helper.map_ingest import tile_config_signature
    from arc_helper.map_ingest import write_maps_manifest

    monkeypatch.setattr(map_ingest, "maps_dir", lambda: tmp_path)
    monkeypatch.setattr(
        map_ingest, "map_dir", lambda map_id: tmp_path / map_id
    )
    mid = "blue-gate"
    cfg = MAP_TILE_CONFIGS[mid]
    mdir = tmp_path / mid
    mdir.mkdir()
    # Minimal valid PNG
    from PIL import Image

    Image.new("RGBA", (8, 8), (0, 0, 0, 255)).save(mdir / "reference.png")
    assert assets_outdated(mid)  # no manifest yet
    write_maps_manifest(map_ids=(mid,))
    assert not assets_outdated(mid)
    # Layer path drift → outdated
    manifest = {
        "maps": {
            mid: {**tile_config_signature(cfg), "layer_path": "blue-gate/old"}
        }
    }
    assert assets_outdated(mid, manifest=manifest)

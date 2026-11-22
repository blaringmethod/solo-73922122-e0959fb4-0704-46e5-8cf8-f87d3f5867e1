"""Tests for historical work-area wet-cell statistics.

Two storage eras are covered:

* **new packages** (ingested after the upgrade) keep every reported point in
  ``package_wet_archive`` and therefore get exact answers;
* **historical packages** (already in a version-1 database when the upgrade
  ran) only keep the wet/dry verdicts knowable at upgrade time; cells then
  covered by a higher version stay unknown and the original reported total
  bounds the possible wet count.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.db import connect
from app.repository import union_area

from .conftest import make_app, post_pkg
from .test_parsing import pkg


@pytest.fixture()
def client(tmp_path):
    app = make_app(tmp_path)
    with app.test_client() as c:
        yield c


def history(client, version, r0, r1, c0, c1):
    response = client.get(
        "/api/history/wet"
        f"?version={version}&row_start={r0}&row_end={r1}&col_start={c0}&col_end={c1}"
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


# --------------------------------------------------------------------------
# New packages: exact answers from the full archive
# --------------------------------------------------------------------------


def test_new_package_keeps_reported_points_even_when_superseded(client):
    post_pkg(client, pkg(1, 0, 10, 0, 10, [(1, 1), (2, 2), (3, 3)]))
    # v2 re-surveys the whole rectangle and reports only one wet cell.
    post_pkg(client, pkg(2, 0, 10, 0, 10, [(5, 5)]))

    body = history(client, 1, 0, 10, 0, 10)
    assert body["exact"] is True
    assert body["count"] == 3
    assert body["min_wet"] == body["max_wet"] == 3
    assert body["historical"] is False
    assert body["known_wet"] == 3
    assert body["known_dry"] == 97
    assert body["unknown_cells"] == 0
    # The archive holds all three original points, although current_wet has one.
    listing = client.get("/api/packages").get_json()["packages"]
    by_version = {p["version"]: p for p in listing}
    assert by_version[1]["archived_wet_points"] == 3
    assert by_version[1]["current_wet_points"] == 0
    assert by_version[2]["archived_wet_points"] == 1


def test_new_package_subbox_counts_archived_points(client):
    post_pkg(client, pkg(1, 0, 20, 0, 20, [(1, 1), (2, 2), (15, 15)]))
    body = history(client, 1, 0, 10, 0, 10)
    assert body["exact"] is True
    assert body["count"] == 2
    assert [(c["row"], c["col"]) for c in body["archived_cells"]] == [(1, 1), (2, 2)]


def test_new_package_shadowed_late_arrival_still_exact(client):
    # Higher version first, then a late older package whose points are shadowed.
    post_pkg(client, pkg(10, 0, 10, 0, 10, []))
    post_pkg(client, pkg(5, 0, 10, 0, 10, [(3, 3), (4, 4)]))
    body = history(client, 5, 0, 10, 0, 10)
    assert body["exact"] is True
    assert body["count"] == 2  # both original reports survive in the archive


def test_no_intersection_reports_zero_exact(client):
    post_pkg(client, pkg(1, 0, 10, 0, 10, [(1, 1)]))
    body = history(client, 1, 20, 30, 20, 30)
    assert body["intersection"] is None
    assert body["exact"] is True
    assert body["count"] == 0
    assert body["min_wet"] == body["max_wet"] == 0


def test_partial_intersection_uses_only_overlap(client):
    post_pkg(client, pkg(1, 0, 10, 0, 10, [(1, 1), (8, 8)]))
    body = history(client, 1, 5, 15, 5, 15)
    assert body["intersection"] == {
        "row_start": 5, "row_end": 10, "col_start": 5, "col_end": 10
    }
    assert body["count"] == 1


def test_history_endpoint_validation(client):
    post_pkg(client, pkg(1, 0, 10, 0, 10, []))
    assert client.get(
        "/api/history/wet?version=1&row_start=5&row_end=0&col_start=0&col_end=5"
    ).status_code == 400
    assert client.get(
        "/api/history/wet?version=x&row_start=0&row_end=5&col_start=0&col_end=5"
    ).status_code == 400
    missing = client.get(
        "/api/history/wet?row_start=0&row_end=5&col_start=0&col_end=5"
    )
    assert missing.status_code == 400
    not_found = client.get(
        "/api/history/wet?version=99&row_start=0&row_end=5&col_start=0&col_end=5"
    )
    assert not_found.status_code == 404
    assert not_found.get_json()["error"] == "package_not_found"


# --------------------------------------------------------------------------
# Historical packages: frozen upgrade-time knowledge + bounded estimates
# --------------------------------------------------------------------------


def _build_legacy_database(db_path: str) -> None:
    """Create a version-1 warehouse with two packages, then run the upgrade."""
    conn = connect(db_path)
    conn.executescript(
        """
        CREATE TABLE packages (
          version INTEGER PRIMARY KEY, digest TEXT NOT NULL,
          r0 INTEGER NOT NULL, r1 INTEGER NOT NULL,
          c0 INTEGER NOT NULL, c1 INTEGER NOT NULL,
          wet_count INTEGER NOT NULL CHECK (wet_count >= 0),
          imported_at TEXT NOT NULL,
          CHECK (r0 < r1), CHECK (c0 < c1));
        CREATE VIRTUAL TABLE coverage_rects USING rtree_i32(
          version, r0, r1, c0, c1);
        CREATE TABLE current_wet (
          row INTEGER NOT NULL, col INTEGER NOT NULL, version INTEGER NOT NULL,
          PRIMARY KEY (row, col)) WITHOUT ROWID;
        PRAGMA user_version = 1;
        """
    )
    with conn:
        # v3 reports four points over a 10x10 rectangle.
        conn.execute("INSERT INTO packages VALUES (3, 'd3', 0, 10, 0, 10, 4, 't')")
        conn.execute("INSERT INTO coverage_rects VALUES (3, 0, 10, 0, 10)")
        for row, col in [(1, 1), (2, 2), (6, 6), (7, 7)]:
            conn.execute("INSERT INTO current_wet VALUES (?, ?, 3)", (row, col))
        # v7 covers the 5x5 bottom-right corner and supersedes v3 there.
        conn.execute("INSERT INTO packages VALUES (7, 'd7', 5, 10, 5, 10, 1, 't')")
        conn.execute("INSERT INTO coverage_rects VALUES (7, 5, 10, 5, 10)")
        conn.execute(
            "DELETE FROM current_wet "
            "WHERE row >= 5 AND row < 10 AND col >= 5 AND col < 10 AND version <= 7"
        )
        conn.execute("INSERT INTO current_wet VALUES (6, 6, 7)")
    conn.close()


@pytest.fixture()
def upgraded_client(tmp_path):
    db_path = str(tmp_path / "legacy.sqlite3")
    _build_legacy_database(db_path)
    app = make_app(tmp_path, DATABASE=db_path)
    with app.test_client() as c:
        yield c, db_path


def test_upgrade_flags_packages_historical_and_freezes_snapshot(upgraded_client):
    client, _db_path = upgraded_client
    packages = client.get("/api/packages").get_json()["packages"]
    by_version = {p["version"]: p for p in packages}
    assert by_version[3]["historical"] is True
    assert by_version[7]["historical"] is True
    # v3 still had two effective wet cells when the upgrade ran.
    assert by_version[3]["archived_wet_points"] == 2
    assert by_version[7]["archived_wet_points"] == 1

    raw = connect(_db_path)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 2
        rects = raw.execute(
            "SELECT r0, r1, c0, c1 FROM package_unknown_rects WHERE package_version = 3"
        ).fetchall()
        assert [tuple(r) for r in rects] == [(5, 10, 5, 10)]
        # v7 has no higher version above it, hence no unknown region.
        assert raw.execute(
            "SELECT COUNT(*) FROM package_unknown_rects WHERE package_version = 7"
        ).fetchone()[0] == 0
    finally:
        raw.close()


def test_historical_known_only_subbox_is_exact(upgraded_client):
    client, _ = upgraded_client
    body = history(client, 3, 0, 5, 0, 10)
    assert body["exact"] is True
    assert body["count"] == 2
    assert body["unknown_cells"] == 0
    assert body["known_wet"] == 2
    assert body["known_dry"] == 48


def test_historical_unknown_subbox_is_bounded(upgraded_client):
    client, _ = upgraded_client
    # The whole 5x5 corner is unknown; v3 reported 4 points, 2 known wet outside.
    body = history(client, 3, 5, 10, 5, 10)
    assert body["exact"] is False
    assert body["count"] is None
    assert body["min_wet"] == 0
    assert body["max_wet"] == 2
    assert body["unknown_cells"] == 25
    assert body["known_wet"] == 0
    assert body["known_dry"] == 0


def test_historical_whole_coverage_bounds(upgraded_client):
    client, _ = upgraded_client
    body = history(client, 3, 0, 10, 0, 10)
    # Two un-archived reported points must be somewhere in the 25 unknown cells.
    assert body["exact"] is False
    assert body["min_wet"] == 4
    assert body["max_wet"] == 6
    assert body["known_wet"] == 2
    assert body["known_dry"] == 73
    assert body["unknown_cells"] == 25
    assert body["listed_wet_total"] == 4


def test_historical_partial_intersection_bounds(upgraded_client):
    client, _ = upgraded_client
    # Intersection 5x5; unknown part is rows/cols 5..8 -> 9 cells.
    body = history(client, 3, 3, 8, 3, 8)
    assert body["unknown_cells"] == 9
    assert body["known_wet"] == 0
    assert body["known_dry"] == 16
    assert body["min_wet"] == 0
    assert body["max_wet"] == 2


def test_later_resurvey_keeps_upgrade_time_verdicts(upgraded_client):
    client, _ = upgraded_client
    # A brand-new full re-sweep clears every current wet cell.
    response = post_pkg(
        client, pkg(9, 0, 10, 0, 10, [])
    )
    assert response.status_code == 201
    body = history(client, 3, 0, 10, 0, 10)
    # Frozen snapshot unchanged: same bounds, same unknown rectangle.
    assert body["min_wet"] == 4
    assert body["max_wet"] == 6
    assert body["known_wet"] == 2
    assert body["unknown_cells"] == 25
    assert body["unknown_rects"] == [
        {"row_start": 5, "row_end": 10, "col_start": 5, "col_end": 10}
    ]
    # Meanwhile the live current state really is dry everywhere.
    live = client.get("/api/wet?row_start=0&row_end=10&col_start=0&col_end=10").get_json()
    assert live["count"] == 0


def test_new_ingest_after_upgrade_is_not_historical(upgraded_client):
    client, _ = upgraded_client
    post_pkg(client, pkg(11, 0, 4, 0, 4, [(0, 0)]))
    body = history(client, 11, 0, 4, 0, 4)
    assert body["historical"] is False
    assert body["exact"] is True
    assert body["count"] == 1
    packages = client.get("/api/packages").get_json()["packages"]
    assert next(p for p in packages if p["version"] == 11)["historical"] is False


# --------------------------------------------------------------------------
# Rectangle union math and failure atomicity
# --------------------------------------------------------------------------


def test_union_area_basic_cases():
    assert union_area([]) == 0
    assert union_area([(0, 10, 0, 10)]) == 100
    # Two overlapping 10x10 squares on a 15x15 span: 100 + 100 - 25.
    assert union_area([(0, 10, 0, 10), (5, 15, 5, 15)]) == 175
    # Nested rectangle adds nothing; touching rectangles share no cell.
    assert union_area([(0, 10, 0, 10), (2, 4, 2, 4)]) == 100
    assert union_area([(0, 5, 0, 5), (5, 10, 0, 5)]) == 50


def test_archive_write_failure_rolls_back_everything(client, tmp_path):
    """The archive write and the ingest are one transaction."""
    from app.parsing import parse_package
    from app.repository import ingest

    post_pkg(client, pkg(1, 0, 10, 0, 10, [(1, 1)]))

    raw = connect(client.application.config["DATABASE"])

    class FailingConn:
        """Proxy that fails on the archive insert step."""

        def __getattr__(self, name):
            return getattr(raw, name)

        def executemany(self, sql, parameters):
            if "INSERT INTO package_wet_archive" in sql:
                raise sqlite3.OperationalError("simulated archive failure")
            return raw.executemany(sql, parameters)

    with pytest.raises(sqlite3.OperationalError):
        ingest(FailingConn(), parse_package(pkg(2, 0, 10, 0, 10, [(2, 2)])))
    raw.close()

    # Nothing from package 2 leaked into any table, and the app still serves v1.
    listing = client.get("/api/packages").get_json()["packages"]
    assert [p["version"] for p in listing] == [1]
    check = connect(client.application.config["DATABASE"])
    try:
        assert check.execute("SELECT COUNT(*) FROM packages").fetchone()[0] == 1
        assert check.execute("SELECT COUNT(*) FROM coverage_rects").fetchone()[0] == 1
        assert check.execute("SELECT COUNT(*) FROM package_wet_archive").fetchone()[0] == 1
        assert check.execute("SELECT COUNT(*) FROM current_wet").fetchone()[0] == 1
    finally:
        check.close()


def test_reset_clears_history_tables(client):
    post_pkg(client, pkg(1, 0, 10, 0, 10, [(1, 1), (2, 2)]))
    assert client.post("/api/reset").status_code == 200
    raw = connect(client.application.config["DATABASE"])
    try:
        assert raw.execute("SELECT COUNT(*) FROM package_wet_archive").fetchone()[0] == 0
        assert raw.execute("SELECT COUNT(*) FROM package_unknown_rects").fetchone()[0] == 0
    finally:
        raw.close()


def test_historical_flow_without_rtree(tmp_path):
    """The upgrade and statistics also work on the plain-table fallback."""
    db_path = str(tmp_path / "legacy-no-rtree.sqlite3")
    conn = connect(db_path)
    conn.executescript(
        """
        CREATE TABLE packages (
          version INTEGER PRIMARY KEY, digest TEXT NOT NULL,
          r0 INTEGER NOT NULL, r1 INTEGER NOT NULL,
          c0 INTEGER NOT NULL, c1 INTEGER NOT NULL,
          wet_count INTEGER NOT NULL, imported_at TEXT NOT NULL,
          CHECK (r0 < r1), CHECK (c0 < c1));
        CREATE TABLE coverage_rects (
          version INTEGER PRIMARY KEY, r0 INTEGER NOT NULL, r1 INTEGER NOT NULL,
          c0 INTEGER NOT NULL, c1 INTEGER NOT NULL);
        CREATE TABLE current_wet (
          row INTEGER NOT NULL, col INTEGER NOT NULL, version INTEGER NOT NULL,
          PRIMARY KEY (row, col)) WITHOUT ROWID;
        PRAGMA user_version = 1;
        """
    )
    with conn:
        conn.execute("INSERT INTO packages VALUES (1, 'd', 0, 8, 0, 8, 2, 't')")
        conn.execute("INSERT INTO coverage_rects VALUES (1, 0, 8, 0, 8)")
        conn.execute("INSERT INTO current_wet VALUES (1, 1, 1)")
        conn.execute("INSERT INTO current_wet VALUES (6, 6, 1)")
        conn.execute("INSERT INTO packages VALUES (2, 'd', 4, 8, 4, 8, 0, 't')")
        conn.execute("INSERT INTO coverage_rects VALUES (2, 4, 8, 4, 8)")
        conn.execute("DELETE FROM current_wet WHERE row >= 4 AND col >= 4 AND version <= 2")
    conn.close()

    app = make_app(tmp_path, DATABASE=db_path, PREFER_RTREE=False)
    with app.test_client() as c:
        body = history(c, 1, 0, 8, 0, 8)
        assert body["historical"] is True
        # 4x4 unknown corner (16 cells), one known wet outside it.  The second
        # reported point is un-archived and must lie in the unknown region, so
        # the minimum is exactly 2; the region could hide up to two reported
        # points (the archived outside point was a report too), hence max 3.
        assert body["unknown_cells"] == 16
        assert body["known_wet"] == 1
        assert body["min_wet"] == 2
        assert body["max_wet"] == 3
        assert body["exact"] is False

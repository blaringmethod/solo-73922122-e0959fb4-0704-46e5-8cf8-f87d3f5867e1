"""End-to-end HTTP test of the exact demo flow through a real gunicorn-ish app.

Run manually with ``python3 scripts/demo_http.py`` (uses Flask's test client,
so no network needed) or point ``FLOOD_DEMO_URL`` at a running server.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SAMPLES = ROOT / "app" / "static" / "samples"


def via_url(base: str) -> int:
    def request(path, data=None):
        req = urllib.request.Request(base + path, data=data)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    checks = []
    for name in ("v3.json", "v7.json", "v5-late.json"):
        code, body = request("/api/packages", (SAMPLES / name).read_bytes())
        checks.append((name, code, body.get("status")))
    code, body = request(
        "/api/packages", (SAMPLES / "v7-retry.json").read_bytes()
    )
    checks.append(("v7-retry.json", code, body.get("status")))
    code, body = request(
        "/api/packages", (SAMPLES / "v7-conflict.json").read_bytes()
    )
    checks.append(("v7-conflict.json", code, body.get("error")))

    _, wet_cell = request("/api/cell?row=8&col=5")
    _, dry_cell = request("/api/cell?row=22&col=7")
    _, unknown_cell = request("/api/cell?row=500&col=500")
    checks.append(("cell(8,5)", 200, wet_cell["label"]))
    checks.append(("cell(22,7)", 200, dry_cell["label"]))
    checks.append(("cell(500,500)", 200, unknown_cell["label"]))

    # New packages archive every reported point, so a superseded package still
    # answers with an exact historical count.
    code, hist = request(
        "/api/history/wet?version=3&row_start=0&row_end=30&col_start=0&col_end=30"
    )
    checks.append((
        "history v3 (new, exact)",
        code,
        f"exact={hist['exact']} count={hist['count']}",
    ))
    return report(checks)


def _build_legacy_database(db_path: str) -> None:
    """Reproduce a version-1 warehouse, as it existed before the upgrade."""
    from app.db import connect

    conn = connect(db_path)
    conn.executescript(
        """
        CREATE TABLE packages (
          version INTEGER PRIMARY KEY, digest TEXT NOT NULL,
          r0 INTEGER NOT NULL, r1 INTEGER NOT NULL,
          c0 INTEGER NOT NULL, c1 INTEGER NOT NULL,
          wet_count INTEGER NOT NULL, imported_at TEXT NOT NULL,
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
        # v3 reports four wet cells over 10x10; v7 later owns the 5x5 corner.
        conn.execute("INSERT INTO packages VALUES (3, 'd3', 0, 10, 0, 10, 4, 't')")
        conn.execute("INSERT INTO coverage_rects VALUES (3, 0, 10, 0, 10)")
        for row, col in [(1, 1), (2, 2), (6, 6), (7, 7)]:
            conn.execute("INSERT INTO current_wet VALUES (?, ?, 3)", (row, col))
        conn.execute("INSERT INTO packages VALUES (7, 'd7', 5, 10, 5, 10, 1, 't')")
        conn.execute("INSERT INTO coverage_rects VALUES (7, 5, 10, 5, 10)")
        conn.execute(
            "DELETE FROM current_wet "
            "WHERE row >= 5 AND row < 10 AND col >= 5 AND col < 10 AND version <= 7"
        )
        conn.execute("INSERT INTO current_wet VALUES (6, 6, 7)")
    conn.close()


def via_test_client() -> int:
    from app import create_app

    tmp = tempfile.mkdtemp(prefix="flood-demo-")
    app = create_app({"TESTING": True, "DATABASE": f"{tmp}/demo.sqlite3"})
    client = app.test_client()

    checks = []
    for name in ("v3.json", "v7.json", "v5-late.json"):
        resp = client.post(
            "/api/packages", data=(SAMPLES / name).read_bytes(),
            content_type="application/json",
        )
        checks.append((name, resp.status_code, resp.get_json().get("status")))
    resp = client.post(
        "/api/packages", data=(SAMPLES / "v7-retry.json").read_bytes(),
        content_type="application/json",
    )
    checks.append(("v7-retry.json", resp.status_code, resp.get_json().get("status")))
    resp = client.post(
        "/api/packages", data=(SAMPLES / "v7-conflict.json").read_bytes(),
        content_type="application/json",
    )
    checks.append(("v7-conflict.json", resp.status_code, resp.get_json().get("error")))

    for row, col in ((8, 5), (22, 7), (500, 500)):
        body = client.get(f"/api/cell?row={row}&col={col}").get_json()
        checks.append((f"cell({row},{col})", 200, body["label"]))

    # --- new-package historical statistics: exact even when superseded ---
    hist = client.get(
        "/api/history/wet?version=3&row_start=0&row_end=30&col_start=0&col_end=30"
    ).get_json()
    checks.append((
        "history v3 (new package)", 200,
        f"exact={hist['exact']} count={hist['count']} archived={hist['known_wet']}",
    ))

    # --- historical packages: upgrade a version-1 database, then estimate ---
    legacy_path = f"{tmp}/legacy.sqlite3"
    _build_legacy_database(legacy_path)
    legacy_app = create_app({"TESTING": True, "DATABASE": legacy_path})
    legacy = legacy_app.test_client()

    whole = legacy.get(
        "/api/history/wet?version=3&row_start=0&row_end=10&col_start=0&col_end=10"
    ).get_json()
    checks.append((
        "legacy v3 whole box", 200,
        f"min={whole['min_wet']} max={whole['max_wet']} exact={whole['exact']} "
        f"known_wet={whole['known_wet']} unknown={whole['unknown_cells']}",
    ))
    corner = legacy.get(
        "/api/history/wet?version=3&row_start=5&row_end=10&col_start=5&col_end=10"
    ).get_json()
    checks.append((
        "legacy v3 unknown corner", 200,
        f"min={corner['min_wet']} max={corner['max_wet']} exact={corner['exact']}",
    ))
    known = legacy.get(
        "/api/history/wet?version=3&row_start=0&row_end=5&col_start=0&col_end=10"
    ).get_json()
    checks.append((
        "legacy v3 known-only box", 200,
        f"min={known['min_wet']} max={known['max_wet']} exact={known['exact']}",
    ))

    # A later full re-sweep must not change the frozen upgrade-time verdicts.
    legacy.post(
        "/api/packages",
        data=json.dumps({
            "version": 9,
            "coverage": {"row_start": 0, "row_end": 10, "col_start": 0, "col_end": 10},
            "wet_cells": [],
        }),
        content_type="application/json",
    )
    after = legacy.get(
        "/api/history/wet?version=3&row_start=0&row_end=10&col_start=0&col_end=10"
    ).get_json()
    frozen = (
        after["min_wet"] == whole["min_wet"]
        and after["max_wet"] == whole["max_wet"]
        and after["unknown_cells"] == whole["unknown_cells"]
        and after["known_wet"] == whole["known_wet"]
    )
    checks.append(("legacy v3 after v9 resweep", 200, f"frozen={frozen}"))
    return report(checks)


def report(checks) -> int:
    failed = 0
    for name, code, value in checks:
        print(f"{name:30} HTTP {code:<4} {value}")
        if code >= 400 and name != "v7-conflict.json":
            failed += 1
        if isinstance(value, str) and value == "frozen=False":
            failed += 1
    if failed:
        print(f"\n{failed} unexpected result(s).")
    else:
        print("\nAll demo steps behaved as expected.")
    return 1 if failed else 0


if __name__ == "__main__":
    url = os.environ.get("FLOOD_DEMO_URL")
    sys.exit(via_url(url.rstrip("/")) if url else via_test_client())

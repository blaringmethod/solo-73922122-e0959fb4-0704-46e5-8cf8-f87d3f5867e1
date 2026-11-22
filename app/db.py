"""SQLite connection helpers and schema for the flood-cell warehouse."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    version     INTEGER PRIMARY KEY,
    digest      TEXT    NOT NULL,
    r0          INTEGER NOT NULL,
    r1          INTEGER NOT NULL,
    c0          INTEGER NOT NULL,
    c1          INTEGER NOT NULL,
    wet_count   INTEGER NOT NULL CHECK (wet_count >= 0),
    imported_at TEXT    NOT NULL,
    historical  INTEGER NOT NULL DEFAULT 0,
    CHECK (r0 < r1),
    CHECK (c0 < c1)
);

-- Coverage is kept as rectangles only, never expanded to cells.  When SQLite
-- was built with R*Tree support we use it; otherwise a plain rectangle table
-- with indexes answers the same intersection queries.  Both have identical
-- column names so repository SQL is shared.
-- (created by init_db)

-- Materialised *current* wet cells.  There is deliberately no history table:
-- package content is resolved while ingesting, so rectangle queries never
-- rescan superseded water points.
CREATE TABLE IF NOT EXISTS current_wet (
    row     INTEGER NOT NULL,
    col     INTEGER NOT NULL,
    version INTEGER NOT NULL REFERENCES packages(version) ON DELETE CASCADE,
    PRIMARY KEY (row, col)
) WITHOUT ROWID;

-- Original wet points of every package, archived at ingest time.  New
-- packages keep every point they reported, even points a higher version had
-- already covered.  For packages that predate this feature the table holds
-- only the wet cells that were still determinable when the upgrade ran; the
-- cells then covered by a higher version stay unknown (see
-- package_unknown_rects).
CREATE TABLE IF NOT EXISTS package_wet_archive (
    package_version INTEGER NOT NULL REFERENCES packages(version) ON DELETE CASCADE,
    row             INTEGER NOT NULL,
    col             INTEGER NOT NULL,
    PRIMARY KEY (package_version, row, col)
) WITHOUT ROWID;

-- Frozen per-package snapshot of the area that higher-version packages
-- covered when the upgrade ran.  Only historical (pre-upgrade) packages get
-- rows here.  Rectangles are clipped to the package's own coverage and stored
-- as rectangles; the union (never a per-cell expansion) is the "unknown"
-- region for historical statistics.  Later re-surveys never touch these rows,
-- so the dry/wet verdicts that were knowable at upgrade time stay fixed.
CREATE TABLE IF NOT EXISTS package_unknown_rects (
    package_version INTEGER NOT NULL REFERENCES packages(version) ON DELETE CASCADE,
    r0              INTEGER NOT NULL,
    r1              INTEGER NOT NULL,
    c0              INTEGER NOT NULL,
    c1              INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS package_unknown_rects_idx
    ON package_unknown_rects (package_version);
"""

RTREE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS coverage_rects USING rtree_i32(
    version, r0, r1, c0, c1
)
"""

RECT_SQL = """
CREATE TABLE IF NOT EXISTS coverage_rects (
    version INTEGER PRIMARY KEY,
    r0      INTEGER NOT NULL,
    r1      INTEGER NOT NULL,
    c0      INTEGER NOT NULL,
    c1      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS coverage_rects_rows ON coverage_rects (r0, r1);
CREATE INDEX IF NOT EXISTS coverage_rects_cols ON coverage_rects (c0, c1);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with the pragmas this application relies on."""
    path = str(path)
    conn = sqlite3.connect(path, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        # Another process is switching the journal mode; not fatal, the
        # default rollback journal still works.
        pass
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection, prefer_rtree: bool = True) -> None:
    conn.executescript(SCHEMA)
    # The choice of coverage index is made once, when the database file is
    # first created; reopening an existing file reuses whatever is there.
    has_coverage = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'coverage_rects'"
    ).fetchone()
    if not has_coverage:
        if prefer_rtree:
            try:
                conn.executescript(RTREE_SQL)
            except sqlite3.OperationalError:
                conn.executescript(RECT_SQL)
        else:
            conn.executescript(RECT_SQL)

    # user_version 1: original schema (packages without ``historical`` and
    # without the archive / unknown-rectangle tables).  An upgrade freezes the
    # historical wet/dry knowledge before any new package can change it.
    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if user_version < 2:
        _upgrade_to_v2(conn)
    conn.execute("PRAGMA user_version = 2")


def _upgrade_to_v2(conn: sqlite3.Connection) -> None:
    """Add historical statistics support to a version-1 database.

    Everything runs in one write transaction: if any step fails, the original
    data is left untouched.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(packages)")}
    need_column = "historical" not in columns
    conn.execute("BEGIN IMMEDIATE")
    try:
        if need_column:
            conn.execute(
                "ALTER TABLE packages ADD COLUMN historical INTEGER NOT NULL DEFAULT 1"
            )
        # Every package already in the warehouse at upgrade time is historical.
        conn.execute("UPDATE packages SET historical = 1")

        # Freeze the wet cells that were still effective then.  A cell owned
        # by the package itself is a knowable "wet" verdict; cells owned by a
        # higher version lie inside an unknown rectangle instead.
        conn.execute(
            """
            INSERT INTO package_wet_archive (package_version, row, col)
            SELECT p.version, w.row, w.col
            FROM packages p
            JOIN current_wet w
              ON w.version = p.version
             AND w.row >= p.r0 AND w.row < p.r1
             AND w.col >= p.c0 AND w.col < p.c1
            """
        )

        # Freeze the higher-version coverage as clipped rectangles.  Their
        # union is exactly the region where the old package's own verdict was
        # no longer knowable at upgrade time.
        higher = conn.execute(
            """
            SELECT p.version AS pkg_version,
                   MAX(t.r0, p.r0) AS x0, MIN(t.r1, p.r1) AS x1,
                   MAX(t.c0, p.c0) AS y0, MIN(t.c1, p.c1) AS y1
            FROM packages p
            JOIN coverage_rects t
              ON t.version > p.version
             AND t.r0 < p.r1 AND t.r1 > p.r0
             AND t.c0 < p.c1 AND t.c1 > p.c0
            GROUP BY p.version, x0, x1, y0, y1
            """
        ).fetchall()
        conn.executemany(
            "INSERT INTO package_unknown_rects (package_version, r0, r1, c0, c1) "
            "VALUES (?, ?, ?, ?, ?)",
            [(r["pkg_version"], r["x0"], r["x1"], r["y0"], r["y1"]) for r in higher],
        )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _schema_present(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'packages'"
        ).fetchone()
        is not None
    )


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        path = current_app.config["DATABASE"]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = connect(path)
        # If the database file was removed or replaced underneath a running
        # process, recreate the schema instead of failing every request.
        if not _schema_present(conn):
            init_db(conn, prefer_rtree=current_app.config.get("PREFER_RTREE", True))
        g.db = conn
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()

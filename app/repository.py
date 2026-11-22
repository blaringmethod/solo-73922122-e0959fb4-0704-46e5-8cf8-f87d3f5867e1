"""Persistence and query logic.

Design notes
------------
* ``coverage_rects`` stores one row per package rectangle.  Coverage is never
  expanded into individual cells, so a package spanning millions of cells
  costs a handful of bytes.
* ``current_wet`` holds only the *currently effective* wet cells.  Packages are
  resolved against each other on write, so a box query is an indexed range scan
  over current state and never replays historical water points.
* Every import runs in a single ``BEGIN IMMEDIATE`` transaction.  A rejected
  package leaves both coverage and current wet state exactly as they were.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from .parsing import Coverage, Package


class ConflictError(Exception):
    """Same version number, different content."""


class PackageNotFoundError(Exception):
    """A statistics query named a version that was never ingested."""


@dataclass(frozen=True)
class Box:
    row_start: int
    row_end: int
    col_start: int
    col_end: int

    def as_dict(self) -> dict:
        return {
            "row_start": self.row_start,
            "row_end": self.row_end,
            "col_start": self.col_start,
            "col_end": self.col_end,
        }

    @property
    def area(self) -> int:
        return (self.row_end - self.row_start) * (self.col_end - self.col_start)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _max_version(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM packages").fetchone()[0]


def _newer_rects_in(
    conn: sqlite3.Connection, coverage: Coverage, than_version: int
) -> list[tuple[int, int, int, int]]:
    """Coverage rectangles newer than ``than_version`` that intersect ``coverage``."""
    rows = conn.execute(
        """
        SELECT r0, r1, c0, c1 FROM coverage_rects
        WHERE r0 < ? AND r1 > ?
          AND c0 < ? AND c1 > ?
          AND version > ?
        """,
        (
            coverage.row_end,
            coverage.row_start,
            coverage.col_end,
            coverage.col_start,
            than_version,
        ),
    ).fetchall()
    return [(r["r0"], r["r1"], r["c0"], r["c1"]) for r in rows]


def _shadowed(row: int, col: int, rects: list[tuple[int, int, int, int]]) -> bool:
    return any(r0 <= row < r1 and c0 <= col < c1 for r0, r1, c0, c1 in rects)


@contextmanager
def _transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")


def ingest(conn: sqlite3.Connection, package: Package) -> dict:
    """Apply ``package`` atomically.

    Returns a summary with ``status`` one of ``imported`` or ``unchanged``.
    Raises :class:`ConflictError` for a version collision with different
    content, and lets validation / SQLite errors propagate.  Because the whole
    write happens in one transaction, a failure leaves the previous coverage
    and wet state untouched.
    """
    coverage = package.coverage
    max_before = _max_version(conn)

    with _transaction(conn):
        existing = conn.execute(
            "SELECT digest FROM packages WHERE version = ?", (package.version,)
        ).fetchone()
        if existing is not None:
            if existing["digest"] == package.digest:
                return {
                    "status": "unchanged",
                    "version": package.version,
                    "digest": package.digest,
                    "max_version": max_before,
                    "message": "同版同内容重传，结果未改变。",
                }
            raise ConflictError(
                f"版号 {package.version} 已存在且内容不同，整包退回。"
            )

        conn.execute(
            """
            INSERT INTO packages
                (version, digest, r0, r1, c0, c1, wet_count, imported_at, historical)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                package.version,
                package.digest,
                coverage.row_start,
                coverage.row_end,
                coverage.col_start,
                coverage.col_end,
                len(package.wet_cells),
                _now(),
            ),
        )
        # Coverage stays a rectangle.  Only the R*Tree index sees it.
        conn.execute(
            "INSERT INTO coverage_rects (version, r0, r1, c0, c1) VALUES (?, ?, ?, ?, ?)",
            (
                package.version,
                coverage.row_start,
                coverage.row_end,
                coverage.col_start,
                coverage.col_end,
            ),
        )

        # Archive the package's own reported points together with the ingest,
        # in the same transaction.  Every reported point is kept, including
        # ones a higher version has already covered; the archive is what makes
        # historical statistics for new packages an exact answer.
        conn.executemany(
            "INSERT INTO package_wet_archive (package_version, row, col) "
            "VALUES (?, ?, ?)",
            [(package.version, row, col) for row, col in package.wet_cells],
        )

        # This package now owns the whole rectangle.  Any current wet cell in
        # it left by an older-or-equal version is superseded.
        removed = conn.execute(
            """
            DELETE FROM current_wet
            WHERE row >= ? AND row < ?
              AND col >= ? AND col < ?
              AND version <= ?
            """,
            (
                coverage.row_start,
                coverage.row_end,
                coverage.col_start,
                coverage.col_end,
                package.version,
            ),
        ).rowcount

        inserted = 0
        shadowed = 0
        newer = _newer_rects_in(conn, coverage, package.version)
        # Rows still in the rectangle after the delete above belong to a newer
        # version that only partially overlaps.  Those verdicts win.
        newer_cells = {
            (r["row"], r["col"])
            for r in conn.execute(
                """
                SELECT row, col FROM current_wet
                WHERE row >= ? AND row < ?
                  AND col >= ? AND col < ?
                """,
                (
                    coverage.row_start,
                    coverage.row_end,
                    coverage.col_start,
                    coverage.col_end,
                ),
            )
        }
        rows_to_insert = []
        for row, col in package.wet_cells:
            if (row, col) in newer_cells or _shadowed(row, col, newer):
                # A newer survey already checked this cell; the late older
                # package must not overwrite the newer verdict.
                shadowed += 1
                continue
            rows_to_insert.append((row, col, package.version))
        if rows_to_insert:
            conn.executemany(
                "INSERT INTO current_wet (row, col, version) VALUES (?, ?, ?) "
                "ON CONFLICT(row, col) DO UPDATE SET version = excluded.version",
                rows_to_insert,
            )
            inserted = len(rows_to_insert)

    return {
        "status": "imported",
        "version": package.version,
        "digest": package.digest,
        "max_version": _max_version(conn),
        "late_arrival": package.version < max_before,
        "warning": package.warning,
        "summary": {
            "coverage": coverage_dict(coverage),
            "coverage_area": coverage.area,
            "listed_wet_points": len(package.wet_cells),
            "current_wet_added": inserted,
            "current_wet_removed": removed,
            "wet_points_shadowed_by_newer": shadowed,
        },
    }


def coverage_dict(coverage: Coverage) -> dict:
    return {
        "row_start": coverage.row_start,
        "row_end": coverage.row_end,
        "col_start": coverage.col_start,
        "col_end": coverage.col_end,
    }


def query_wet(conn: sqlite3.Connection, box: Box, limit: int) -> dict:
    """Current wet cells inside ``box`` (half-open)."""
    rows = conn.execute(
        """
        SELECT row, col, version
        FROM current_wet
        WHERE row >= ? AND row < ?
          AND col >= ? AND col < ?
        ORDER BY row, col
        LIMIT ?
        """,
        (box.row_start, box.row_end, box.col_start, box.col_end, limit + 1),
    ).fetchall()
    truncated = len(rows) > limit
    cells = [
        {"row": r["row"], "col": r["col"], "version": r["version"]}
        for r in rows[:limit]
    ]
    return {
        "box": box.as_dict(),
        "cells": cells,
        "count": len(cells),
        "truncated": truncated,
        "limit": limit,
    }


def coverage_in_box(conn: sqlite3.Connection, box: Box) -> list[dict]:
    """Survey rectangles intersecting ``box``; used only for UI painting."""
    rows = conn.execute(
        """
        SELECT t.version, t.r0, t.r1, t.c0, t.c1
        FROM coverage_rects t
        WHERE t.r0 < ? AND t.r1 > ?
          AND t.c0 < ? AND t.c1 > ?
        ORDER BY t.version
        """,
        (box.row_end, box.row_start, box.col_end, box.col_start),
    ).fetchall()
    return [
        {
            "version": r["version"],
            "row_start": r["r0"],
            "row_end": r["r1"],
            "col_start": r["c0"],
            "col_end": r["c1"],
        }
        for r in rows
    ]


def cell_status(conn: sqlite3.Connection, row: int, col: int) -> dict:
    """Return wet / dry / unsurveyed for one cell.

    ``dry`` means the cell is inside at least one survey rectangle but is not
    a current wet cell.  ``unsurveyed`` means no package ever covered it.
    """
    wet = conn.execute(
        "SELECT version FROM current_wet WHERE row = ? AND col = ?", (row, col)
    ).fetchone()
    if wet is not None:
        return {
            "row": row,
            "col": col,
            "status": "wet",
            "version": wet["version"],
            "label": "有积水",
        }

    cover = conn.execute(
        """
        SELECT version FROM coverage_rects
        WHERE r0 <= ? AND r1 > ? AND c0 <= ? AND c1 > ?
        ORDER BY version DESC
        LIMIT 1
        """,
        (row, row, col, col),
    ).fetchone()
    if cover is not None:
        # The highest covering version is the one that produced this verdict.
        # If an even higher dry package covered the cell it would be the max.
        return {
            "row": row,
            "col": col,
            "status": "dry",
            "version": cover["version"],
            "label": "已查无积水",
        }
    return {
        "row": row,
        "col": col,
        "status": "unsurveyed",
        "version": None,
        "label": "未巡查",
    }


def list_packages(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        SELECT version, digest, r0, r1, c0, c1, wet_count, historical, imported_at
        FROM packages
        ORDER BY version DESC
        """
    ).fetchall()
    current_counts = dict(
        conn.execute(
            "SELECT version, COUNT(*) FROM current_wet GROUP BY version"
        ).fetchall()
    )
    archive_counts = dict(
        conn.execute(
            "SELECT package_version, COUNT(*) FROM package_wet_archive GROUP BY package_version"
        ).fetchall()
    )
    total_current_wet = sum(current_counts.values())
    return {
        "packages": [
            {
                "version": r["version"],
                "digest": r["digest"],
                "coverage": {
                    "row_start": r["r0"],
                    "row_end": r["r1"],
                    "col_start": r["c0"],
                    "col_end": r["c1"],
                },
                "listed_wet_points": r["wet_count"],
                "current_wet_points": current_counts.get(r["version"], 0),
                "archived_wet_points": archive_counts.get(r["version"], 0),
                "historical": bool(r["historical"]),
                "imported_at": r["imported_at"],
            }
            for r in rows
        ],
        "max_version": _max_version(conn),
        "total_current_wet": total_current_wet,
    }


# ---------------------------------------------------------------------------
# Historical box statistics
# ---------------------------------------------------------------------------


def _intersect(
    a_r0: int, a_r1: int, a_c0: int, a_c1: int, box: Box
) -> tuple[int, int, int, int] | None:
    """Intersection of a rectangle with ``box``; ``None`` when they do not meet."""
    r0 = max(a_r0, box.row_start)
    r1 = min(a_r1, box.row_end)
    c0 = max(a_c0, box.col_start)
    c1 = min(a_c1, box.col_end)
    if r0 < r1 and c0 < c1:
        return r0, r1, c0, c1
    return None


def union_area(rects: list[tuple[int, int, int, int]]) -> int:
    """Area of the union of axis-aligned half-open rectangles.

    Rectangles stay rectangles all the way: a sweep line merges column
    intervals per row band, so coverage spanning millions of cells is never
    expanded into individual cells.
    """
    if not rects:
        return 0
    edges: list[tuple[int, int, int, int]] = []
    for r0, r1, c0, c1 in rects:
        edges.append((r0, 1, c0, c1))
        edges.append((r1, -1, c0, c1))
    edges.sort(key=lambda e: (e[0], e[1]))

    def merged_span(intervals: list[tuple[int, int]]) -> int:
        if not intervals:
            return 0
        ordered = sorted(intervals)
        span = 0
        lo, hi = ordered[0]
        for c0, c1 in ordered[1:]:
            if c0 <= hi:
                hi = max(hi, c1)
            else:
                span += hi - lo
                lo, hi = c0, c1
        return span + (hi - lo)

    active: list[tuple[int, int]] = []
    total = 0
    prev_row = edges[0][0]
    for row, kind, c0, c1 in edges:
        if row > prev_row:
            total += (row - prev_row) * merged_span(active)
            prev_row = row
        if kind == 1:
            active.append((c0, c1))
        else:
            active.remove((c0, c1))
    return total


def _unknown_rects(
    conn: sqlite3.Connection, version: int
) -> list[tuple[int, int, int, int]]:
    rows = conn.execute(
        "SELECT r0, r1, c0, c1 FROM package_unknown_rects WHERE package_version = ?",
        (version,),
    ).fetchall()
    return [(r["r0"], r["r1"], r["c0"], r["c1"]) for r in rows]


def _archived_cells(
    conn: sqlite3.Connection, version: int, box: Box, limit: int
) -> tuple[list[dict], bool]:
    rows = conn.execute(
        """
        SELECT row, col FROM package_wet_archive
        WHERE package_version = ?
          AND row >= ? AND row < ?
          AND col >= ? AND col < ?
        ORDER BY row, col
        LIMIT ?
        """,
        (
            version,
            box.row_start,
            box.row_end,
            box.col_start,
            box.col_end,
            limit + 1,
        ),
    ).fetchall()
    truncated = len(rows) > limit
    return (
        [{"row": r["row"], "col": r["col"]} for r in rows[:limit]],
        truncated,
    )


def historical_box_statistics(
    conn: sqlite3.Connection, version: int, box: Box, point_limit: int
) -> dict:
    """Possible wet-cell count of one package inside a work-area rectangle.

    New packages (ingested after the upgrade) keep every reported point in the
    archive, so the answer is exact.  Historical packages only retain the
    dry/wet verdicts that were knowable at upgrade time: cells then covered by
    a higher version are unknown, and the package's original reported total
    bounds how many wet cells can still hide among them.
    """
    package = conn.execute(
        "SELECT r0, r1, c0, c1, wet_count, historical FROM packages WHERE version = ?",
        (version,),
    ).fetchone()
    if package is None:
        raise PackageNotFoundError(f"版号 {version} 尚未入仓。")

    overlap = _intersect(
        package["r0"], package["r1"], package["c0"], package["c1"], box
    )
    listed_total = package["wet_count"]
    if overlap is None:
        return _statistics_payload(
            version=version,
            box=box,
            overlap=None,
            historical=bool(package["historical"]),
            listed_total=listed_total,
            min_wet=0,
            max_wet=0,
            unknown_area=0,
            known_dry=0,
            known_wet=0,
            unknown_rects=[],
            archived=[],
            archived_truncated=False,
        )

    overlap_box = Box(*overlap)
    overlap_area = overlap_box.area
    archived, archived_truncated = _archived_cells(
        conn, version, overlap_box, point_limit
    )

    def archived_count(within: Box) -> int:
        return conn.execute(
            """
            SELECT COUNT(*) FROM package_wet_archive
            WHERE package_version = ?
              AND row >= ? AND row < ?
              AND col >= ? AND col < ?
            """,
            (
                version,
                within.row_start,
                within.row_end,
                within.col_start,
                within.col_end,
            ),
        ).fetchone()[0]

    if not package["historical"]:
        # Full archive: every reported point survived, so the count is exact
        # even if a higher version had already covered this package at ingest.
        exact_count = archived_count(overlap_box)
        return _statistics_payload(
            version=version,
            box=box,
            overlap=overlap_box,
            historical=False,
            listed_total=listed_total,
            min_wet=exact_count,
            max_wet=exact_count,
            unknown_area=0,
            known_dry=overlap_area - exact_count,
            known_wet=exact_count,
            unknown_rects=[],
            archived=archived,
            archived_truncated=archived_truncated,
        )

    # Historical package.  The unknown region is the frozen union of the
    # higher-version coverage clipped to this package's rectangle.
    frozen_unknown = _unknown_rects(conn, version)
    unknown_in_overlap = [
        rect
        for rect in (
            _intersect(r0, r1, c0, c1, overlap_box)
            for r0, r1, c0, c1 in frozen_unknown
        )
        if rect is not None
    ]
    unknown_area = union_area(unknown_in_overlap)

    # Archived rows for a historical package hold only the wet cells that were
    # still effective at upgrade time; none lies in an unknown rectangle.
    known_wet = archived_count(overlap_box)
    known_dry = overlap_area - unknown_area - known_wet

    # The reported total ``T`` is distributed over the whole package.  Of the
    # reports hidden in the unknown region, at most ``unknown_outside`` of
    # them can lie outside the work area, so the rest must be wet inside it.
    coverage_area = (package["r1"] - package["r0"]) * (
        package["c1"] - package["c0"]
    )
    unknown_total = coverage_area - union_area(frozen_unknown)
    unknown_outside = unknown_total - (overlap_area - unknown_area)
    wet_outside = conn.execute(
        """
        SELECT COUNT(*) FROM package_wet_archive
        WHERE package_version = ?
          AND NOT (row >= ? AND row < ? AND col >= ? AND col < ?)
        """,
        (
            version,
            overlap_box.row_start,
            overlap_box.row_end,
            overlap_box.col_start,
            overlap_box.col_end,
        ),
    ).fetchone()[0]
    # Archived wet points are knowable, so every un-archived reported point is
    # somewhere in the unknown region.
    archived_total = conn.execute(
        "SELECT COUNT(*) FROM package_wet_archive WHERE package_version = ?",
        (version,),
    ).fetchone()[0]
    reports_in_unknown = listed_total - archived_total

    remaining_reports = listed_total - wet_outside
    min_wet = known_wet + max(0, reports_in_unknown - unknown_outside)
    max_wet = known_wet + min(unknown_area, remaining_reports)
    if min_wet > max_wet:  # defensive only; the data model forbids this
        min_wet = max_wet

    return _statistics_payload(
        version=version,
        box=box,
        overlap=overlap_box,
        historical=True,
        listed_total=listed_total,
        min_wet=min_wet,
        max_wet=max_wet,
        unknown_area=unknown_area,
        known_dry=known_dry,
        known_wet=known_wet,
        unknown_rects=[
            {
                "row_start": r0,
                "row_end": r1,
                "col_start": c0,
                "col_end": c1,
            }
            for r0, r1, c0, c1 in unknown_in_overlap
        ],
        archived=archived,
        archived_truncated=archived_truncated,
        reasoning={
            "wet_outside_intersection": wet_outside,
            "unknown_outside_intersection": unknown_outside,
            "reported_points_in_unknown_region": reports_in_unknown,
            "remaining_reported_points": remaining_reports,
        },
    )


def _statistics_payload(
    *,
    version: int,
    box: Box,
    overlap: Box | None,
    historical: bool,
    listed_total: int,
    min_wet: int,
    max_wet: int,
    unknown_area: int,
    known_dry: int,
    known_wet: int,
    unknown_rects: list[dict],
    archived: list[dict],
    archived_truncated: bool,
    reasoning: dict | None = None,
) -> dict:
    exact = min_wet == max_wet
    return {
        "version": version,
        "box": box.as_dict(),
        "intersection": overlap.as_dict() if overlap is not None else None,
        "historical": historical,
        "listed_wet_total": listed_total,
        "min_wet": min_wet,
        "max_wet": max_wet,
        "exact": exact,
        "count": min_wet if exact else None,
        "known_wet": known_wet,
        "known_dry": known_dry,
        "unknown_cells": unknown_area,
        "unknown_rects": unknown_rects,
        "archived_cells": archived,
        "archived_truncated": archived_truncated,
        "reasoning": reasoning or {},
    }

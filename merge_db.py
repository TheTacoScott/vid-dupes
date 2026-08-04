#!/usr/bin/env python3
"""Merge scanned video databases (videos + frame_hashes) into a single target DB."""

import argparse
import sqlite3
import sys
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id          INTEGER PRIMARY KEY,
    path        TEXT    UNIQUE NOT NULL,
    md5         TEXT    NOT NULL,
    duration    REAL,
    file_size   INTEGER,
    width       INTEGER,
    height      INTEGER,
    codec       TEXT,
    scanned_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS frame_hashes (
    id          INTEGER PRIMARY KEY,
    md5         TEXT    NOT NULL,
    frame_index INTEGER NOT NULL,
    phash       INTEGER NOT NULL,
    UNIQUE(md5, frame_index)
);

CREATE INDEX IF NOT EXISTS idx_videos_md5      ON videos(md5);
CREATE INDEX IF NOT EXISTS idx_videos_duration ON videos(duration);
CREATE INDEX IF NOT EXISTS idx_fh_md5          ON frame_hashes(md5);
"""


def open_source(path: str) -> sqlite3.Connection:
    if not Path(path).exists():
        print(f"error: source DB not found: {path}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def open_target(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def merge_source(src: sqlite3.Connection, tgt: sqlite3.Connection, src_path: str, dry_run: bool) -> dict:
    counts = {'added': 0, 'skipped': 0, 'no_hashes': 0}

    # Load existing MD5s in target for fast lookup
    existing_md5s = {
        row[0] for row in tgt.execute("SELECT md5 FROM videos")
    }

    videos = src.execute(
        "SELECT id, path, md5, duration, file_size, width, height, codec, scanned_at FROM videos"
    ).fetchall()

    for v in videos:
        src_id = v['id']
        md5 = v['md5']

        if md5 in existing_md5s:
            counts['skipped'] += 1
            continue

        hashes = src.execute(
            "SELECT frame_index, phash FROM frame_hashes WHERE md5 = ? ORDER BY frame_index",
            (md5,)
        ).fetchall()

        if not hashes:
            counts['no_hashes'] += 1
            continue

        if not dry_run:
            tgt.execute(
                """INSERT INTO videos (path, md5, duration, file_size, width, height, codec, scanned_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (v['path'], md5, v['duration'], v['file_size'],
                 v['width'], v['height'], v['codec'], v['scanned_at']),
            )
            tgt.executemany(
                "INSERT OR IGNORE INTO frame_hashes (md5, frame_index, phash) VALUES (?, ?, ?)",
                [(md5, h['frame_index'], h['phash']) for h in hashes],
            )
            existing_md5s.add(md5)

        counts['added'] += 1

    if not dry_run:
        tgt.commit()

    return counts


def main():
    parser = argparse.ArgumentParser(
        description="Merge one or more scan databases into a target database."
    )
    parser.add_argument('sources', nargs='+', metavar='SOURCE_DB',
                        help="Source database(s) to merge from")
    parser.add_argument('--target', required=True, metavar='DB',
                        help="Target database to merge into (created if absent)")
    parser.add_argument('--dry-run', action='store_true',
                        help="Show what would be merged without writing anything")
    args = parser.parse_args()

    if args.dry_run:
        print("Dry run — no changes will be written.")

    tgt = open_target(args.target)

    total = {'added': 0, 'skipped': 0, 'no_hashes': 0}

    for src_path in args.sources:
        print(f"\nMerging {src_path} ...")
        src = open_source(src_path)
        counts = merge_source(src, tgt, src_path, args.dry_run)
        src.close()
        print(
            f"  Added: {counts['added']}  "
            f"Skipped (already present): {counts['skipped']}  "
            f"Skipped (no hashes): {counts['no_hashes']}"
        )
        for k in total:
            total[k] += counts[k]

    tgt.close()

    if len(args.sources) > 1:
        print(
            f"\nTotal — Added: {total['added']}  "
            f"Skipped (already present): {total['skipped']}  "
            f"Skipped (no hashes): {total['no_hashes']}"
        )

    if args.dry_run:
        print("\nDry run complete. Re-run without --dry-run to apply.")


if __name__ == '__main__':
    main()

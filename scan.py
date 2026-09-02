#!/usr/bin/env python3
"""Scan video files and catalog metadata + perceptual hashes into SQLite."""

import argparse
import collections
import os
import random
import concurrent.futures
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import imagehash
from PIL import Image

_MD5_IN_FILENAME = re.compile(r'\[([0-9a-f]{32})\]', re.IGNORECASE)

VIDEO_EXTENSIONS = {
    '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv',
    '.webm', '.m4v', '.ts', '.m2ts', '.mpg', '.mpeg', '.vob',
}
FRAME_SIZE         = 64  # pixels, square; phash operates on this
FRAME_IDLE_TIMEOUT = 60  # seconds; per-seek ffmpeg timeout
FFMPEG_WORKERS     = 4   # concurrent ffmpeg seek processes
HIGH_RES_PIXELS    = 3840 * 2160  # 4K UHD; videos at/above this run seeks one at a time

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


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def get_md5(path: str) -> str:
    """Return MD5 from filename if encoded as [<hex32>], otherwise compute from file content."""
    m = _MD5_IN_FILENAME.search(Path(path).name)
    if m:
        return m.group(1).lower()
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def probe_video(path: str) -> dict | None:
    cmd = [
        'ffprobe', '-v', 'quiet', '-print_format', 'json',
        '-show_streams', '-show_format', path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=30)
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        vstream = next(
            (s for s in data.get('streams', []) if s.get('codec_type') == 'video'),
            None,
        )
        if not vstream:
            return None
        duration = float(data['format'].get('duration') or vstream.get('duration') or 0)
        if duration <= 0:
            return None

        # Total frame count: prefer nb_frames from stream, fall back to duration * fps
        nb_frames = int(vstream.get('nb_frames') or 0)
        if nb_frames <= 0:
            fps_str = vstream.get('r_frame_rate', '0/1')
            try:
                num, den = fps_str.split('/')
                fps = float(num) / float(den) if float(den) else 0
            except (ValueError, ZeroDivisionError):
                fps = 0
            nb_frames = int(duration * fps) if fps > 0 else 0

        return {
            'duration':  duration,
            'nb_frames': nb_frames,
            'width':     vstream.get('width'),
            'height':    vstream.get('height'),
            'codec':     vstream.get('codec_name'),
        }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def fmt_eta(secs: float) -> str:
    if secs < 60:
        return f"{int(secs)}s"
    elif secs < 3600:
        m, s = divmod(int(secs), 60)
        return f"{m}m {s:02d}s"
    else:
        h, rem = divmod(int(secs), 3600)
        return f"{h}h {rem // 60:02d}m"


def _seek_one_frame(path: str, timestamp: float) -> bytes | None:
    """Seek to timestamp and extract one frame, returning raw RGB bytes or None."""
    frame_bytes = FRAME_SIZE * FRAME_SIZE * 3
    cmd = [
        'ffmpeg',
        '-accurate_seek', '-ss', f'{timestamp:.6f}',
        '-i', path,
        '-frames:v', '1',
        '-vf', f'scale={FRAME_SIZE}:{FRAME_SIZE}',
        '-f', 'rawvideo',
        '-pix_fmt', 'rgb24',
        '-loglevel', 'error',
        'pipe:1',
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, timeout=FRAME_IDLE_TIMEOUT)
        if r.returncode != 0 or len(r.stdout) < frame_bytes:
            return None
        return r.stdout[:frame_bytes]
    except subprocess.TimeoutExpired:
        return None


def extract_frame_hashes(path: str, duration: float, num_samples: int, workers: int = FFMPEG_WORKERS) -> list[int] | None:
    """
    Extract num_samples equally-spaced frames via parallel seeks and return their phash values.

    Up to `workers` seeks run concurrently. Progress is tracked as futures return;
    each individual seek is bounded by FRAME_IDLE_TIMEOUT.
    """
    interval = duration / num_samples if num_samples > 1 else duration
    timestamps = [(i + 0.5) * interval for i in range(num_samples)]

    frame_bytes = FRAME_SIZE * FRAME_SIZE * 3
    results: dict[int, int] = {}
    completed = 0
    start_time = time.monotonic()

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_idx = {
            executor.submit(_seek_one_frame, path, t): i
            for i, t in enumerate(timestamps)
        }

        for future in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[future]
            raw = future.result()

            if raw is not None:
                img = Image.frombytes('RGB', (FRAME_SIZE, FRAME_SIZE), raw)
                h = int(str(imagehash.phash(img)), 16)
                # SQLite INTEGER is signed 64-bit; store as two's complement.
                if h >= (1 << 63):
                    h -= (1 << 64)
                results[idx] = h

            completed += 1
            elapsed = time.monotonic() - start_time
            fps = completed / elapsed if elapsed > 0 else 0.0
            remaining = num_samples - completed
            file_eta = fmt_eta(remaining / fps) if fps > 0 and remaining > 0 else ''
            if completed == 1:
                print(file=sys.stderr)  # move past the stdout path line before first update
            eta_part = f'  eta {file_eta}' if file_eta else ''
            print(f'\r    frame {completed}/{num_samples}  ({fps:.1f} fps){eta_part}\033[K', end='', flush=True, file=sys.stderr)

    if results:
        print(file=sys.stderr)
        return [results[i] for i in sorted(results)]
    return None


def resolve_true_case(path: Path, listdir_cache: dict) -> Path | None:
    """Resolve an absolute path to its true on-disk casing, or None if missing.

    On a case-insensitive filesystem (e.g. a CIFS/SMB mount to a Windows
    share), Path.exists() can't distinguish "this exact path" from "some
    differently-cased path with the same name" -- both succeed, because the
    OS/server does the case-insensitive match transparently. That means a
    file renamed only by case (Foo.mp4 -> foo.mp4) leaves the old DB row
    looking like it still exists, so plain existence checks never clean it
    up. This walks each path component against the real directory listing
    to find the actual casing, so callers can detect the mismatch.

    `listdir_cache` is a dict the caller reuses across calls so shared parent
    directories are only listed once.
    """
    current = Path(path.root)
    for part in path.parts[1:]:
        if current in listdir_cache:
            entries = listdir_cache[current]
        else:
            try:
                entries = os.listdir(current)
            except OSError:
                entries = None
            listdir_cache[current] = entries
        if entries is None:
            return None
        if part not in entries:
            lower = part.lower()
            match = next((e for e in entries if e.lower() == lower), None)
            if match is None:
                return None
            part = match
        current = current / part
    return current


def find_video_files(paths: list[str], ignore: list[Path] | None = None):
    ignore = ignore or []
    for p in paths:
        p = Path(p)
        if p.is_file():
            if p.suffix.lower() in VIDEO_EXTENSIONS:
                if not ignore or not any(p.resolve().is_relative_to(ig) for ig in ignore):
                    yield p
        elif p.is_dir():
            for root, dirs, files in os.walk(p, topdown=True):
                rroot = Path(root).resolve()
                if ignore:
                    dirs[:] = [d for d in dirs
                               if not any((rroot / d).is_relative_to(ig) for ig in ignore)]
                for name in files:
                    if Path(name).suffix.lower() in VIDEO_EXTENSIONS:
                        yield Path(root) / name
        else:
            print(f"Warning: {p} does not exist, skipping.", file=sys.stderr)


def scan_file(conn: sqlite3.Connection, path: Path, num_frames: int, verbose: bool,
              workers: int = FFMPEG_WORKERS) -> str:
    """Scan one video. Returns: 'no_md5' | 'skipped' | 'added' | 'reused' | 'failed'"""
    if not _MD5_IN_FILENAME.search(path.name):
        return 'no_md5'

    path_str = str(path.resolve())
    file_size = path.stat().st_size
    current_md5 = get_md5(path_str)

    existing_row = conn.execute(
        "SELECT md5 FROM videos WHERE path = ?", (path_str,)
    ).fetchone()

    if existing_row:
        if current_md5 == existing_row[0]:
            return 'skipped'
        # MD5 in filename changed at same path — drop video record; hashes stay (prune separately)
        conn.execute("DELETE FROM videos WHERE path = ?", (path_str,))
        conn.commit()

    # Probe metadata (fast — no frame extraction)
    info = probe_video(path_str)
    if not info:
        if verbose:
            print(f"    could not probe (not a video or unreadable)", file=sys.stderr)
        return 'failed'

    # Only extract frames if no hashes exist for this md5
    has_hashes = conn.execute(
        "SELECT 1 FROM frame_hashes WHERE md5 = ? LIMIT 1", (current_md5,)
    ).fetchone() is not None

    if not has_hashes:
        effective_workers = workers
        if info['width'] and info['height'] and info['width'] * info['height'] >= HIGH_RES_PIXELS:
            effective_workers = 1
            if verbose:
                print(f"    {info['width']}x{info['height']} is high-res, using 1 worker", file=sys.stderr)
        hashes = extract_frame_hashes(path_str, info['duration'], num_frames, effective_workers)
        if not hashes:
            if verbose:
                print(f"    frame extraction failed", file=sys.stderr)
            return 'failed'

    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            """INSERT INTO videos (path, md5, duration, file_size, width, height, codec, scanned_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (path_str, current_md5, info['duration'], file_size,
             info['width'], info['height'], info['codec'], now),
        )
        if not has_hashes:
            conn.executemany(
                "INSERT INTO frame_hashes (md5, frame_index, phash) VALUES (?, ?, ?)",
                [(current_md5, i, h) for i, h in enumerate(hashes)],
            )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return 'skipped'

    return 'reused' if has_hashes else 'added'


def main():
    parser = argparse.ArgumentParser(
        description="Scan video files into a duplicate-detection SQLite database."
    )
    parser.add_argument('paths', nargs='*', help="Files or directories to scan")
    parser.add_argument('--db', default='videos.db',
                        help="SQLite database path (default: videos.db)")
    parser.add_argument('--frames', type=int, default=100,
                        help="Max frames to sample per video (default: 100); "
                             "videos with fewer total frames capture all frames")
    parser.add_argument('--prune-videos', action='store_true',
                        help="Remove video records for missing files (also reconciles "
                             "records left stale by case-only renames on case-insensitive "
                             "filesystems), then exit")
    parser.add_argument('--prune-hashes', action='store_true',
                        help="Remove frame hashes with no corresponding video record, then exit")
    parser.add_argument('--prune-all', action='store_true',
                        help="Run --prune-videos then --prune-hashes, then exit")
    parser.add_argument('--ignore', metavar='DIR', action='append', default=[],
                        help="Ignore files under this path (repeatable)")
    parser.add_argument('--shuffle', action='store_true',
                        help="Process files in random order instead of alphabetical")
    parser.add_argument('--workers', type=int, default=FFMPEG_WORKERS,
                        help=f"Concurrent ffmpeg seek processes per video (default: {FFMPEG_WORKERS})")
    parser.add_argument('-v', '--verbose', action='store_true',
                        help="Print per-file status details")
    args = parser.parse_args()

    conn = init_db(args.db)

    if args.prune_videos or args.prune_all:
        all_known = conn.execute("SELECT id, path FROM videos").fetchall()
        print(f"Checking {len(all_known)} video record(s)...")
        known_paths = {path for _, path in all_known}
        listdir_cache: dict = {}
        removed = 0
        fixed = 0
        for vid_id, path in all_known:
            true_path = resolve_true_case(Path(path), listdir_cache)
            if true_path is None:
                conn.execute("DELETE FROM videos WHERE id = ?", (vid_id,))
                removed += 1
                print(f"  removed (missing): {path}")
                continue
            true_str = str(true_path)
            if true_str == path:
                continue
            # Path exists case-insensitively but under a different real casing
            # -- almost certainly a case-only rename on a case-insensitive fs.
            if true_str in known_paths:
                # A record with the correct casing already exists; this one is stale.
                conn.execute("DELETE FROM videos WHERE id = ?", (vid_id,))
                removed += 1
                print(f"  removed (stale casing, superseded by {true_str}): {path}")
            else:
                conn.execute("UPDATE videos SET path = ? WHERE id = ?", (true_str, vid_id))
                known_paths.discard(path)
                known_paths.add(true_str)
                fixed += 1
                print(f"  fixed casing: {path} -> {true_str}")
        if removed or fixed:
            conn.commit()
        print(f"Removed {removed} missing/stale video record(s), fixed {fixed} casing mismatch(es).")
        if not args.prune_all:
            conn.close()
            return

    if args.prune_hashes or args.prune_all:
        orphan_count = conn.execute(
            "SELECT COUNT(DISTINCT md5) FROM frame_hashes WHERE md5 NOT IN (SELECT md5 FROM videos)"
        ).fetchone()[0]
        cur = conn.execute(
            "DELETE FROM frame_hashes WHERE md5 NOT IN (SELECT md5 FROM videos)"
        )
        conn.commit()
        conn.close()
        print(f"Pruned {orphan_count} orphaned hash set(s) ({cur.rowcount} row(s)).")
        return

    if not args.paths:
        parser.error("paths are required unless --prune-videos, --prune-hashes, or --prune-all is used")

    ignore_paths = [Path(p).resolve() for p in args.ignore]

    print("Scanning:")
    for p in args.paths:
        print(f"  {p}")
    if ignore_paths:
        print("Ignoring:")
        for p in ignore_paths:
            print(f"  {p}")
    print("Finding video files...")
    found = set()
    for f in find_video_files(args.paths, ignore_paths):
        found.add(f.resolve())
        if len(found) % 1000 == 0:
            print(f"  {len(found)} found so far...")
    videos = sorted(found)
    if args.shuffle:
        random.shuffle(videos)
    if not videos:
        print("No video files found.")
        return

    print(f"Found {len(videos)} video file(s). Scanning into {args.db} ...")

    counts = {'no_md5': 0, 'skipped': 0, 'added': 0, 'reused': 0, 'failed': 0}
    scan_times: collections.deque = collections.deque(maxlen=100)
    for i, path in enumerate(videos, 1):
        print(f"[{i}/{len(videos)}] {path} ... ", end='', flush=True)
        file_start = time.monotonic()
        status = scan_file(conn, path, args.frames, args.verbose, args.workers)
        counts[status] += 1
        if status not in ('skipped', 'reused', 'no_md5'):
            scan_times.append(time.monotonic() - file_start)
        if scan_times and i < len(videos):
            avg = sum(scan_times) / len(scan_times)
            run_eta = fmt_eta(avg * (len(videos) - i))
            eta_part = f'  [run eta {run_eta}]'
        else:
            eta_part = ''
        print(f'{status}{eta_part}')

    conn.close()
    print(
        f"\nDone. "
        f"Added: {counts['added']}  "
        f"Reused: {counts['reused']}  "
        f"Skipped: {counts['skipped']}  "
        f"No MD5: {counts['no_md5']}  "
        f"Failed: {counts['failed']}"
    )


if __name__ == '__main__':
    main()

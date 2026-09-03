#!/usr/bin/env python3
"""Query the video database for duplicate candidates based on duration + perceptual hash similarity."""

import argparse
import os
import re
import shlex
import sqlite3
import sys
from pathlib import Path

_MD5_RE = re.compile(r'\[[0-9a-f]{32}\]', re.IGNORECASE)

DURATION_WINDOW   = 2.0   # seconds — max duration difference between candidates
HAMMING_THRESHOLD = 25    # max mean Hamming distance across paired frame hashes
MIN_FRAMES        = 10    # minimum shared frame count to bother comparing


def load_videos(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT id, path, md5, duration, file_size, width, height
             FROM videos
            WHERE duration IS NOT NULL
            ORDER BY duration"""
    ).fetchall()
    return [
        {'id': r[0], 'path': r[1], 'md5': r[2], 'duration': r[3],
         'file_size': r[4], 'width': r[5], 'height': r[6]}
        for r in rows
    ]


def load_all_frame_hashes(conn: sqlite3.Connection) -> dict[str, list[int]]:
    """Load every frame hash into memory as integers, grouped by md5."""
    rows = conn.execute(
        "SELECT md5, phash FROM frame_hashes ORDER BY md5, frame_index"
    ).fetchall()
    result: dict[str, list[int]] = {}
    for md5, phash_int in rows:
        result.setdefault(md5, []).append(phash_int)
    return result


def load_non_matches(conn: sqlite3.Connection) -> set[frozenset]:
    """Return the set of confirmed non-match md5 pairs, or empty set if table absent."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='non_matches'"
    ).fetchone()
    if not exists:
        return set()
    rows = conn.execute("SELECT md5_a, md5_b FROM non_matches").fetchall()
    return {frozenset({a, b}) for a, b in rows}


def duration_candidate_pairs(videos: list[dict], window: float) -> list[tuple[dict, dict]]:
    """
    Return all pairs whose durations are within `window` seconds of each other.
    Videos must be sorted by duration ascending.
    Uses a sliding window — O(n + k) where k is the number of pairs returned.
    """
    pairs = []
    lo = 0
    for hi in range(len(videos)):
        while videos[hi]['duration'] - videos[lo]['duration'] > window:
            lo += 1
        for j in range(lo, hi):
            pairs.append((videos[j], videos[hi]))
    return pairs


def mean_hamming(hashes_a: list[int], hashes_b: list[int], threshold: float) -> float | None:
    n = min(len(hashes_a), len(hashes_b))
    if n == 0:
        return None
    limit = threshold * n
    running = 0
    for a, b in zip(hashes_a, hashes_b):
        # Mask to 64 bits before counting to handle signed storage correctly
        running += ((a ^ b) & 0xFFFFFFFFFFFFFFFF).bit_count()
        if running > limit:
            return None
    return running / n


def merge_groups(pairs: list[tuple[int, int]]) -> list[set[int]]:
    """Union-find style merge of overlapping id pairs into groups."""
    groups: list[set[int]] = []
    for a, b in pairs:
        to_merge = []
        for i, g in enumerate(groups):
            if a in g or b in g:
                to_merge.append(i)
        if not to_merge:
            groups.append({a, b})
        else:
            base = groups[to_merge[0]]
            base.add(a)
            base.add(b)
            for i in reversed(to_merge[1:]):
                base |= groups.pop(i)
    return groups


def fmt_size(n: int | None) -> str:
    if n is None:
        return '?'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_pair_stats(pair_stats: dict, keep_id: int, vid_id: int, group: set[int]) -> str:
    """Return inline stats string for a DELETE video.

    Prefers the direct pair vs KEEP; falls back to any direct pair within the
    group for videos that are only transitively connected to KEEP.
    """
    s = pair_stats.get(frozenset({keep_id, vid_id}))
    transitive = s is None
    if transitive:
        for k, v in pair_stats.items():
            if vid_id in k and k <= group:
                s = v
                break
    if s is None:
        return ''
    h = 'md5' if s['match_type'] == 'md5' else f"{s['hamming']:.1f}"
    suffix = '  [transitive]' if transitive else ''
    return f"  hamming {h}  Δdur {s['dur_diff']:.2f}s{suffix}"


def fmt_duration(secs: float | None) -> str:
    if secs is None:
        return '?'
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = secs % 60
    if h:
        return f"{h}h{m:02d}m{s:05.2f}s"
    return f"{m}m{s:05.2f}s"


def main():
    parser = argparse.ArgumentParser(
        description="Find duplicate videos in the scan database."
    )
    parser.add_argument('--db', default='videos.db',
                        help="SQLite database path (default: videos.db)")
    parser.add_argument('--duration-window', type=float, default=DURATION_WINDOW,
                        help=f"Max duration difference in seconds (default: {DURATION_WINDOW})")
    parser.add_argument('--hamming', type=float, default=HAMMING_THRESHOLD,
                        help=f"Max mean Hamming distance across frames (default: {HAMMING_THRESHOLD})")
    parser.add_argument('--min-frames', type=int, default=MIN_FRAMES,
                        help=f"Minimum overlapping frame count to compare (default: {MIN_FRAMES})")
    parser.add_argument('--prefix', metavar='DIR',
                        help="Only consider videos whose path starts with DIR")
    parser.add_argument('--script', metavar='FILE',
                        help="Write a reviewable bash delete script to FILE (use - for stdout)")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)

    videos = load_videos(conn)
    if not videos:
        print("No videos in database. Run scan.py first.")
        sys.exit(0)

    print(f"Loaded {len(videos)} videos.", file=sys.stderr)

    all_hashes = load_all_frame_hashes(conn)
    excluded_pairs = load_non_matches(conn)
    conn.close()

    if excluded_pairs:
        print(f"Loaded {len(excluded_pairs)} non-match exclusion(s).", file=sys.stderr)

    pairs = duration_candidate_pairs(videos, args.duration_window)
    print(f"Duration candidates: {len(pairs)} pair(s) to compare.", file=sys.stderr)

    dupe_pairs: list[tuple[int, int]] = []
    pair_stats: dict[frozenset, dict] = {}

    for a, b in pairs:
        dur_diff = abs(a['duration'] - b['duration'])

        if a['md5'] == b['md5']:
            key = frozenset({a['id'], b['id']})
            dupe_pairs.append((a['id'], b['id']))
            pair_stats[key] = {'match_type': 'md5', 'hamming': None, 'dur_diff': dur_diff}
            continue

        if a['md5'] and b['md5'] and frozenset({a['md5'], b['md5']}) in excluded_pairs:
            continue

        ha = all_hashes.get(a['md5'], [])
        hb = all_hashes.get(b['md5'], [])
        if len(ha) < args.min_frames or len(hb) < args.min_frames:
            continue

        dist = mean_hamming(ha, hb, args.hamming)
        if dist is not None and dist <= args.hamming:
            key = frozenset({a['id'], b['id']})
            dupe_pairs.append((a['id'], b['id']))
            pair_stats[key] = {'match_type': 'phash', 'hamming': dist, 'dur_diff': dur_diff}

    if not dupe_pairs:
        print("No duplicates found.")
        sys.exit(0)

    groups = merge_groups(dupe_pairs)
    video_by_id = {v['id']: v for v in videos}

    if args.prefix:
        prefix = str(Path(args.prefix).resolve()).lower()
        groups = [g for g in groups if any(video_by_id[vid]['path'].lower().startswith(prefix) for vid in g)]

    def group_members(group):
        return sorted(
            [video_by_id[vid] for vid in group],
            key=lambda v: ((v['width'] or 0) * (v['height'] or 0), v['file_size'] or 0),
            reverse=True,
        )

    def group_sort_key(group):
        direct = [v for k, v in pair_stats.items() if k <= group]
        if not direct:
            return (float('inf'), float('inf'))
        min_hamming = min(0.0 if s['match_type'] == 'md5' else s['hamming'] for s in direct)
        min_dur = min(s['dur_diff'] for s in direct)
        return (min_hamming, min_dur)

    groups.sort(key=group_sort_key)

    print(f"Found {len(groups)} duplicate group(s).\n")

    for gi, group in enumerate(groups, 1):
        members = group_members(group)
        keep = members[0]
        print(f"=== Group {gi} ===")
        for i, v in enumerate(members):
            res = f"{v['width']}x{v['height']}" if v['width'] and v['height'] else '?'
            tag = '[KEEP]' if i == 0 else '[DELETE]'
            stats = '' if i == 0 else fmt_pair_stats(pair_stats, keep['id'], v['id'], group)
            print(
                f"  [{fmt_size(v['file_size'])}]"
                f" [{fmt_duration(v['duration'])}]"
                f" [{res:<9}]"
                f"  {tag:<8}"
                f"{stats}"
            )
            print(f"    {v['path']}")
        print()

    if args.script:
        scan_py = Path(__file__).parent / 'scan.py'
        cleanup_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(scan_py))} --prune-videos --db {shlex.quote(args.db)}"
        lines = [
            '#!/usr/bin/env bash',
            'set -euo pipefail',
            '',
            f"trap {shlex.quote(cleanup_cmd)} EXIT",
            '',
        ]
        for gi, group in enumerate(groups, 1):
            members = group_members(group)
            keep = members[0]
            to_delete = members[1:]
            keep_res = f"{keep['width']}x{keep['height']}" if keep['width'] and keep['height'] else '?'
            keep_meta = f"[{fmt_size(keep['file_size'])}] [{fmt_duration(keep['duration'])}] [{keep_res}]"
            lines.append(f'# === Group {gi} ===')
            windows_path_keep = keep["path"].replace("/mnt/mondo/","z:\\").replace("/","\\")
            lines.append(f'# {keep_meta}  [KEEP]')
            lines.append(f'# rm -v {shlex.quote(keep["path"])}')
            for v in to_delete:
                res = f"{v['width']}x{v['height']}" if v['width'] and v['height'] else '?'
                meta = f"[{fmt_size(v['file_size'])}] [{fmt_duration(v['duration'])}] [{res}]"
                stats = fmt_pair_stats(pair_stats, keep['id'], v['id'], group)
                windows_path_delete = v["path"].replace("/mnt/mondo/","z:\\").replace("/","\\")
                lines.append(f'# {meta}  [DELETE]{stats}')
                lines.append(f'# rm -v {shlex.quote(v["path"])}')
            for d in to_delete:
                target_stem = _MD5_RE.sub(f'[{keep["md5"]}]', Path(d['path']).stem)
                target_name = target_stem + Path(keep['path']).suffix
                target_path = str(Path(d['path']).parent / target_name)
                lines.append(f'# mv -v {shlex.quote(keep["path"])} {shlex.quote(target_path)}')
            lines.append('')

        all_md5s = []
        seen_md5s = set()
        for group in groups:
            for v in group_members(group):
                if v['md5'] not in seen_md5s:
                    seen_md5s.add(v['md5'])
                    all_md5s.append(v['md5'])
        lines.append('# All MD5s above, for manage_non_matches.py:')
        lines.append('# ' + ' '.join(all_md5s))

        lines += ['']

        script = '\n'.join(lines)
        if args.script == '-':
            print(script)
        else:
            with open(args.script, 'w') as f:
                f.write(script + '\n')
            os.chmod(args.script, 0o755)
            print(f"Script written to {args.script}")


if __name__ == '__main__':
    main()

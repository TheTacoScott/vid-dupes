#!/usr/bin/env python3
"""Manage confirmed non-duplicate video pairs in the non_matches table."""

import argparse
import itertools
import sqlite3
import sys


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS non_matches (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            md5_a TEXT NOT NULL,
            md5_b TEXT NOT NULL,
            UNIQUE(md5_a, md5_b)
        )
    """)
    conn.commit()


def canonical(a: str, b: str) -> tuple[str, str]:
    """Always store the lexicographically smaller md5 first."""
    return (a, b) if a.lower() <= b.lower() else (b, a)


def resolve_md5s(conn: sqlite3.Connection, paths: list[str]) -> list[str]:
    md5s = []
    for p in paths:
        row = conn.execute("SELECT md5 FROM videos WHERE path = ?", (p,)).fetchone()
        if row is None:
            print(f"error: path not found in DB: {p}", file=sys.stderr)
            sys.exit(1)
        if row[0] is None:
            print(f"error: no md5 for path: {p}", file=sys.stderr)
            sys.exit(1)
        md5s.append(row[0])
    return md5s


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

def cmd_add(conn: sqlite3.Connection, args) -> None:
    values = args.values
    if len(values) < 2:
        print("error: need at least 2 md5s/paths.", file=sys.stderr)
        sys.exit(1)

    md5s = resolve_md5s(conn, values) if args.by_path else values

    inserted = skipped = 0
    for a, b in itertools.combinations(md5s, 2):
        ca, cb = canonical(a, b)
        try:
            conn.execute("INSERT INTO non_matches (md5_a, md5_b) VALUES (?, ?)", (ca, cb))
            inserted += 1
        except sqlite3.IntegrityError:
            skipped += 1
    conn.commit()

    msg = f"Added {inserted} pair(s)."
    if skipped:
        msg += f" {skipped} already existed (skipped)."
    print(msg)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def load_entries(conn: sqlite3.Connection) -> list[dict]:
    has_videos = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='videos'"
    ).fetchone() is not None

    if has_videos:
        rows = conn.execute("""
            SELECT nm.id, nm.md5_a, nm.md5_b,
                   va.path AS path_a, vb.path AS path_b
              FROM non_matches nm
              LEFT JOIN videos va ON va.md5 = nm.md5_a
              LEFT JOIN videos vb ON vb.md5 = nm.md5_b
             ORDER BY nm.id
        """).fetchall()
        return [
            {
                'id':     r[0],
                'md5_a':  r[1],
                'md5_b':  r[2],
                'path_a': r[3] or '(not in DB)',
                'path_b': r[4] or '(not in DB)',
            }
            for r in rows
        ]
    else:
        rows = conn.execute(
            "SELECT id, md5_a, md5_b FROM non_matches ORDER BY id"
        ).fetchall()
        return [
            {
                'id':     r[0],
                'md5_a':  r[1],
                'md5_b':  r[2],
                'path_a': '(no videos table)',
                'path_b': '(no videos table)',
            }
            for r in rows
        ]


def render_list(entries: list[dict], db_path: str) -> str:
    count = len(entries)
    noun = 'entry' if count == 1 else 'entries'
    lines = [
        f"Non-matches database: {db_path}",
        f"{count} {noun}",
    ]

    for e in entries:
        lines.append('')
        lines.append(f"#{e['id']}")
        lines.append(f"  {e['md5_a']}  {e['path_a']}")
        lines.append(f"  {e['md5_b']}  {e['path_b']}")

    return '\n'.join(lines)


def cmd_list(conn: sqlite3.Connection, args) -> None:
    entries = load_entries(conn)
    report  = render_list(entries, args.db)
    print(report)

    if args.report:
        with open(args.report, 'w') as f:
            f.write(report + '\n')
        print(f"\nReport written to {args.report}", file=sys.stderr)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

def cmd_delete(conn: sqlite3.Connection, args) -> None:
    if args.id is not None:
        cur = conn.execute("DELETE FROM non_matches WHERE id = ?", (args.id,))
        conn.commit()
        if cur.rowcount:
            print(f"Deleted entry {args.id}.")
        else:
            print(f"error: no entry with ID {args.id}.", file=sys.stderr)
            sys.exit(1)

    elif args.all_md5 is not None:
        md5 = resolve_md5s(conn, [args.all_md5])[0] if args.by_path else args.all_md5
        cur = conn.execute(
            "DELETE FROM non_matches WHERE md5_a = ? OR md5_b = ?", (md5, md5)
        )
        conn.commit()
        print(f"Deleted {cur.rowcount} pair(s) involving {md5}.")

    elif args.values:
        values = args.values
        if len(values) != 2:
            print("error: delete requires exactly 2 md5s/paths (or use --id or --all).", file=sys.stderr)
            sys.exit(1)
        md5s = resolve_md5s(conn, values) if args.by_path else values
        ca, cb = canonical(md5s[0], md5s[1])
        cur = conn.execute(
            "DELETE FROM non_matches WHERE md5_a = ? AND md5_b = ?", (ca, cb)
        )
        conn.commit()
        if cur.rowcount:
            print("Deleted 1 pair.")
        else:
            print("error: pair not found.", file=sys.stderr)
            sys.exit(1)

    else:
        print("error: specify --id N, --all MD5, or two md5s/paths.", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Manage confirmed non-duplicate video pairs."
    )
    parser.add_argument('--db', default='videos.db',
                        help="SQLite database path (default: videos.db)")

    sub = parser.add_subparsers(dest='command', required=True)

    # -- add --
    p_add = sub.add_parser('add', help="Mark a set of videos as confirmed non-matches.")
    p_add.add_argument('values', nargs='+', metavar='MD5_OR_PATH',
                       help="Two or more MD5s (or paths with --by-path) to mark as non-matches.")
    p_add.add_argument('--by-path', action='store_true',
                       help="Treat positional args as file paths and resolve their MD5s from the DB.")

    # -- list --
    p_list = sub.add_parser('list', help="List all non-match entries.")
    p_list.add_argument('--report', metavar='FILE',
                        help="Also write the report to FILE.")

    # -- delete --
    p_del = sub.add_parser('delete', help="Remove non-match entries.")
    p_del.add_argument('values', nargs='*', metavar='MD5_OR_PATH',
                       help="Two MD5s (or paths with --by-path) identifying the pair to remove.")
    p_del.add_argument('--id', type=int, metavar='N',
                       help="Delete the entry with this ID (from 'list' output).")
    p_del.add_argument('--all', dest='all_md5', metavar='MD5_OR_PATH',
                       help="Delete all pairs involving this MD5 (or path with --by-path).")
    p_del.add_argument('--by-path', action='store_true',
                       help="Treat MD5 arguments as file paths.")

    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    ensure_table(conn)

    if args.command == 'add':
        cmd_add(conn, args)
    elif args.command == 'list':
        cmd_list(conn, args)
    elif args.command == 'delete':
        cmd_delete(conn, args)

    conn.close()


if __name__ == '__main__':
    main()

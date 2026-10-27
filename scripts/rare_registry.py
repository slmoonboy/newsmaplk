#!/usr/bin/env python3
"""Index the Rare UI shadcn registry into a local SQLite database.

Rare UI (https://rareui.com) publishes a machine-readable registry at

    https://raw.githubusercontent.com/swamimalode07/rare-ui/main/public/r/registry.json

This script stores *metadata only* (name, title, description, dependencies and
file paths) so the components can be searched offline while designing. It does
not vendor or redistribute the component source code; see
``docs/RARE_UI.md`` and the upstream licence.

Usage::

    python scripts/rare_registry.py sync
    python scripts/rare_registry.py list
    python scripts/rare_registry.py list --type registry:ui
    python scripts/rare_registry.py search motion
    python scripts/rare_registry.py show animated-counter
    python scripts/rare_registry.py show animated-counter --json
    python scripts/rare_registry.py stats

The database lives at ``data/rare-ui.db`` (git-ignored, like the app database).
Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REGISTRY_URL = (
    "https://raw.githubusercontent.com/swamimalode07/rare-ui/main/public/r/registry.json"
)
ITEM_URL = "https://raw.githubusercontent.com/swamimalode07/rare-ui/main/public/r/{name}.json"
HOMEPAGE = "https://rareui.com"
REPO = "https://github.com/swamimalode07/rare-ui"
LICENSE = "MIT + Commons Clause 1.0 + Attribution (Rare UI)"
USER_AGENT = "LankaPulse/1.1 (rare-ui registry indexer; local design tooling)"
TIMEOUT_SECONDS = 30

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT_DIR / "data" / "rare-ui.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS components (
    name                  TEXT PRIMARY KEY,
    title                 TEXT NOT NULL,
    type                  TEXT NOT NULL,
    description           TEXT NOT NULL,
    dependencies          TEXT NOT NULL DEFAULT '[]',
    registry_dependencies TEXT NOT NULL DEFAULT '[]',
    files                 TEXT NOT NULL DEFAULT '[]',
    docs_url              TEXT NOT NULL,
    install_command       TEXT NOT NULL,
    synced_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_components_type ON components(type);
"""


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def install_command(name: str) -> str:
    return f"npx shadcn@latest add swamimalode07/rare-ui/{name}"


def sync(db_path: Path, *, verbose: bool = True) -> int:
    """Download the registry and replace the local component index."""
    if verbose:
        print(f"→ registry  {REGISTRY_URL}")
    registry = fetch_json(REGISTRY_URL)
    items = registry.get("items") or []
    if not items:
        raise ValueError("registry contains no items")

    synced_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rows = []
    for item in items:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        files = [
            entry.get("path", "")
            for entry in (item.get("files") or [])
            if isinstance(entry, dict) and entry.get("path")
        ]
        rows.append(
            (
                name,
                (item.get("title") or name).strip(),
                (item.get("type") or "registry:ui").strip(),
                (item.get("description") or "").strip(),
                json.dumps(item.get("dependencies") or [], ensure_ascii=False),
                json.dumps(item.get("registryDependencies") or [], ensure_ascii=False),
                json.dumps(files, ensure_ascii=False),
                f"{HOMEPAGE}/components",
                install_command(name),
                synced_at,
            )
        )

    connection = connect(db_path)
    try:
        with connection:
            connection.execute("DELETE FROM components")
            connection.executemany(
                """
                INSERT INTO components (
                    name, title, type, description, dependencies,
                    registry_dependencies, files, docs_url, install_command, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            for key, value in {
                "registry_url": REGISTRY_URL,
                "homepage": HOMEPAGE,
                "repo": REPO,
                "license": LICENSE,
                "synced_at": synced_at,
                "component_count": str(len(rows)),
            }.items():
                connection.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
    finally:
        connection.close()

    if verbose:
        print(f"✓ indexed {len(rows)} components → {db_path}")
        print(f"  licence: {LICENSE}")
        print(f"  credit : {HOMEPAGE}")
    return len(rows)


def require_db(path: Path) -> sqlite3.Connection:
    if not path.exists():
        print(
            f"✗ no database at {path}\n  run: python scripts/rare_registry.py sync",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return connect(path)


def print_rows(rows: list[sqlite3.Row]) -> None:
    if not rows:
        print("No matching components.")
        return
    for row in rows:
        deps = json.loads(row["dependencies"] or "[]")
        extra = f" · deps: {', '.join(deps)}" if deps else ""
        print(f"{row['name']:<24} {row['type']:<14} {row['title']}")
        if row["description"]:
            print(f"  {row['description']}{extra}")


def cmd_list(db_path: Path, args: argparse.Namespace) -> int:
    connection = require_db(db_path)
    sql = "SELECT * FROM components"
    params: list[str] = []
    if args.type:
        sql += " WHERE type = ?"
        params.append(args.type)
    sql += " ORDER BY type, name LIMIT ?"
    params.append(str(args.limit))
    rows = connection.execute(sql, params).fetchall()
    connection.close()
    print_rows(rows)
    return 0


def cmd_search(db_path: Path, args: argparse.Namespace) -> int:
    connection = require_db(db_path)
    pattern = f"%{args.query}%"
    rows = connection.execute(
        """
        SELECT * FROM components
        WHERE name LIKE ? OR title LIKE ? OR description LIKE ?
        ORDER BY name LIMIT ?
        """,
        (pattern, pattern, pattern, str(args.limit)),
    ).fetchall()
    connection.close()
    print_rows(rows)
    return 0


def cmd_show(db_path: Path, args: argparse.Namespace) -> int:
    connection = require_db(db_path)
    row = connection.execute(
        "SELECT * FROM components WHERE name = ?", (args.name,)
    ).fetchone()
    connection.close()
    if row is None:
        print(f"✗ unknown component: {args.name}", file=sys.stderr)
        return 1

    record = dict(row)
    for key in ("dependencies", "registry_dependencies", "files"):
        record[key] = json.loads(record[key] or "[]")

    if args.json:
        print(json.dumps(record, indent=2, ensure_ascii=False))
        return 0

    print(f"{record['title']}  ({record['name']})")
    print(f"  type        {record['type']}")
    print(f"  description {record['description']}")
    if record["dependencies"]:
        print(f"  deps        {', '.join(record['dependencies'])}")
    if record["registry_dependencies"]:
        print(f"  registry    {', '.join(record['registry_dependencies'])}")
    if record["files"]:
        print(f"  files       {', '.join(record['files'])}")
    print(f"  docs        {record['docs_url']}")
    print(f"  install     {record['install_command']}")
    print(f"  synced      {record['synced_at']}")
    return 0


def cmd_stats(db_path: Path) -> int:
    connection = require_db(db_path)
    meta = {
        row["key"]: row["value"]
        for row in connection.execute("SELECT key, value FROM meta").fetchall()
    }
    groups = connection.execute(
        "SELECT type, COUNT(*) AS n FROM components GROUP BY type ORDER BY type"
    ).fetchall()
    connection.close()
    print(f"database   {db_path}")
    for key in ("component_count", "synced_at", "license", "homepage", "registry_url"):
        if key in meta:
            print(f"{key:<16} {meta[key]}")
    for group in groups:
        print(f"{group['type']:<16} {group['n']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"database path (default: {DEFAULT_DB})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sync_parser = sub.add_parser("sync", help="download and re-index the registry")
    sync_parser.add_argument("-q", "--quiet", action="store_true")

    list_parser = sub.add_parser("list", help="list indexed components")
    list_parser.add_argument("--type", help="filter by registry type")
    list_parser.add_argument("--limit", type=int, default=100)

    search_parser = sub.add_parser("search", help="search name, title and description")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=20)

    show_parser = sub.add_parser("show", help="show one component")
    show_parser.add_argument("name")
    show_parser.add_argument("--json", action="store_true")

    sub.add_parser("stats", help="show index metadata")

    args = parser.parse_args(argv)
    args.limit = max(1, min(500, getattr(args, "limit", 100) or 100))

    if args.command == "sync":
        try:
            sync(args.db, verbose=not args.quiet)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            print(f"✗ sync failed: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.command == "list":
        return cmd_list(args.db, args)
    if args.command == "search":
        return cmd_search(args.db, args)
    if args.command == "show":
        return cmd_show(args.db, args)
    if args.command == "stats":
        return cmd_stats(args.db)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

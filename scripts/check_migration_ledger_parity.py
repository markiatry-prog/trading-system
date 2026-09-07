#!/usr/bin/env python3
"""Migration-ledger parity guard.

WHY THIS EXISTS. The retired Command Center developed silent ledger
drift: four migrations were applied through the Supabase dashboard and
recorded under different version timestamps than the repository files
carried. The schema was correct, but `supabase db push` from a clean
checkout would have tried to re-run all four and failed on existing
objects. Nobody noticed until an audit went looking, because nothing
checked.

THE INVARIANT:

    repository migration versions == applied remote ledger versions

Exactly. Same set, same order, no extras on either side.

TWO MODES, deliberately:

  local  (no DSN)   -- validates the repository half: filename format,
                       unique and ordered versions, no duplicates. Runs on
                       every PR, needs no credential, catches the mistakes
                       that are made while writing a migration.

  remote (with DSN) -- the real invariant, comparing the repository
                       against supabase_migrations.schema_migrations.
                       Required before deployment; that is where drift
                       actually becomes dangerous.

Local mode passing is NOT evidence the remote is in sync. It says so on
exit, so a green PR check is never mistaken for a deployable state.

Usage:
    check_migration_ledger_parity.py [--migrations DIR] [--dsn DSN]
    check_migration_ledger_parity.py --selftest

The DSN may also come from TRADING_MIGRATION_DSN.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# <14-digit version>_<snake_case name>.sql -- the shape the Supabase CLI
# generates and the ledger stores.
MIGRATION_RE = re.compile(r"^(?P<version>\d{14})_(?P<name>[a-z0-9_]+)\.sql$")


class ParityError(Exception):
    pass


def repository_migrations(directory: Path) -> list[tuple[str, str]]:
    """Returns [(version, name)] sorted by version, raising on any file
    that does not conform. A malformed name is a hard error: it is how a
    migration ends up unmatchable against the ledger."""
    if not directory.is_dir():
        raise ParityError(f"migrations directory not found: {directory}")
    entries: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    for path in sorted(directory.iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue
        if path.suffix != ".sql":
            raise ParityError(f"non-SQL file in migrations directory: {path.name}")
        m = MIGRATION_RE.match(path.name)
        if not m:
            raise ParityError(
                f"migration filename does not match <14-digit-version>_<name>.sql: {path.name}"
            )
        version, name = m.group("version"), m.group("name")
        if version in seen:
            raise ParityError(
                f"duplicate migration version {version}: {seen[version]} and {path.name}"
            )
        seen[version] = path.name
        entries.append((version, name))
    return sorted(entries, key=lambda e: e[0])


def remote_migrations(dsn: str) -> list[tuple[str, str]]:
    import psycopg2  # imported lazily so local mode needs no driver
    conn = psycopg2.connect(dsn, connect_timeout=15)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select version, coalesce(name, '') "
                "from supabase_migrations.schema_migrations order by version"
            )
            return [(r[0], r[1]) for r in cur.fetchall()]
    finally:
        conn.close()


def compare(repo: list[tuple[str, str]], remote: list[tuple[str, str]]) -> list[str]:
    problems: list[str] = []
    repo_v = {v: n for v, n in repo}
    remote_v = {v: n for v, n in remote}

    for version in sorted(set(repo_v) - set(remote_v)):
        problems.append(
            f"version {version} ({repo_v[version]}) is in the repository but NOT applied remotely"
        )
    for version in sorted(set(remote_v) - set(repo_v)):
        problems.append(
            f"version {version} ({remote_v[version]!r}) is applied remotely but NOT in the "
            f"repository -- likely a dashboard-applied migration"
        )
    for version in sorted(set(repo_v) & set(remote_v)):
        # An empty remote name is acceptable (older ledger rows); a
        # populated one that disagrees means the same version records two
        # different migrations, which is worse than a missing one.
        if remote_v[version] and remote_v[version] != repo_v[version]:
            problems.append(
                f"version {version} name mismatch: repository {repo_v[version]!r} "
                f"vs remote {remote_v[version]!r}"
            )
    return problems


def _selftest() -> int:
    failures = []
    repo = [("20260906215558", "trading_foundation"), ("20260906220427", "roles_and_grants")]

    if compare(repo, list(repo)):
        failures.append("identical ledgers should compare clean")
    if not compare(repo, repo[:1]):
        failures.append("a missing remote migration should be reported")
    if not compare(repo[:1], repo):
        failures.append("an extra remote migration should be reported")
    if not compare(repo, [("20260906215558", "something_else"), repo[1]]):
        failures.append("a name mismatch should be reported")
    # The exact Command Center failure: same names, different versions.
    drift = [("20260903011339", "trading_foundation"), ("20260903011407", "roles_and_grants")]
    if len(compare(repo, drift)) < 4:
        failures.append("dashboard-style version drift should be reported on both sides")
    if compare(repo, [("20260906215558", ""), ("20260906220427", "")]):
        failures.append("empty remote names should be tolerated")

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        (p / "20260906215558_trading_foundation.sql").write_text("-- x")
        if repository_migrations(p) != [("20260906215558", "trading_foundation")]:
            failures.append("well-formed migration should parse")
        (p / "not-a-migration.sql").write_text("-- x")
        try:
            repository_migrations(p); failures.append("malformed filename should raise")
        except ParityError:
            pass

    if failures:
        print("SELFTEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SELFTEST PASSED: parity comparison, drift detection, and filename "
          "validation all behave correctly.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--migrations", default=None)
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()

    directory = Path(args.migrations) if args.migrations else \
        Path(__file__).resolve().parents[1] / "supabase" / "migrations"

    try:
        repo = repository_migrations(directory)
    except ParityError as exc:
        print(f"MIGRATION LEDGER PARITY FAILED: {exc}")
        return 1

    print(f"repository migrations ({len(repo)}):")
    for version, name in repo:
        print(f"  {version}  {name}")

    dsn = args.dsn or os.environ.get("TRADING_MIGRATION_DSN")
    if not dsn:
        print("\nLOCAL MODE -- repository half validated only (format, uniqueness, order).")
        print("This is NOT evidence the remote ledger matches. Re-run with --dsn "
              "(or TRADING_MIGRATION_DSN) before deploying.")
        return 0

    try:
        remote = remote_migrations(dsn)
    except Exception as exc:
        print(f"\nMIGRATION LEDGER PARITY FAILED: could not read the remote ledger "
              f"({type(exc).__name__})")
        return 1

    problems = compare(repo, remote)
    if problems:
        print(f"\nMIGRATION LEDGER PARITY FAILED -- {len(problems)} discrepancy(ies):\n")
        for p in problems:
            print(f"  - {p}")
        print("\nDo not deploy. Reconcile the repository and the ledger first. Dashboard-"
              "only schema changes are prohibited for this project except as documented "
              "emergency recovery (see docs/MIGRATIONS.md).")
        return 1

    print(f"\nOK -- repository and remote ledger match exactly ({len(repo)} migration(s)).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

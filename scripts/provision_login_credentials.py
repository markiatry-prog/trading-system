#!/usr/bin/env python3
"""Deploy-time LOGIN credential provisioning -- the second half of the
two-tier role pattern.

THE PATTERN, restated so this script's constraints make sense: a NOLOGIN
`_svc` group role holds every grant; a NOLOGIN `_proc` role inherits it.
Neither can connect. A LOGIN credential is attached to the `_proc` role
immediately before the process that uses it deploys -- never in a
migration, never speculatively. Rotating a credential then touches no
grant and no policy, because the grants live on the group role.

WHY THIS IS A SCRIPT AND NOT SOMETHING ALREADY RUN

T-001 deliberately did NOT provision these. Creating live LOGIN
credentials with no deployed consumer expands attack surface for zero
benefit, and it would contradict the pattern's own rule. Run this when
Railway exists and is about to receive the values -- not before.

SECRET HANDLING

Passwords are generated HERE, server-side, with secrets.token_urlsafe(32).
They are never printed, never logged, never committed, and never passed
as arguments. The assembled DSNs are written to one file whose path you
give with --out; that file is the only place they exist outside the
database. Put the values into your deployment platform, then delete it.

USAGE

    provision_login_credentials.py --admin-dsn <dsn> \
        --pooler-host <host> --project-ref <ref> \
        --out /secure/path/trading-dsns.env
    provision_login_credentials.py --selftest
    provision_login_credentials.py --dry-run --pooler-host h --project-ref r

The admin DSN needs ALTER ROLE, i.e. the project's postgres role.

TWO THINGS THAT ARE EASY TO GET WRONG, AND WHY THEY ARE HANDLED HERE

`--project-ref` is not cosmetic. Supabase fronts Postgres with Supavisor,
which multiplexes many projects behind one hostname and therefore
authenticates on `<role>.<project-ref>`, not on the role alone. A DSN
carrying a bare role name is accepted by the parser and then fails
authentication at connect time -- so omitting this produces seven live
credentials that look correct and cannot connect.

The default port is 5432, session mode, NOT 6543. Transaction mode does
not support session-level features, `SET` among them. This system's
entire authority model is role-scoped, so a pooling mode that forbids
`SET ROLE` is a trap for every stage built on this foundation, even
though the current health read happens not to need it. Override with
--port only if you know the consumer is short-lived and never sets
session state.
"""
from __future__ import annotations

import argparse
import secrets
import sys
import urllib.parse

# Capability -> (role to give LOGIN, env var the runtime reads).
# Mirrors trading_system/db.py::DSN_VARS exactly. If these two ever
# disagree, a process gets a credential the code never reads, or reads a
# variable nothing provisioned -- so the self-test asserts they match.
CAPABILITIES = {
    "md_ingest":       ("md_ingest_proc",       "TRADING_DATABASE_URL_MD_INGEST"),
    "feature_engine":  ("feature_engine_proc",  "TRADING_DATABASE_URL_FEATURE_ENGINE"),
    "setup_detector":  ("setup_detector_proc",  "TRADING_DATABASE_URL_SETUP_DETECTOR"),
    "analyst":         ("analyst_proc",         "TRADING_DATABASE_URL_ANALYST"),
    "reviewer":        ("reviewer_proc",        "TRADING_DATABASE_URL_REVIEWER"),
    "delivery":        ("delivery_proc",        "TRADING_DATABASE_URL_DELIVERY"),
    "trading_control": ("trading_control_proc", "TRADING_DATABASE_URL_CONTROL"),
}

# Session mode. See the module docstring: transaction mode (6543) cannot
# run `SET`, and this architecture is built on role-scoped sessions.
POOLER_PORT_SESSION = 5432
POOLER_PORT_TRANSACTION = 6543
POOLER_PORT = POOLER_PORT_SESSION
POOLER_DB = "postgres"
PASSWORD_BYTES = 32


def generate_password() -> str:
    # token_urlsafe: no quote/shell-special characters, so a value cannot
    # break a DSN, a YAML file, or a platform variable field.
    return secrets.token_urlsafe(PASSWORD_BYTES)


def build_dsn(role: str, password: str, host: str, project_ref: str = None,
              port: int = POOLER_PORT, database: str = POOLER_DB) -> str:
    """Percent-encodes the password. token_urlsafe never emits a character
    that needs it, but encoding unconditionally means a hand-supplied
    password cannot silently corrupt the DSN either.

    When `project_ref` is given the username becomes `<role>.<ref>`, which
    is what Supavisor requires -- it routes on the tenant, so a bare role
    name authenticates as nobody. Omit it only for a direct (non-pooled)
    Postgres endpoint, where the bare role is correct."""
    quoted = urllib.parse.quote(password, safe="")
    user = f"{role}.{project_ref}" if project_ref else role
    return f"postgresql://{user}:{quoted}@{host}:{port}/{database}"


def alter_statements(assignments: dict) -> list:
    """One ALTER ROLE per capability. Password is passed as a bound
    parameter by the caller, never interpolated into SQL text."""
    return [f"alter role {role} login password %s" for role, _ in assignments.values()]


def provision(conn, host: str, project_ref: str = None,
              port: int = POOLER_PORT) -> dict:
    """Returns {env_var: dsn}. Each ALTER runs with the password bound, so
    it never appears in a query string that could be logged by the server."""
    out = {}
    with conn.cursor() as cur:
        for capability, (role, env_var) in CAPABILITIES.items():
            password = generate_password()
            # Role names come from the constant above, never from input.
            cur.execute(f"alter role {role} login password %s", (password,))
            out[env_var] = build_dsn(role, password, host, project_ref, port)
    conn.commit()
    return out


def write_env_file(values: dict, path: str) -> None:
    lines = [
        "# Generated by provision_login_credentials.py.",
        "# THESE ARE LIVE CREDENTIALS. Put them into your deployment",
        "# platform's variable store, then delete this file.",
        "# Never commit it. Never paste it into a chat or an issue.",
        "",
    ]
    for var in CAPABILITIES.values():
        env_var = var[1]
        lines.append(f"{env_var}={values[env_var]}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    try:
        import os
        os.chmod(path, 0o600)
    except OSError:
        pass


def _selftest() -> int:
    failures = []

    # The two maps that must never drift apart.
    try:
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
        from trading_system.db import DSN_VARS
        if set(DSN_VARS) != set(CAPABILITIES):
            failures.append("capability sets differ between db.py and this script")
        for cap, var in DSN_VARS.items():
            if CAPABILITIES.get(cap, (None, None))[1] != var:
                failures.append(f"env var mismatch for {cap}")
    except ImportError:
        print("  (db.py not importable from here -- skipping cross-check)")

    pw = generate_password()
    if len(pw) < 40:
        failures.append("password is too short")
    if any(c in pw for c in "'\" ;@:/"):
        failures.append("password contains a character that could break a DSN")
    if generate_password() == generate_password():
        failures.append("passwords are not unique per call")

    dsn = build_dsn("analyst_proc", "a b@c/d", "h.example")
    if "a b@c/d" in dsn:
        failures.append("password was not percent-encoded")
    if not dsn.startswith("postgresql://analyst_proc:"):
        failures.append("DSN shape is wrong")
    if not dsn.endswith("@h.example:5432/postgres"):
        failures.append("pooler host/port/db is wrong")

    # Supavisor authenticates on <role>.<project-ref>. A bare role name
    # parses fine and then fails at connect time, so this is asserted
    # rather than trusted.
    tenant = build_dsn("analyst_proc", "pw", "h.example", "abcdefghijklmnop")
    if not tenant.startswith("postgresql://analyst_proc.abcdefghijklmnop:"):
        failures.append("project ref is not in the DSN username")

    # Session mode by default: transaction mode cannot run SET, and this
    # system's authority model is role-scoped.
    if POOLER_PORT != POOLER_PORT_SESSION:
        failures.append("default port is not session mode")
    if POOLER_PORT_TRANSACTION == POOLER_PORT_SESSION:
        failures.append("session and transaction ports must differ")

    # Every role is a _proc role -- never a _svc group role, which holds
    # the grants and must stay unable to log in.
    for role, _ in CAPABILITIES.values():
        if not role.endswith("_proc"):
            failures.append(f"{role} is not a _proc role")

    # Analyst and reviewer must remain separate credentials.
    if CAPABILITIES["analyst"][1] == CAPABILITIES["reviewer"][1]:
        failures.append("analyst and reviewer share an env var")
    if CAPABILITIES["analyst"][0] == CAPABILITIES["reviewer"][0]:
        failures.append("analyst and reviewer share a role")

    if failures:
        print("SELFTEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"SELFTEST PASSED: {len(CAPABILITIES)} capabilities, distinct roles and "
          f"env vars, passwords URL-safe and unique, DSNs correctly encoded.")
    return 0


def main(argv: list) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--admin-dsn")
    p.add_argument("--pooler-host")
    p.add_argument("--project-ref",
                   help="Supabase project ref. Required for a pooler host; "
                        "omit only for a direct Postgres endpoint.")
    p.add_argument("--port", type=int, default=POOLER_PORT,
                   help=f"default {POOLER_PORT_SESSION} (session mode)")
    p.add_argument("--out")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args(argv)

    if a.selftest:
        return _selftest()

    if a.dry_run:
        print("Would provision LOGIN for:")
        for cap, (role, var) in CAPABILITIES.items():
            host = a.pooler_host or "<pooler-host>"
            ref = a.project_ref or "<project-ref>"
            print(f"  {cap:16} role={role:22} -> {var}")
            print(f"      postgresql://{role}.{ref}:<generated>"
                  f"@{host}:{a.port}/{POOLER_DB}")
        print(f"\nport {a.port} "
              f"({'session' if a.port == POOLER_PORT_SESSION else 'transaction'} mode)")
        return 0

    if not (a.admin_dsn and a.pooler_host and a.out):
        print("--admin-dsn, --pooler-host and --out are all required", file=sys.stderr)
        return 2

    # Refusing is right here. Provisioning without the tenant would mint
    # seven live credentials that cannot authenticate, and the failure
    # would not surface until the deploy healthcheck.
    if not a.project_ref and "pooler" in a.pooler_host:
        print("--project-ref is required for a Supabase pooler host: "
              "Supavisor authenticates on <role>.<project-ref>, so a bare "
              "role name will fail at connect time. Pass --project-ref, or "
              "use a direct endpoint if you really want a bare role.",
              file=sys.stderr)
        return 2

    import psycopg2
    conn = psycopg2.connect(a.admin_dsn, connect_timeout=15)
    try:
        values = provision(conn, a.pooler_host, a.project_ref, a.port)
    finally:
        conn.close()
    write_env_file(values, a.out)
    print(f"Provisioned {len(values)} LOGIN credentials.")
    print(f"Wrote {a.out} (mode 0600). Load these into your deployment "
          f"platform, then DELETE the file.")
    print("No credential was printed to stdout or written to any log.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Acquire the OPERATOR-APPROVED T-004 dataset. Nothing else.

The approved acquisition is encoded below as a frozen constant. The
script rebuilds the request from that constant, re-prices it, and
compares against the approved figure BEFORE transferring anything. A
request that does not match the approval, or that has become materially
more expensive, aborts.

This is mechanical on purpose. "Check it matches what was approved" done
by eye, at the keyboard, at the moment of spending money, is exactly the
check that gets skipped.

APPROVED 2026-09-07 by the operator:
    GLBX.MDP3 / ohlcv-1m / NQ.c.0, MNQ.c.0, ES.c.0
    2021-09-01 .. 2026-09-01, get_cost() = $19.25, 15.4% of the credit

Explicitly NOT authorised: additional schemas, additional instruments,
depth or MBO of any kind, or any extension of the date range.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_system.sources.databento_source import DATASET, SCHEMA, STYPE_IN

# ---------------------------------------------------------------------
# THE APPROVAL. Changing any value here changes what may be bought, so
# it is one frozen block rather than scattered defaults.
# ---------------------------------------------------------------------
APPROVED = {
    "dataset": "GLBX.MDP3",
    "schema": "ohlcv-1m",
    "symbols": ["NQ.c.0", "MNQ.c.0", "ES.c.0"],
    "stype_in": "continuous",
    "start": "2021-09-01",
    "end": "2026-09-01",
    "approved_cost_usd": 19.25,
    "credit_usd": 125.00,
    "approved_at": "2026-09-07",
}

# A re-price above this aborts. 10% absorbs ordinary rate drift; anything
# larger means the request is not the one that was approved.
COST_TOLERANCE = 1.10
# Independent ceiling: never spend more than a third of the credit on one
# pull, whatever the tolerance arithmetic says.
HARD_CEILING_FRACTION = 0.3333


class AcquisitionRefused(Exception):
    pass


def request_digest(spec: dict) -> str:
    body = {k: spec[k] for k in ("dataset", "schema", "symbols", "stype_in",
                                 "start", "end")}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def revalidate(spec: dict) -> None:
    """The constructed request must BE the approved request."""
    if spec["dataset"] != APPROVED["dataset"]:
        raise AcquisitionRefused(f"dataset {spec['dataset']!r} was not approved")
    if spec["schema"] != APPROVED["schema"]:
        raise AcquisitionRefused(f"schema {spec['schema']!r} was not approved")
    if sorted(spec["symbols"]) != sorted(APPROVED["symbols"]):
        raise AcquisitionRefused(f"symbols {spec['symbols']} do not match the approval")
    if spec["stype_in"] != APPROVED["stype_in"]:
        raise AcquisitionRefused("stype_in does not match the approval")
    if spec["start"] != APPROVED["start"] or spec["end"] != APPROVED["end"]:
        raise AcquisitionRefused("date range does not match the approval")
    for banned in ("mbo", "mbp", "tbbo", "trades", "bbo", "definition", "statistics"):
        if banned in spec["schema"]:
            raise AcquisitionRefused(
                f"schema {spec['schema']!r} contains {banned!r}; depth, tick "
                f"and auxiliary schemas were explicitly excluded")


def check_cost(actual: float) -> None:
    approved = APPROVED["approved_cost_usd"]
    ceiling = APPROVED["credit_usd"] * HARD_CEILING_FRACTION
    if actual > approved * COST_TOLERANCE:
        raise AcquisitionRefused(
            f"re-priced at ${actual:.2f}, which is more than "
            f"{int((COST_TOLERANCE - 1) * 100)}% above the approved "
            f"${approved:.2f}. Stopping and reporting, as instructed.")
    if actual > ceiling:
        raise AcquisitionRefused(
            f"${actual:.2f} exceeds the hard ceiling of ${ceiling:.2f} "
            f"(one third of the ${APPROVED['credit_usd']:.0f} credit)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="data/t004")
    ap.add_argument("--dry-run", action="store_true",
                    help="revalidate and re-price only; transfer nothing")
    args = ap.parse_args()

    spec = {k: APPROVED[k] for k in ("dataset", "schema", "symbols",
                                     "stype_in", "start", "end")}
    print("T-004 acquisition -- approved request")
    print(f"  dataset  {spec['dataset']}   schema {spec['schema']}")
    print(f"  symbols  {', '.join(spec['symbols'])}  ({spec['stype_in']})")
    print(f"  period   {spec['start']} .. {spec['end']}")
    print(f"  digest   {request_digest(spec)[:32]}\n")

    try:
        revalidate(spec)
        print("1. request matches the approval  OK")
    except AcquisitionRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    key = os.environ.get("TRADING_DATABENTO_API_KEY")
    if not key:
        print("TRADING_DATABENTO_API_KEY is not set", file=sys.stderr)
        return 2
    try:
        import databento as db
    except ImportError:
        print("databento is not installed; run: pip install databento",
              file=sys.stderr)
        return 2

    client = db.Historical(key)

    cost = float(client.metadata.get_cost(
        dataset=spec["dataset"], schema=spec["schema"], symbols=spec["symbols"],
        stype_in=spec["stype_in"], start=spec["start"], end=spec["end"]))
    pct = 100.0 * cost / APPROVED["credit_usd"]
    print(f"2. re-priced: ${cost:.2f} ({pct:.1f}% of credit); "
          f"approved was ${APPROVED['approved_cost_usd']:.2f}")
    try:
        check_cost(cost)
        print("3. cost within the approved tolerance  OK")
    except AcquisitionRefused as exc:
        print(f"\nSTOPPING: {exc}", file=sys.stderr)
        return 3

    if args.dry_run:
        print("\n--dry-run: nothing transferred.")
        return 0

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    print(f"\n4. transferring to {out} ...")

    data = client.timeseries.get_range(
        dataset=spec["dataset"], schema=spec["schema"], symbols=spec["symbols"],
        stype_in=spec["stype_in"], start=spec["start"], end=spec["end"])
    path = out / "t004_ohlcv_1m.dbn.zst"
    data.to_file(path)
    finished = datetime.now(timezone.utc)

    size = path.stat().st_size
    sha = hashlib.sha256(path.read_bytes()).hexdigest()

    manifest = {
        "approved": APPROVED,
        "request": spec,
        "request_digest": request_digest(spec),
        "repriced_cost_usd": cost,
        "credit_fraction": pct / 100.0,
        "file": path.name,
        "bytes": size,
        "sha256": sha,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "provider": "databento",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))

    print(f"5. wrote {path.name}  {size:,} bytes")
    print(f"   sha256 {sha}")
    print(f"   manifest.json written -- this is the provenance record")
    print(f"\nActual charge: ${cost:.2f} ({pct:.1f}% of the $125 credit).")
    print("Next: python3 scripts/run_t004_study.py --data-dir " + str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

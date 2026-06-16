#!/usr/bin/env python3
"""
Pull Anthropic API-key usage (tokens) and organization cost (USD) from the Admin API.

    export ANTHROPIC_ADMIN_KEY=sk-ant-admin01-...
    python pull_usage_cost.py                      # last 7 days
    python pull_usage_cost.py --days 30
    python pull_usage_cost.py --start 2025-01-01 --end 2025-02-01
    python pull_usage_cost.py --days 30 --csv ./out

Requires an ADMIN API key (starts with sk-ant-admin...), NOT a regular sk-ant-api... key.
Provision one in the Console: Settings -> Admin keys (requires the admin org role).

Endpoints used (all under https://api.anthropic.com/v1/organizations):
  GET /usage_report/messages   token usage, groupable by api_key_id
  GET /cost_report             USD spend, groupable by workspace_id / description only
  GET /api_keys                resolve api_key_id -> name
  GET /workspaces              resolve workspace_id -> name
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import requests

BASE_URL = "https://api.anthropic.com/v1/organizations"
API_VERSION = "2023-06-01"
TIMEOUT = 30
DAILY_PAGE_LIMIT = 31  # max time buckets per page for bucket_width=1d


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def make_session() -> requests.Session:
    key = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not key:
        sys.exit(
            "ERROR: set ANTHROPIC_ADMIN_KEY first.\n"
            "  It must be an Admin API key (starts with sk-ant-admin...), not a regular API key.\n"
            "  Create one in the Console: Settings -> Admin keys (requires the admin role)."
        )
    if not key.startswith("sk-ant-admin"):
        print(
            "WARNING: ANTHROPIC_ADMIN_KEY does not start with 'sk-ant-admin' — the usage/cost "
            "endpoints reject regular API keys with 401.",
            file=sys.stderr,
        )
    session = requests.Session()
    session.headers.update({"x-api-key": key, "anthropic-version": API_VERSION})
    return session


def get(session: requests.Session, path: str, params: dict) -> dict:
    resp = session.get(f"{BASE_URL}{path}", params=params, timeout=TIMEOUT)
    if resp.status_code == 401:
        sys.exit("ERROR: 401 Unauthorized — confirm ANTHROPIC_ADMIN_KEY is an Admin key (sk-ant-admin...).")
    if resp.status_code == 403:
        sys.exit("ERROR: 403 Forbidden — this key/org lacks Admin API access (need admin role on a Console org).")
    resp.raise_for_status()
    return resp.json()


def paginate_report(session, path, params):
    """Usage/cost reports paginate via next_page -> page."""
    params = dict(params)
    while True:
        body = get(session, path, params)
        yield from body.get("data", [])
        if body.get("has_more") and body.get("next_page"):
            params["page"] = body["next_page"]
        else:
            return


def paginate_list(session, path, params):
    """Org list endpoints (api_keys, workspaces) paginate via last_id -> after_id."""
    params = dict(params)
    while True:
        body = get(session, path, params)
        yield from body.get("data", [])
        if body.get("has_more") and body.get("last_id"):
            params["after_id"] = body["last_id"]
        else:
            return


# --------------------------------------------------------------------------- #
# Name lookups (IDs -> human-readable)
# --------------------------------------------------------------------------- #
def fetch_api_key_names(session) -> dict:
    return {k["id"]: (k.get("name") or k["id"]) for k in paginate_list(session, "/api_keys", {"limit": 1000})}


def fetch_workspace_names(session) -> dict:
    return {w["id"]: (w.get("name") or w["id"]) for w in paginate_list(session, "/workspaces", {"limit": 1000})}


# --------------------------------------------------------------------------- #
# Usage (tokens) grouped by API key
# --------------------------------------------------------------------------- #
def _cache_creation_total(result: dict) -> int:
    cc = result.get("cache_creation") or {}
    return (cc.get("ephemeral_1h_input_tokens") or 0) + (cc.get("ephemeral_5m_input_tokens") or 0)


def fetch_usage_by_api_key(session, starting_at: str, ending_at: str) -> dict:
    params = {
        "starting_at": starting_at,
        "ending_at": ending_at,
        "bucket_width": "1d",
        "group_by[]": ["api_key_id"],
        "limit": DAILY_PAGE_LIMIT,
    }
    agg: dict = defaultdict(lambda: defaultdict(int))  # api_key_id (or None) -> field -> count
    for bucket in paginate_report(session, "/usage_report/messages", params):
        for r in bucket.get("results", []):
            a = agg[r.get("api_key_id")]  # None == Console/Workbench traffic
            a["uncached_input_tokens"] += r.get("uncached_input_tokens") or 0
            a["cache_read_input_tokens"] += r.get("cache_read_input_tokens") or 0
            a["cache_creation_input_tokens"] += _cache_creation_total(r)
            a["output_tokens"] += r.get("output_tokens") or 0
            a["web_search_requests"] += (r.get("server_tool_use") or {}).get("web_search_requests") or 0
    return agg


# --------------------------------------------------------------------------- #
# Cost (USD) — amounts come back in CENTS as decimal strings
# --------------------------------------------------------------------------- #
def fetch_cost_grouped(session, starting_at: str, ending_at: str, group_field: str) -> dict:
    params = {
        "starting_at": starting_at,
        "ending_at": ending_at,
        "bucket_width": "1d",
        "group_by[]": [group_field],
        "limit": DAILY_PAGE_LIMIT,
    }
    agg: dict = defaultdict(lambda: Decimal("0"))  # group value -> cents
    for bucket in paginate_report(session, "/cost_report", params):
        for r in bucket.get("results", []):
            agg[r.get(group_field)] += Decimal(r.get("amount") or "0")
    return agg


# --------------------------------------------------------------------------- #
# Formatting / output
# --------------------------------------------------------------------------- #
def _usd(cents: Decimal) -> Decimal:
    return cents / Decimal(100)


def _fmt_usd(cents: Decimal) -> str:
    return f"${_usd(cents):,.4f}"


def print_usage_table(agg: dict, key_names: dict) -> None:
    print("\n=== Token usage by API key ===")
    if not agg:
        print("  (no usage in this period)")
        return
    rows = []
    for kid, f in agg.items():
        label = "Console / Workbench (no API key)" if kid is None else key_names.get(kid, kid)
        total = (
            f["uncached_input_tokens"]
            + f["cache_read_input_tokens"]
            + f["cache_creation_input_tokens"]
            + f["output_tokens"]
        )
        rows.append((label, f, total))
    rows.sort(key=lambda r: r[2], reverse=True)

    hdr = f"{'API key':<34}{'uncached_in':>14}{'cache_read':>13}{'cache_create':>14}{'output':>13}{'total':>15}{'web_search':>12}"
    print(hdr)
    print("-" * len(hdr))
    for label, f, total in rows:
        print(
            f"{label[:34]:<34}"
            f"{f['uncached_input_tokens']:>14,}"
            f"{f['cache_read_input_tokens']:>13,}"
            f"{f['cache_creation_input_tokens']:>14,}"
            f"{f['output_tokens']:>13,}"
            f"{total:>15,}"
            f"{f['web_search_requests']:>12,}"
        )


def print_cost_table(title: str, agg: dict, name_map: dict | None) -> None:
    print(f"\n=== {title} ===")
    if not agg:
        print("  (no cost in this period)")
        return
    rows, total = [], Decimal("0")
    for key, cents in agg.items():
        if name_map is not None:
            label = "Default workspace" if key is None else name_map.get(key, key)
        else:
            label = key if key is not None else "(uncategorized)"
        rows.append((str(label), cents))
        total += cents
    rows.sort(key=lambda r: r[1], reverse=True)
    for label, cents in rows:
        print(f"  {label[:52]:<52}{_fmt_usd(cents):>16}")
    print(f"  {'-' * 52}{'-' * 16}")
    print(f"  {'TOTAL':<52}{_fmt_usd(total):>16}")


def write_csv(path: Path, header: list, rows: list) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    print(f"  wrote {path}")


# --------------------------------------------------------------------------- #
# Date range
# --------------------------------------------------------------------------- #
def _day_floor(d: datetime) -> datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _to_rfc3339(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_date_arg(s: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            d = datetime.strptime(s, fmt)
            return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d
        except ValueError:
            continue
    sys.exit(f"ERROR: could not parse date '{s}'. Use YYYY-MM-DD or RFC3339 (e.g. 2025-01-01T00:00:00Z).")


def resolve_range(args) -> tuple[str, str]:
    today = _day_floor(datetime.now(timezone.utc))
    if args.start or args.end:
        start = _parse_date_arg(args.start) if args.start else today - timedelta(days=7)
        end = _parse_date_arg(args.end) if args.end else today + timedelta(days=1)
    else:
        end = today + timedelta(days=1)              # include today's partial day
        start = today - timedelta(days=args.days - 1)
    if start >= end:
        sys.exit("ERROR: start must be before end.")
    return _to_rfc3339(start), _to_rfc3339(end)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Pull Anthropic API-key usage and org cost from the Admin API.")
    ap.add_argument("--days", type=int, default=7, help="Days back from today (default 7). Ignored if --start/--end given.")
    ap.add_argument("--start", help="Start date YYYY-MM-DD or RFC3339 (UTC). Overrides --days.")
    ap.add_argument("--end", help="End date (exclusive). Defaults to tomorrow 00:00 UTC.")
    ap.add_argument("--csv", metavar="DIR", help="Also write CSV files into DIR.")
    args = ap.parse_args()

    starting_at, ending_at = resolve_range(args)
    session = make_session()

    print(f"Window: {starting_at}  ->  {ending_at}  (daily buckets, UTC)")
    print("Resolving API key + workspace names...")
    key_names = fetch_api_key_names(session)
    ws_names = fetch_workspace_names(session)

    print("Fetching token usage (grouped by api_key_id)...")
    usage = fetch_usage_by_api_key(session, starting_at, ending_at)
    print_usage_table(usage, key_names)

    print("\nFetching cost (USD)...")
    cost_ws = fetch_cost_grouped(session, starting_at, ending_at, "workspace_id")
    print_cost_table("Cost by workspace (USD)", cost_ws, ws_names)
    cost_desc = fetch_cost_grouped(session, starting_at, ending_at, "description")
    print_cost_table("Cost by line item / model (USD)", cost_desc, None)

    print(
        "\nNote: USD cost cannot be grouped by API key — the cost endpoint groups only by "
        "workspace_id / description.\n"
        "      Use per-key tokens above as a proxy, or isolate keys in their own workspace for "
        "clean dollar attribution.\n"
        "      Console/Workbench traffic has no api_key_id; the default workspace has no workspace_id."
    )

    if args.csv:
        out = Path(args.csv)
        out.mkdir(parents=True, exist_ok=True)
        usage_rows = []
        for kid, f in usage.items():
            label = "Console/Workbench (no key)" if kid is None else key_names.get(kid, kid)
            total = (
                f["uncached_input_tokens"] + f["cache_read_input_tokens"]
                + f["cache_creation_input_tokens"] + f["output_tokens"]
            )
            usage_rows.append([
                kid or "", label, f["uncached_input_tokens"], f["cache_read_input_tokens"],
                f["cache_creation_input_tokens"], f["output_tokens"], total, f["web_search_requests"],
            ])
        write_csv(
            out / "usage_by_api_key.csv",
            ["api_key_id", "api_key_name", "uncached_input_tokens", "cache_read_input_tokens",
             "cache_creation_input_tokens", "output_tokens", "total_tokens", "web_search_requests"],
            usage_rows,
        )
        write_csv(
            out / "cost_by_workspace.csv",
            ["workspace_id", "workspace_name", "cost_usd"],
            [[wid or "", "Default workspace" if wid is None else ws_names.get(wid, wid), f"{_usd(c):.6f}"]
             for wid, c in cost_ws.items()],
        )
        write_csv(
            out / "cost_by_description.csv",
            ["description", "cost_usd"],
            [[d if d is not None else "", f"{_usd(c):.6f}"] for d, c in cost_desc.items()],
        )


if __name__ == "__main__":
    main()

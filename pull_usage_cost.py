#!/usr/bin/env python3
"""
Pull Anthropic API-key usage (tokens) and organization cost (USD) from the Admin API,
and estimate per-API-key cost by deriving effective rates from the cost endpoint.

    export ANTHROPIC_ADMIN_KEY=sk-ant-admin01-...
    python pull_usage_cost.py                      # last 7 days
    python pull_usage_cost.py --days 30
    python pull_usage_cost.py --start 2025-01-01 --end 2025-02-01
    python pull_usage_cost.py --days 30 --csv ./out

The time window is a HALF-OPEN interval in UTC: --start is inclusive, --end is exclusive.
So `--start 2025-01-01 --end 2025-02-01` covers all of January (Jan 1 through Jan 31) and
does NOT include Feb 1. See resolve_range() for the full explanation and rationale.

Requires an ADMIN API key (starts with sk-ant-admin...), NOT a regular sk-ant-api... key.
Provision one in the Console: Settings -> Admin keys (requires the admin org role).

How per-key cost is computed (the cost endpoint cannot group by api_key_id):
  rate(model, tier, context_window, token_type) = cost_amount / total_tokens   # from the real bill
  per_key_cost(key)                             = Σ  per_key_tokens × rate
The cost endpoint's `token_type` strings match the usage fields 1:1, so the join is exact
and per-key estimates sum back to the cost-endpoint total. Caveats it surfaces explicitly:
  - priority/flex tokens have no cost rows -> reported as volume, "$ n/a"
  - code-execution/session costs aren't per-key in the usage endpoint -> org-level line
  - Console/Workbench traffic has no api_key_id; the default workspace has no workspace_id
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

# token_type strings shared by the usage fields and the cost endpoint's `token_type`.
TOKEN_TYPES = (
    "uncached_input_tokens",
    "cache_read_input_tokens",
    "cache_creation.ephemeral_1h_input_tokens",
    "cache_creation.ephemeral_5m_input_tokens",
    "output_tokens",
)
CONSOLE_LABEL = "Console / Workbench (no API key)"


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


def key_label(kid, key_names) -> str:
    return CONSOLE_LABEL if kid is None else key_names.get(kid, kid)


# --------------------------------------------------------------------------- #
# Usage (tokens), grouped by the pricing dimensions so cost can be derived
# --------------------------------------------------------------------------- #
def _token_entries(r: dict) -> dict:
    cc = r.get("cache_creation") or {}
    return {
        "uncached_input_tokens": r.get("uncached_input_tokens") or 0,
        "cache_read_input_tokens": r.get("cache_read_input_tokens") or 0,
        "cache_creation.ephemeral_1h_input_tokens": cc.get("ephemeral_1h_input_tokens") or 0,
        "cache_creation.ephemeral_5m_input_tokens": cc.get("ephemeral_5m_input_tokens") or 0,
        "output_tokens": r.get("output_tokens") or 0,
    }


def _new_key_record() -> dict:
    return {
        "tokens": defaultdict(int),  # token_type -> count (for the usage table)
        "tuples": defaultdict(int),  # (model, tier, ctx, token_type) -> count (for costing)
        "web_search_requests": 0,
    }


def fetch_usage(session, starting_at: str, ending_at: str) -> dict:
    """Returns { api_key_id|None: {tokens, tuples, web_search_requests} }."""
    params = {
        "starting_at": starting_at,
        "ending_at": ending_at,
        "bucket_width": "1d",
        "group_by[]": ["api_key_id", "model", "service_tier", "context_window"],
        "limit": DAILY_PAGE_LIMIT,
    }
    keys: dict = defaultdict(_new_key_record)
    for bucket in paginate_report(session, "/usage_report/messages", params):
        for r in bucket.get("results", []):
            rec = keys[r.get("api_key_id")]  # None == Console/Workbench
            dims = (r.get("model"), r.get("service_tier"), r.get("context_window"))
            for token_type, n in _token_entries(r).items():
                rec["tokens"][token_type] += n
                rec["tuples"][dims + (token_type,)] += n
            rec["web_search_requests"] += (r.get("server_tool_use") or {}).get("web_search_requests") or 0
    return keys


# --------------------------------------------------------------------------- #
# Cost (USD) — amounts come back in CENTS as decimal strings
# --------------------------------------------------------------------------- #
def fetch_cost_breakdown(session, starting_at: str, ending_at: str) -> dict:
    """Group by description to get token dollars per (model,tier,ctx,token_type) plus
    web_search / code_execution / session_usage totals, all in cents."""
    params = {
        "starting_at": starting_at,
        "ending_at": ending_at,
        "bucket_width": "1d",
        "group_by[]": ["description"],
        "limit": DAILY_PAGE_LIMIT,
    }
    out = {
        "by_tuple": defaultdict(lambda: Decimal("0")),  # (model,tier,ctx,token_type) -> cents
        "web_search": Decimal("0"),
        "code_execution": Decimal("0"),
        "session_usage": Decimal("0"),
        "other": Decimal("0"),
        "grand_total": Decimal("0"),
    }
    for bucket in paginate_report(session, "/cost_report", params):
        for c in bucket.get("results", []):
            amt = Decimal(c.get("amount") or "0")
            out["grand_total"] += amt
            ct = c.get("cost_type")
            if ct == "tokens":
                out["by_tuple"][(c.get("model"), c.get("service_tier"), c.get("context_window"), c.get("token_type"))] += amt
            elif ct in ("web_search", "code_execution", "session_usage"):
                out[ct] += amt
            else:
                out["other"] += amt
    return out


def fetch_cost_by_workspace(session, starting_at: str, ending_at: str) -> dict:
    params = {
        "starting_at": starting_at,
        "ending_at": ending_at,
        "bucket_width": "1d",
        "group_by[]": ["workspace_id"],
        "limit": DAILY_PAGE_LIMIT,
    }
    agg: dict = defaultdict(lambda: Decimal("0"))
    for bucket in paginate_report(session, "/cost_report", params):
        for r in bucket.get("results", []):
            agg[r.get("workspace_id")] += Decimal(r.get("amount") or "0")
    return agg


# --------------------------------------------------------------------------- #
# Cost attribution (pure function — unit-testable without the network)
# --------------------------------------------------------------------------- #
def compute_key_costs(usage: dict, cost: dict) -> dict:
    """Derive effective per-token / per-request rates from the cost endpoint and apply
    them to per-key usage. All money values are Decimal CENTS."""
    # Denominators: total tokens per pricing tuple, and total web-search requests.
    total_tuple: dict = defaultdict(int)
    total_web = 0
    for rec in usage.values():
        for tup, n in rec["tuples"].items():
            total_tuple[tup] += n
        total_web += rec["web_search_requests"]

    # Effective rates straight from the bill.
    rate: dict = {}
    cost_without_usage = Decimal("0")  # cost rows that had no matching usage (window skew/anomaly)
    for tup, cents in cost["by_tuple"].items():
        tok = total_tuple.get(tup, 0)
        if tok > 0:
            rate[tup] = cents / Decimal(tok)
        else:
            cost_without_usage += cents
    web_rate = (cost["web_search"] / Decimal(total_web)) if total_web > 0 else Decimal("0")

    # Per-key application.
    per_key: dict = {}
    unpriced_by_tier: dict = defaultdict(int)
    for kid, rec in usage.items():
        token_cost = Decimal("0")
        unpriced = 0
        for tup, n in rec["tuples"].items():
            if tup in rate:
                token_cost += Decimal(n) * rate[tup]
            elif n > 0:
                unpriced += n
                unpriced_by_tier[tup[1]] += n  # tup[1] is service_tier
        web_cost = Decimal(rec["web_search_requests"]) * web_rate
        per_key[kid] = {
            "token_cost": token_cost,
            "web_cost": web_cost,
            "total": token_cost + web_cost,
            "unpriced_tokens": unpriced,
        }

    attributed = sum((v["total"] for v in per_key.values()), Decimal("0"))
    org_level = cost["code_execution"] + cost["session_usage"] + cost["other"]
    grand = cost["grand_total"]
    return {
        "per_key": per_key,
        "composition": {
            "tokens": sum(cost["by_tuple"].values(), Decimal("0")),
            "web_search": cost["web_search"],
            "code_execution": cost["code_execution"],
            "session_usage": cost["session_usage"],
            "other": cost["other"],
        },
        "org_level": org_level,
        "unpriced_by_tier": dict(unpriced_by_tier),
        "unpriced_tokens_total": sum(unpriced_by_tier.values()),
        "attributed": attributed,
        "grand_total": grand,
        "residual": grand - (attributed + org_level),  # ~= cost_without_usage
        "cost_without_usage": cost_without_usage,
    }


# --------------------------------------------------------------------------- #
# Formatting / output
# --------------------------------------------------------------------------- #
def _usd(cents: Decimal) -> Decimal:
    return cents / Decimal(100)


def _fmt_usd(cents: Decimal) -> str:
    return f"${_usd(cents):,.4f}"


def print_usage_table(usage: dict, key_names: dict, per_key_cost: dict) -> None:
    print("\n=== Token usage by API key (with estimated cost) ===")
    if not usage:
        print("  (no usage in this period)")
        return
    rows = []
    for kid, rec in usage.items():
        t = rec["tokens"]
        total = sum(t.values())
        rows.append((key_label(kid, key_names), t, total, rec["web_search_requests"], per_key_cost[kid]["total"]))
    rows.sort(key=lambda r: r[4], reverse=True)  # sort by estimated cost

    hdr = (f"{'API key':<34}{'uncached_in':>13}{'cache_read':>12}{'cache_create':>13}"
           f"{'output':>12}{'total_tok':>14}{'web_srch':>9}{'est_cost_usd':>15}")
    print(hdr)
    print("-" * len(hdr))
    for label, t, total, web, cost in rows:
        print(
            f"{label[:34]:<34}"
            f"{t['uncached_input_tokens']:>13,}"
            f"{t['cache_read_input_tokens']:>12,}"
            f"{(t['cache_creation.ephemeral_1h_input_tokens'] + t['cache_creation.ephemeral_5m_input_tokens']):>13,}"
            f"{t['output_tokens']:>12,}"
            f"{total:>14,}"
            f"{web:>9,}"
            f"{_fmt_usd(cost):>15}"
        )


def print_cost_breakdown(result: dict, usage: dict, key_names: dict) -> None:
    pk = result["per_key"]

    print("\n=== Per-key cost split (USD) ===")
    rows = sorted(
        ((key_label(kid, key_names), v) for kid, v in pk.items()),
        key=lambda r: r[1]["total"], reverse=True,
    )
    hdr = f"{'API key':<34}{'tokens':>16}{'web_search':>16}{'total':>16}{'unpriced_tok':>15}"
    print(hdr)
    print("-" * len(hdr))
    for label, v in rows:
        print(f"{label[:34]:<34}{_fmt_usd(v['token_cost']):>16}{_fmt_usd(v['web_cost']):>16}"
              f"{_fmt_usd(v['total']):>16}{v['unpriced_tokens']:>15,}")

    print("\n=== Org-level cost (not attributable to a single key) ===")
    comp = result["composition"]
    print(f"  {'Code execution':<28}{_fmt_usd(comp['code_execution']):>16}")
    print(f"  {'Session usage':<28}{_fmt_usd(comp['session_usage']):>16}")
    if comp["other"] > 0:
        print(f"  {'Other':<28}{_fmt_usd(comp['other']):>16}")

    if result["unpriced_tokens_total"] > 0:
        print("\n=== Unpriced usage (no dollars available from the cost endpoint) ===")
        for tier, toks in sorted(result["unpriced_by_tier"].items(), key=lambda x: -x[1]):
            print(f"  tier={tier!s:<22}{toks:>14,} tokens   $ n/a  (priority/flex bill separately)")

    print("\n=== Grand total composition (matches the cost endpoint) ===")
    print(f"  {'Tokens':<20}{_fmt_usd(comp['tokens']):>16}")
    print(f"  {'Web search':<20}{_fmt_usd(comp['web_search']):>16}")
    print(f"  {'Code execution':<20}{_fmt_usd(comp['code_execution']):>16}")
    print(f"  {'Session usage':<20}{_fmt_usd(comp['session_usage']):>16}")
    if comp["other"] > 0:
        print(f"  {'Other':<20}{_fmt_usd(comp['other']):>16}")
    print(f"  {'-' * 20}{'-' * 16}")
    print(f"  {'GRAND TOTAL':<20}{_fmt_usd(result['grand_total']):>16}")

    # Reconciliation: per-key attributed + org-level should equal the cost-endpoint total.
    recon = result["attributed"] + result["org_level"]
    gt = result["grand_total"]
    delta = result["residual"]
    pct = (delta / gt * 100) if gt else Decimal("0")
    print("\n=== Reconciliation ===")
    print(f"  attributed to keys (tokens+web): {_fmt_usd(result['attributed'])}")
    print(f"  org-level (code exec + session): {_fmt_usd(result['org_level'])}")
    print(f"  sum vs cost-endpoint total:      {_fmt_usd(recon)}  vs  {_fmt_usd(gt)}"
          f"   (Δ {_fmt_usd(delta)}, {pct:.2f}%)")
    if result["cost_without_usage"] > 0:
        print(f"  note: {_fmt_usd(result['cost_without_usage'])} of cost had no matching usage "
              f"(likely window edge — widen the range or align timestamps).")


def print_cost_table(title: str, agg: dict, name_map: dict) -> None:
    print(f"\n=== {title} ===")
    if not agg:
        print("  (no cost in this period)")
        return
    rows, total = [], Decimal("0")
    for key, cents in agg.items():
        label = "Default workspace" if key is None else name_map.get(key, key)
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
    """Resolve CLI args into a (starting_at, ending_at) RFC 3339 pair for the API.

    The window is a HALF-OPEN interval in UTC: [starting_at, ending_at).
      - starting_at is INCLUSIVE — the API returns buckets that start on or after it.
      - ending_at   is EXCLUSIVE — the API returns buckets that fall before it.
      - All times are UTC. A bare 'YYYY-MM-DD' is treated as 00:00:00Z on that day,
        NOT local midnight.

    Worked example — `--start 2025-01-01 --end 2025-02-01` resolves to the range
    2025-01-01T00:00:00Z up to (but not including) 2025-02-01T00:00:00Z. With daily
    buckets that is Jan 1 through Jan 31 — all of January. Feb 1 is NOT included.
    To include a particular end day's data, set --end to the day AFTER it
    (e.g. --end 2025-02-02 to include Feb 1).

    With --days N (and no explicit --start/--end) the window is the last N days
    INCLUDING today. `end` is set to tomorrow 00:00Z on purpose: because the bound is
    exclusive, tomorrow-00:00Z is the value that pulls today's still-accumulating
    bucket into the range. (Using today 00:00Z would stop the window at the start of
    today and drop today's data entirely.)
    """
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
    ap = argparse.ArgumentParser(description="Pull Anthropic API-key usage and per-key cost from the Admin API.")
    ap.add_argument("--days", type=int, default=7,
                    help="Days back from today, INCLUDING today (default 7). Ignored if --start/--end given.")
    ap.add_argument("--start", metavar="DATE",
                    help="Window start, INCLUSIVE (YYYY-MM-DD or RFC3339, UTC). Overrides --days.")
    ap.add_argument("--end", metavar="DATE",
                    help="Window end, EXCLUSIVE (YYYY-MM-DD or RFC3339, UTC). "
                         "e.g. '--end 2025-02-01' stops before Feb 1, giving you through Jan 31. "
                         "Defaults to tomorrow 00:00Z so today is included.")
    ap.add_argument("--csv", metavar="DIR", help="Also write CSV files into DIR.")
    args = ap.parse_args()

    starting_at, ending_at = resolve_range(args)
    session = make_session()

    print(f"Window: {starting_at}  ->  {ending_at}  (daily buckets, UTC)")
    print("Resolving API key + workspace names...")
    key_names = fetch_api_key_names(session)
    ws_names = fetch_workspace_names(session)

    print("Fetching token usage (grouped by api_key_id, model, service_tier, context_window)...")
    usage = fetch_usage(session, starting_at, ending_at)
    print("Fetching cost breakdown + deriving per-key rates...")
    cost = fetch_cost_breakdown(session, starting_at, ending_at)
    result = compute_key_costs(usage, cost)
    cost_ws = fetch_cost_by_workspace(session, starting_at, ending_at)

    print_usage_table(usage, key_names, result["per_key"])
    print_cost_breakdown(result, usage, key_names)
    print_cost_table("Cost by workspace (USD) — ground truth", cost_ws, ws_names)

    print(
        "\nNotes: per-key cost is derived from the cost endpoint's effective rates (exact for "
        "standard/batch tokens + web search).\n"
        "       priority/flex tiers have no cost rows (shown as unpriced volume); code-exec/session "
        "are org-level; Console/Workbench has no key."
    )

    if args.csv:
        out = Path(args.csv)
        out.mkdir(parents=True, exist_ok=True)
        usage_rows = []
        for kid, rec in usage.items():
            t, c = rec["tokens"], result["per_key"][kid]
            cache_create = t["cache_creation.ephemeral_1h_input_tokens"] + t["cache_creation.ephemeral_5m_input_tokens"]
            usage_rows.append([
                kid or "", key_label(kid, key_names),
                t["uncached_input_tokens"], t["cache_read_input_tokens"], cache_create, t["output_tokens"],
                sum(t.values()), rec["web_search_requests"],
                f"{_usd(c['token_cost']):.6f}", f"{_usd(c['web_cost']):.6f}", f"{_usd(c['total']):.6f}",
                c["unpriced_tokens"],
            ])
        write_csv(
            out / "usage_cost_by_api_key.csv",
            ["api_key_id", "api_key_name", "uncached_input_tokens", "cache_read_input_tokens",
             "cache_creation_input_tokens", "output_tokens", "total_tokens", "web_search_requests",
             "token_cost_usd", "web_search_cost_usd", "est_cost_usd", "unpriced_tokens"],
            usage_rows,
        )
        comp = result["composition"]
        write_csv(
            out / "cost_composition.csv",
            ["cost_type", "amount_usd"],
            [["tokens", f"{_usd(comp['tokens']):.6f}"], ["web_search", f"{_usd(comp['web_search']):.6f}"],
             ["code_execution", f"{_usd(comp['code_execution']):.6f}"], ["session_usage", f"{_usd(comp['session_usage']):.6f}"],
             ["other", f"{_usd(comp['other']):.6f}"], ["grand_total", f"{_usd(result['grand_total']):.6f}"]],
        )
        write_csv(
            out / "cost_by_workspace.csv",
            ["workspace_id", "workspace_name", "cost_usd"],
            [[wid or "", "Default workspace" if wid is None else ws_names.get(wid, wid), f"{_usd(c):.6f}"]
             for wid, c in cost_ws.items()],
        )


if __name__ == "__main__":
    main()

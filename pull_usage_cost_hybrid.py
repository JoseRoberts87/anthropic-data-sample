#!/usr/bin/env python3
"""
Per-API-key usage and cost from the Admin API, with FAST-MODE-AWARE cost attribution.

This is the hybrid variant of pull_usage_cost.py. The plain tool derives one effective
rate per (model, service_tier, context_window, token_type) from the cost endpoint and
applies it to per-key tokens. That blends fast-mode and standard usage together, because
the cost endpoint has no `speed` dimension — so a key that runs fast mode is under-charged
and a standard-heavy key over-charged (the org total still reconciles; the per-key split
skews).

This file fixes that using one published fact: fast mode is a flat per-model MULTIPLE of
standard pricing, across every token type (Opus 4.8 = 2x, Opus 4.6/4.7 = 6x; caching and
data-residency multipliers preserve the ratio). So:

    cost_endpoint(tuple) = standard_rate * (standard_tokens + M * fast_tokens)
    =>  standard_rate    = cost_endpoint(tuple) / (standard_tokens + M * fast_tokens)
        per-key standard = standard_rate * key_standard_tokens
        per-key fast     = M * standard_rate * key_fast_tokens

We get fast vs standard token counts per key by adding `speed` to the usage grouping (the
cost side stays combined — it can't split by speed). This reconciles to the cost-endpoint
total exactly, and degrades to the plain tool's behavior when there is no fast usage.

Requires an ADMIN API key (sk-ant-admin...). See pull_usage_cost.py / README.md for setup,
date-window semantics, and the response shapes. Shared plumbing is imported from
pull_usage_cost.

    export ANTHROPIC_ADMIN_KEY=sk-ant-admin01-...
    python pull_usage_cost_hybrid.py --days 30 --csv ./out
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import requests

from pull_usage_cost import (
    BASE_URL,
    DAILY_PAGE_LIMIT,
    TIMEOUT,
    _fmt_usd,
    _token_entries,
    _usd,
    fetch_api_key_names,
    fetch_cost_breakdown,
    fetch_cost_by_workspace,
    fetch_workspace_names,
    key_label,
    make_session,
    paginate_report,
    print_cost_table,
    resolve_range,
    write_csv,
)

# Beta header that exposes the `speed` dimension on the usage endpoint.
FAST_BETA = "fast-mode-2026-02-01"

# Fast-mode price multiplier over standard, per model. Fast is a flat multiple of standard
# across all token types, so we only need this integer (not a full fast price table); the
# standard rate itself still comes from the cost endpoint.
# Source: platform.claude.com/docs/en/about-claude/pricing#fast-mode-pricing
#   Opus 4.8  fast $10/$50  vs standard $5/$25  -> 2x
#   Opus 4.7  fast $30/$150 vs standard $5/$25  -> 6x
#   Opus 4.6  fast $30/$150 vs standard $5/$25  -> 6x
# Models absent here default to M=1 (i.e. blended) and are reported as a warning if they
# show fast usage — update this table when a new fast-capable model ships.
FAST_MULTIPLIER = {
    "claude-opus-4-8": 2,
    "claude-opus-4-7": 6,
    "claude-opus-4-6": 6,
}


# --------------------------------------------------------------------------- #
# Usage fetch (speed-aware, with a fallback when the fast beta isn't available)
# --------------------------------------------------------------------------- #
def _new_rec() -> dict:
    return {
        "tokens": defaultdict(int),       # token_type -> count (for the usage table)
        "tuples_std": defaultdict(int),   # (model, tier, ctx, token_type) -> standard-speed tokens
        "tuples_fast": defaultdict(int),  # (model, tier, ctx, token_type) -> fast-speed tokens
        "web_search_requests": 0,
        "web_by_model": defaultdict(int),
    }


def _accumulate(buckets, keys: dict) -> None:
    for bucket in buckets:
        for r in bucket.get("results", []):
            rec = keys[r.get("api_key_id")]  # None == Console/Workbench
            # When not grouping by speed (fallback), `speed` is null -> treat as standard.
            is_fast = (r.get("speed") == "fast")
            dims = (r.get("model"), r.get("service_tier"), r.get("context_window"))
            target = rec["tuples_fast"] if is_fast else rec["tuples_std"]
            for token_type, n in _token_entries(r).items():
                rec["tokens"][token_type] += n
                target[dims + (token_type,)] += n
            wreq = (r.get("server_tool_use") or {}).get("web_search_requests") or 0
            rec["web_search_requests"] += wreq
            rec["web_by_model"][r.get("model")] += wreq


def _paginate_usage_with_beta(session, params, beta):
    """Local paginator that sends a beta header and RAISES on HTTP errors (so the caller can
    fall back), rather than the friendly sys.exit path used elsewhere."""
    params = dict(params)
    headers = {"anthropic-beta": beta}
    while True:
        resp = session.get(f"{BASE_URL}/usage_report/messages", params=params, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
        yield from body.get("data", [])
        if body.get("has_more") and body.get("next_page"):
            params["page"] = body["next_page"]
        else:
            return


def fetch_usage_hybrid(session, starting_at: str, ending_at: str) -> tuple[dict, bool]:
    """Returns (usage, speed_separated). Tries to group by `speed` (needs the fast beta); on
    any HTTP error falls back to a non-speed grouping (fast, if present, gets blended)."""
    base_group = ["api_key_id", "model", "service_tier", "context_window"]
    params = {
        "starting_at": starting_at,
        "ending_at": ending_at,
        "bucket_width": "1d",
        "limit": DAILY_PAGE_LIMIT,
    }

    keys: dict = defaultdict(_new_rec)
    try:
        _accumulate(
            _paginate_usage_with_beta(session, {**params, "group_by[]": base_group + ["speed"]}, FAST_BETA),
            keys,
        )
        return keys, True
    except requests.HTTPError:
        # Beta not enabled / speed dimension unavailable — fall back to the friendly path.
        keys = defaultdict(_new_rec)
        _accumulate(
            paginate_report(session, "/usage_report/messages", {**params, "group_by[]": base_group}),
            keys,
        )
        return keys, False


# --------------------------------------------------------------------------- #
# Hybrid cost attribution (pure function — unit-testable without the network)
# --------------------------------------------------------------------------- #
def _empty_km() -> dict:
    return {
        "std_tokens": 0,
        "fast_tokens": 0,
        "std_cost": Decimal("0"),
        "fast_cost": Decimal("0"),
        "web_requests": 0,
        "web_cost": Decimal("0"),
        "total": Decimal("0"),
        "unpriced_tokens": 0,
    }


def compute_hybrid_costs(usage: dict, cost: dict, multipliers: dict) -> dict:
    """Decompose the cost endpoint's combined (fast+standard) token dollars using the known
    per-model fast multiplier. All money is Decimal CENTS."""
    # Denominators across all keys.
    total_std: dict = defaultdict(int)
    total_fast: dict = defaultdict(int)
    total_web = 0
    for rec in usage.values():
        for t, n in rec["tuples_std"].items():
            total_std[t] += n
        for t, n in rec["tuples_fast"].items():
            total_fast[t] += n
        total_web += rec["web_search_requests"]

    # Recover the standard rate per tuple from the fast-weighted token count.
    rate: dict = {}
    cost_without_usage = Decimal("0")
    unknown_m_models: set = set()
    for t, cents in cost["by_tuple"].items():
        model = t[0]
        m = multipliers.get(model, 1)
        fast = total_fast.get(t, 0)
        if fast > 0 and model not in multipliers:
            unknown_m_models.add(model)  # m stays 1 -> this model's fast usage is blended
        weighted = total_std.get(t, 0) + m * fast
        if weighted > 0:
            rate[t] = cents / Decimal(weighted)
        else:
            cost_without_usage += cents
    web_rate = (cost["web_search"] / Decimal(total_web)) if total_web > 0 else Decimal("0")

    per_key: dict = {}
    per_key_model: dict = defaultdict(_empty_km)
    unpriced_by_tier: dict = defaultdict(int)
    fast_tokens_total = 0
    for kid, rec in usage.items():
        std_cost = Decimal("0")
        fast_cost = Decimal("0")
        unpriced = 0

        for t, n in rec["tuples_std"].items():
            km = per_key_model[(kid, t[0])]
            km["std_tokens"] += n
            if t in rate:
                c = Decimal(n) * rate[t]
                std_cost += c
                km["std_cost"] += c
            elif n > 0:
                unpriced += n
                unpriced_by_tier[t[1]] += n
                km["unpriced_tokens"] += n

        for t, n in rec["tuples_fast"].items():
            m = multipliers.get(t[0], 1)
            km = per_key_model[(kid, t[0])]
            km["fast_tokens"] += n
            fast_tokens_total += n
            if t in rate:
                c = Decimal(n) * Decimal(m) * rate[t]
                fast_cost += c
                km["fast_cost"] += c
            elif n > 0:
                unpriced += n
                unpriced_by_tier[t[1]] += n
                km["unpriced_tokens"] += n

        for model, reqs in rec.get("web_by_model", {}).items():
            km = per_key_model[(kid, model)]
            km["web_requests"] += reqs
            km["web_cost"] += Decimal(reqs) * web_rate

        web_cost = Decimal(rec["web_search_requests"]) * web_rate
        per_key[kid] = {
            "std_cost": std_cost,
            "fast_cost": fast_cost,
            "web_cost": web_cost,
            "total": std_cost + fast_cost + web_cost,
            "unpriced_tokens": unpriced,
            "fast_tokens": sum(rec["tuples_fast"].values()),
        }
    for km in per_key_model.values():
        km["total"] = km["std_cost"] + km["fast_cost"] + km["web_cost"]

    attributed = sum((v["total"] for v in per_key.values()), Decimal("0"))
    org_level = cost["code_execution"] + cost["session_usage"] + cost["other"]
    grand = cost["grand_total"]
    return {
        "per_key": per_key,
        "per_key_model": dict(per_key_model),
        "composition": {
            "tokens": sum(cost["by_tuple"].values(), Decimal("0")),
            "web_search": cost["web_search"],
            "code_execution": cost["code_execution"],
            "session_usage": cost["session_usage"],
            "other": cost["other"],
        },
        "org_level": org_level,
        "attributed": attributed,
        "grand_total": grand,
        "residual": grand - (attributed + org_level),
        "cost_without_usage": cost_without_usage,
        "unpriced_by_tier": dict(unpriced_by_tier),
        "unpriced_tokens_total": sum(unpriced_by_tier.values()),
        "fast_tokens_total": fast_tokens_total,
        "unknown_m_models": sorted(m for m in unknown_m_models if m is not None),
    }


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def print_mode_banner(speed_separated: bool, result: dict) -> None:
    if not speed_separated:
        print("\n[!] Could not group usage by speed (fast-mode beta unavailable on this org/key).")
        print("    Fast usage, if any, is BLENDED into standard rates — results match the plain tool.")
    elif result["fast_tokens_total"] == 0:
        print("\n[i] No fast-mode usage in this window — per-key cost is exact (identical to standard).")
    else:
        print(f"\n[i] Fast-mode usage decomposed via per-model multipliers "
              f"({result['fast_tokens_total']:,} fast tokens).")
    for model in result["unknown_m_models"]:
        print(f"[!] No fast multiplier known for {model} — its fast usage is BLENDED. "
              f"Add it to FAST_MULTIPLIER.")


def print_usage_table(usage: dict, key_names: dict, per_key: dict) -> None:
    print("\n=== Token usage by API key (with estimated cost) ===")
    if not usage:
        print("  (no usage in this period)")
        return
    rows = []
    for kid, rec in usage.items():
        rows.append((key_label(kid, key_names), sum(rec["tokens"].values()),
                     per_key[kid]["fast_tokens"], per_key[kid]["total"]))
    rows.sort(key=lambda r: r[3], reverse=True)

    hdr = f"{'API key':<34}{'total_tok':>16}{'fast_tok':>16}{'est_cost_usd':>15}"
    print(hdr)
    print("-" * len(hdr))
    for label, total, fast, cost in rows:
        print(f"{label[:34]:<34}{total:>16,}{fast:>16,}{_fmt_usd(cost):>15}")


def print_cost_split(per_key: dict, key_names: dict) -> None:
    print("\n=== Per-key cost split (USD) ===")
    rows = sorted(((key_label(kid, key_names), v) for kid, v in per_key.items()),
                  key=lambda r: r[1]["total"], reverse=True)
    hdr = f"{'API key':<30}{'standard':>14}{'fast':>14}{'web_search':>14}{'total':>14}{'unpriced_tok':>15}"
    print(hdr)
    print("-" * len(hdr))
    for label, v in rows:
        print(f"{label[:30]:<30}{_fmt_usd(v['std_cost']):>14}{_fmt_usd(v['fast_cost']):>14}"
              f"{_fmt_usd(v['web_cost']):>14}{_fmt_usd(v['total']):>14}{v['unpriced_tokens']:>15,}")


def print_by_model(per_key_model: dict, per_key: dict, key_names: dict) -> None:
    print("\n=== Usage & cost by API key x model ===")
    if not per_key_model:
        print("  (no usage in this period)")
        return
    by_key: dict = defaultdict(list)
    for (kid, model), km in per_key_model.items():
        by_key[kid].append((model, km))
    ordered = sorted(by_key, key=lambda k: per_key[k]["total"], reverse=True)

    hdr = f"{'API key':<28}{'model':<22}{'total_tok':>15}{'fast_tok':>14}{'est_cost_usd':>15}"
    print(hdr)
    print("-" * len(hdr))
    for kid in ordered:
        label = key_label(kid, key_names)
        for model, km in sorted(by_key[kid], key=lambda mk: mk[1]["total"], reverse=True):
            total_tok = km["std_tokens"] + km["fast_tokens"]
            print(f"{label[:28]:<28}{(model or '(unknown)')[:22]:<22}{total_tok:>15,}"
                  f"{km['fast_tokens']:>14,}{_fmt_usd(km['total']):>15}")


def print_breakdown(result: dict) -> None:
    comp = result["composition"]

    print("\n=== Org-level cost (not attributable to a single key) ===")
    print(f"  {'Code execution':<28}{_fmt_usd(comp['code_execution']):>16}")
    print(f"  {'Session usage':<28}{_fmt_usd(comp['session_usage']):>16}")
    if comp["other"] > 0:
        print(f"  {'Other':<28}{_fmt_usd(comp['other']):>16}")

    if result["unpriced_tokens_total"] > 0:
        print("\n=== Unpriced usage (no dollars available from the cost endpoint) ===")
        for tier, toks in sorted(result["unpriced_by_tier"].items(), key=lambda x: -x[1]):
            print(f"  tier={tier!s:<22}{toks:>14,} tokens   $ n/a  (priority/flex bill separately)")

    print("\n=== Grand total composition (matches the cost endpoint) ===")
    print(f"  {'Tokens (std+fast)':<20}{_fmt_usd(comp['tokens']):>16}")
    print(f"  {'Web search':<20}{_fmt_usd(comp['web_search']):>16}")
    print(f"  {'Code execution':<20}{_fmt_usd(comp['code_execution']):>16}")
    print(f"  {'Session usage':<20}{_fmt_usd(comp['session_usage']):>16}")
    if comp["other"] > 0:
        print(f"  {'Other':<20}{_fmt_usd(comp['other']):>16}")
    print(f"  {'-' * 20}{'-' * 16}")
    print(f"  {'GRAND TOTAL':<20}{_fmt_usd(result['grand_total']):>16}")

    recon = result["attributed"] + result["org_level"]
    gt = result["grand_total"]
    delta = result["residual"]
    pct = (delta / gt * 100) if gt else Decimal("0")
    print("\n=== Reconciliation ===")
    print(f"  attributed to keys (std+fast+web): {_fmt_usd(result['attributed'])}")
    print(f"  org-level (code exec + session):   {_fmt_usd(result['org_level'])}")
    print(f"  sum vs cost-endpoint total:        {_fmt_usd(recon)}  vs  {_fmt_usd(gt)}"
          f"   (Δ {_fmt_usd(delta)}, {pct:.2f}%)")
    if result["cost_without_usage"] > 0:
        print(f"  note: {_fmt_usd(result['cost_without_usage'])} of cost had no matching usage "
              f"(likely window edge).")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Per-API-key usage and fast-mode-aware cost (hybrid).")
    ap.add_argument("--days", type=int, default=7,
                    help="Days back from today, INCLUDING today (default 7). Ignored if --start/--end given.")
    ap.add_argument("--start", metavar="DATE", help="Window start, INCLUSIVE (YYYY-MM-DD or RFC3339, UTC).")
    ap.add_argument("--end", metavar="DATE", help="Window end, EXCLUSIVE (YYYY-MM-DD or RFC3339, UTC).")
    ap.add_argument("--csv", metavar="DIR", help="Also write CSV files into DIR.")
    args = ap.parse_args()

    starting_at, ending_at = resolve_range(args)
    session = make_session()

    print(f"Window: {starting_at}  ->  {ending_at}  (daily buckets, UTC)")
    print("Resolving API key + workspace names...")
    key_names = fetch_api_key_names(session)
    ws_names = fetch_workspace_names(session)

    print("Fetching token usage (grouped by api_key_id, model, service_tier, context_window, speed)...")
    usage, speed_separated = fetch_usage_hybrid(session, starting_at, ending_at)
    print("Fetching cost breakdown + deriving fast-mode-aware per-key rates...")
    cost = fetch_cost_breakdown(session, starting_at, ending_at)
    result = compute_hybrid_costs(usage, cost, FAST_MULTIPLIER)
    cost_ws = fetch_cost_by_workspace(session, starting_at, ending_at)

    print_mode_banner(speed_separated, result)
    print_usage_table(usage, key_names, result["per_key"])
    print_cost_split(result["per_key"], key_names)
    print_by_model(result["per_key_model"], result["per_key"], key_names)
    print_breakdown(result)
    print_cost_table("Cost by workspace (USD) — ground truth", cost_ws, ws_names)

    print(
        "\nHybrid method: standard rates are derived from the cost endpoint; fast tokens are "
        "priced at M x the standard rate (M from FAST_MULTIPLIER).\n"
        "       Fast cost is exact when M is correct, and reconciles to the cost-endpoint total. "
        "priority/flex remain unpriced; code-exec/session are org-level."
    )

    if args.csv:
        out = Path(args.csv)
        out.mkdir(parents=True, exist_ok=True)
        key_rows = []
        for kid, rec in usage.items():
            t, c = rec["tokens"], result["per_key"][kid]
            cache_create = (t.get("cache_creation.ephemeral_1h_input_tokens", 0)
                            + t.get("cache_creation.ephemeral_5m_input_tokens", 0))
            key_rows.append([
                kid or "", key_label(kid, key_names),
                t.get("uncached_input_tokens", 0), t.get("cache_read_input_tokens", 0), cache_create,
                t.get("output_tokens", 0), sum(t.values()), c["fast_tokens"], rec["web_search_requests"],
                f"{_usd(c['std_cost']):.6f}", f"{_usd(c['fast_cost']):.6f}", f"{_usd(c['web_cost']):.6f}",
                f"{_usd(c['total']):.6f}", c["unpriced_tokens"],
            ])
        write_csv(
            out / "usage_cost_by_api_key.csv",
            ["api_key_id", "api_key_name", "uncached_input_tokens", "cache_read_input_tokens",
             "cache_creation_input_tokens", "output_tokens", "total_tokens", "fast_tokens",
             "web_search_requests", "standard_cost_usd", "fast_cost_usd", "web_search_cost_usd",
             "est_cost_usd", "unpriced_tokens"],
            key_rows,
        )
        model_rows = []
        for (kid, model), km in result["per_key_model"].items():
            model_rows.append([
                kid or "", key_label(kid, key_names), model or "",
                km["std_tokens"], km["fast_tokens"], km["std_tokens"] + km["fast_tokens"],
                f"{_usd(km['std_cost']):.6f}", f"{_usd(km['fast_cost']):.6f}",
                f"{_usd(km['web_cost']):.6f}", f"{_usd(km['total']):.6f}", km["unpriced_tokens"],
            ])
        model_rows.sort(key=lambda r: (r[1], r[0]))
        write_csv(
            out / "usage_cost_by_api_key_model.csv",
            ["api_key_id", "api_key_name", "model", "standard_tokens", "fast_tokens", "total_tokens",
             "standard_cost_usd", "fast_cost_usd", "web_search_cost_usd", "est_cost_usd", "unpriced_tokens"],
            model_rows,
        )
        comp = result["composition"]
        write_csv(
            out / "cost_composition.csv",
            ["cost_type", "amount_usd"],
            [["tokens", f"{_usd(comp['tokens']):.6f}"], ["web_search", f"{_usd(comp['web_search']):.6f}"],
             ["code_execution", f"{_usd(comp['code_execution']):.6f}"],
             ["session_usage", f"{_usd(comp['session_usage']):.6f}"], ["other", f"{_usd(comp['other']):.6f}"],
             ["grand_total", f"{_usd(result['grand_total']):.6f}"]],
        )
        write_csv(
            out / "cost_by_workspace.csv",
            ["workspace_id", "workspace_name", "cost_usd"],
            [[wid or "", "Default workspace" if wid is None else ws_names.get(wid, wid), f"{_usd(c):.6f}"]
             for wid, c in cost_ws.items()],
        )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Reshape the usage tool's per-(api_key, model) output INTO the Console cost-export STRUCTURE.

INPUT  — out/usage_cost_by_api_key_model.csv, produced by pull_usage_cost.py or
         pull_usage_cost_hybrid.py (with --csv). One row per (api_key, model). The base tool
         writes `token_cost_usd` / `web_search_cost_usd`; the hybrid tool additionally writes
         `standard_cost_usd` / `fast_cost_usd` (the fast/standard split). Both are auto-detected.
TARGET — the column structure of a Claude Console cost export (data/claude_api_cost_*.csv):
         usage_date_utc, model, workspace, api_key, usage_type, context_window, token_type,
         cost_usd, list_price_usd, cost_type, inference_geo, speed

The usage-tool output is aggregated to (api_key, model), so dimensions the Console export
splits on but we don't carry — workspace, context_window, token_type, inference_geo — are
emitted as "--", the same not-applicable marker the export itself uses. Each (api_key, model)
becomes:
  - one `token` row per speed present: `standard` always, and `fast` when the hybrid split is
    in the input and non-zero. (Base input -> `standard` only, since it has no fast data.)
  - one `web_search` row per api_key (model "--", matching the export's convention) when there
    is web-search cost.

`cost_usd` is emitted at full input precision (6 dp), not rounded to whole cents like the
Console export, so no sub-cent cost is lost; `list_price_usd` mirrors `cost_usd` (the input
carries no separate list price). Pure CSV reshaping — no API calls.

    python shape_usage_to_cost_export.py
    python shape_usage_to_cost_export.py out/usage_cost_by_api_key_model.csv --date 2026-06-15 \
        --out data/api_cost_by_key_model.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

DEFAULT_INPUT = "out/usage_cost_by_api_key_model.csv"
NA = "--"  # the Console export's not-applicable marker

# Target column order (the Console cost export structure).
CONSOLE_COLUMNS = [
    "usage_date_utc", "model", "workspace", "api_key", "usage_type", "context_window",
    "token_type", "cost_usd", "list_price_usd", "cost_type", "inference_geo", "speed",
]

# claude-opus-4-8 -> "Claude Opus 4.8"; claude-haiku-4-5-20251001 -> "Claude Haiku 4.5".
# Version parts are 1-2 digits; an 6-8 digit trailing group is a date snapshot and is dropped.
_MODEL_RE = re.compile(r"^claude-(opus|sonnet|haiku|fable|mythos)-(\d{1,2}(?:-\d{1,2})*)(?:-\d{6,8})?$")


def model_display(model_id: str) -> str:
    m = _MODEL_RE.match(model_id or "")
    if not m:
        return model_id or NA
    family, version = m.group(1), m.group(2)
    return f"Claude {family.capitalize()} {version.replace('-', '.')}"


def _d(row: dict, col: str) -> Decimal:
    return Decimal(row.get(col) or "0")


def _money(d: Decimal) -> str:
    return f"{d:.6f}"


def transform(rows: list[dict], date: str) -> tuple[list[dict], Decimal, Decimal, bool]:
    """Returns (console_rows, input_total, output_total, hybrid_input)."""
    cols = set(rows[0].keys()) if rows else set()
    hybrid = {"standard_cost_usd", "fast_cost_usd"} <= cols
    if not hybrid and "token_cost_usd" not in cols:
        sys.exit("ERROR: input has neither token_cost_usd (base) nor standard/fast_cost_usd (hybrid) — "
                 "is this a usage_cost_by_api_key_model.csv from pull_usage_cost[_hybrid].py?")

    out: list[dict] = []
    web_by_key: dict = defaultdict(lambda: Decimal("0"))
    input_total = Decimal("0")
    output_total = Decimal("0")

    def emit(model, api_key, usage_type, cost_type, speed, cost):
        nonlocal output_total
        out.append({
            "usage_date_utc": date, "model": model, "workspace": NA, "api_key": api_key,
            "usage_type": usage_type, "context_window": NA, "token_type": NA,
            "cost_usd": _money(cost), "list_price_usd": _money(cost),
            "cost_type": cost_type, "inference_geo": NA, "speed": speed,
        })
        output_total += cost

    for row in rows:
        input_total += _d(row, "est_cost_usd") if "est_cost_usd" in cols else (
            (_d(row, "standard_cost_usd") + _d(row, "fast_cost_usd")) if hybrid else _d(row, "token_cost_usd")
        ) + _d(row, "web_search_cost_usd")

        api_key = row.get("api_key_name") or row.get("api_key_id") or NA
        disp = model_display(row.get("model", ""))

        if hybrid:
            speed_costs = [("standard", _d(row, "standard_cost_usd")), ("fast", _d(row, "fast_cost_usd"))]
        else:
            speed_costs = [("standard", _d(row, "token_cost_usd"))]
        for speed, cost in speed_costs:
            if speed == "fast" and cost == 0:
                continue  # no fast usage -> no fast row
            emit(disp, api_key, "message", "token", speed, cost)

        web = _d(row, "web_search_cost_usd")
        if web != 0:
            web_by_key[api_key] += web

    # Web search: one row per api_key, model "--", per the export's convention.
    for api_key, web in web_by_key.items():
        emit(NA, api_key, NA, "web_search", NA, web)

    out.sort(key=lambda r: (Decimal(r["cost_usd"]), r["api_key"]), reverse=True)
    return out, input_total, output_total, hybrid


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Reshape usage_cost_by_api_key_model.csv into the Console cost-export structure.")
    ap.add_argument("input", nargs="?", default=DEFAULT_INPUT,
                    help=f"Usage tool per-(key,model) CSV. Default: {DEFAULT_INPUT}")
    ap.add_argument("--date", default=NA,
                    help='Value for usage_date_utc (the input carries no date). Default "--".')
    ap.add_argument("--out", metavar="PATH", help="Output CSV. Default: <input>_console_format.csv next to input.")
    args = ap.parse_args()

    in_path = Path(args.input)
    if not in_path.is_file():
        sys.exit(f"ERROR: input not found: {in_path}")
    out_path = Path(args.out) if args.out else in_path.with_name(in_path.stem + "_console_format.csv")

    with open(in_path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        sys.exit(f"ERROR: {in_path} is empty.")

    console_rows, input_total, output_total, hybrid = transform(rows, args.date)

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CONSOLE_COLUMNS)
        w.writeheader()
        w.writerows(console_rows)

    print(f"Read   {len(rows)} (api_key, model) rows from {in_path}  [{'hybrid' if hybrid else 'base'} input]")
    print(f"Wrote  {len(console_rows)} Console-format rows to {out_path}")
    print(f"Total  ${output_total:.6f}  (input est ${input_total:.6f}, Δ ${output_total - input_total:.6f})")
    if not hybrid:
        print('Note:  base input has no fast/standard split — every token row is speed="standard".')
        print("       To populate speed=\"fast\", regenerate the input with pull_usage_cost_hybrid.py --csv out.")
    print('Note:  workspace / context_window / token_type / inference_geo are "--" (not carried at '
          "(api_key, model) grain); cost_usd is full precision, not cent-rounded like the Console export.")


if __name__ == "__main__":
    main()

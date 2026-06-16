# Anthropic API usage & cost puller

A small Python script (`pull_usage_cost.py`) that pulls your organization's **token usage**
and **USD cost** from the Anthropic [Admin API](https://platform.claude.com/docs/en/manage-claude/admin-api),
and estimates **cost per API key** — including a breakdown by model.

It talks to four endpoints under `https://api.anthropic.com/v1/organizations`:

| Endpoint | Used for |
|---|---|
| `GET /usage_report/messages` | token usage, grouped by `api_key_id` × `model` × `service_tier` × `context_window` |
| `GET /cost_report` | USD cost (groupable only by `workspace_id` / `description`) |
| `GET /api_keys` | resolve `api_key_id` → name |
| `GET /workspaces` | resolve `workspace_id` → name |

## Requirements

- **Python 3.9+** (tested on 3.13)
- **An Admin API key** (`sk-ant-admin01-...`) — *not* a regular `sk-ant-api...` key.
  Only an org **admin** can create one, in the Console under
  [Settings → Admin keys](https://platform.claude.com/settings/admin-keys).
  Regular keys get a `401` on these endpoints.
- One dependency: `requests` (see `requirements.txt`).

> **On Claude Enterprise (claude.ai)?** That's a different product with a different API
> (an Analytics API key, not an Admin key). This script targets the Claude **Console /
> Developer Platform**. See [Which API do you need?](https://platform.claude.com/docs/en/api/usage-cost-api#which-api-do-you-need).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export ANTHROPIC_ADMIN_KEY=sk-ant-admin01-...
```

## Usage

```bash
.venv/bin/python pull_usage_cost.py                      # last 7 days (incl. today)
.venv/bin/python pull_usage_cost.py --days 30
.venv/bin/python pull_usage_cost.py --start 2025-01-01 --end 2025-02-01
.venv/bin/python pull_usage_cost.py --days 30 --csv ./out   # also write CSVs to ./out
```

| Flag | Meaning |
|---|---|
| `--days N` | Last `N` days back from today, **including** today (default 7). Ignored if `--start`/`--end` given. |
| `--start DATE` | Window start, **inclusive** (`YYYY-MM-DD` or RFC 3339, UTC). |
| `--end DATE` | Window end, **exclusive** (`YYYY-MM-DD` or RFC 3339, UTC). |
| `--csv DIR` | Also write CSV files into `DIR`. |

### Date windows are half-open (UTC)

The window is `[start, end)` in **UTC** — start inclusive, end exclusive, and a bare
`YYYY-MM-DD` means `00:00:00Z` that day (not local time). So:

```
--start 2025-01-01 --end 2025-02-01   →   all of January (Jan 1 … Jan 31). Feb 1 is NOT included.
```

To include a given end day, set `--end` to the day after it (`--end 2025-02-02` to include Feb 1).
This is also why `--days` uses tomorrow-`00:00Z` as the bound: the exclusive end is what pulls
today's still-accumulating data into the window.

## What it prints

```
=== Token usage by API key (with estimated cost) ===   per-key token totals + est_cost_usd
=== Usage & cost by API key x model ===                 the same, broken down by model
=== Per-key cost split (USD) ===                        tokens $ | web_search $ | total | unpriced_tok
=== Org-level cost (not attributable to a single key)   code execution, session usage
=== Unpriced usage ===                                  priority/flex token volume ($ n/a)
=== Grand total composition ===                         tokens / web / code / session = grand total
=== Reconciliation ===                                  Σ per-key + org-level  vs  cost endpoint (Δ %)
=== Cost by workspace (USD) — ground truth ===          exact USD straight from the cost endpoint
```

With `--csv DIR` it also writes:

| File | Grain |
|---|---|
| `usage_cost_by_api_key.csv` | one row per API key |
| `usage_cost_by_api_key_model.csv` | one row per (API key, model) |
| `cost_composition.csv` | grand total split by cost type |
| `cost_by_workspace.csv` | USD per workspace (ground truth) |

## How per-key cost is estimated

The cost endpoint **cannot** group by `api_key_id`, so per-key dollars are *derived*, not
returned. The script computes effective rates straight from your bill and applies them to
per-key token counts:

```
rate(model, tier, context_window, token_type) = cost_amount / total_tokens     # from the cost endpoint
per_key_cost(key)                              = Σ  per_key_tokens × rate
```

The cost endpoint's `token_type` strings (`uncached_input_tokens`, `output_tokens`,
`cache_read_input_tokens`, `cache_creation.ephemeral_{1h,5m}_input_tokens`) match the usage
fields 1:1, so the join is exact and per-key estimates **sum back to the cost-endpoint total**
(that's what the *Reconciliation* line proves — Δ should be ~0%).

### Caveats it surfaces honestly

- **Priority / flex tiers** aren't in the cost endpoint at all → reported as token *volume*
  with `$ n/a` (their dollars come from your committed-capacity contract, not any API).
- **Code execution / session** costs have dollars but can't be split per key (the usage
  endpoint doesn't break them out per key) → shown as an **org-level** line.
- **Web search** *is* attributable per key (requests × derived rate).
- **Console/Workbench** traffic has no `api_key_id` → shown as its own pseudo-key.
- **Default workspace** has no `workspace_id`.
- Data typically appears within **~5 minutes**; the API supports polling about **once per minute**.

## Endpoint response shapes

Trimmed to the fields this tool uses, with the gotchas called out. All four return
RFC 3339 timestamps and paginate, but the two report endpoints and the two list endpoints
paginate *differently* (see notes).

### `GET /usage_report/messages`

```json
{
  "data": [
    {
      "starting_at": "2025-08-01T00:00:00Z",
      "ending_at": "2025-08-02T00:00:00Z",
      "results": [
        {
          "api_key_id": "apikey_01Rj2N8SVvo6BePZj99NhmiT",
          "workspace_id": "wrkspc_01JwQvzr7rXLA5AGx3HKfFUJ",
          "model": "claude-opus-4-8",
          "service_tier": "standard",
          "context_window": "0-200k",
          "uncached_input_tokens": 1500,
          "cache_read_input_tokens": 200,
          "cache_creation": {
            "ephemeral_1h_input_tokens": 1000,
            "ephemeral_5m_input_tokens": 500
          },
          "output_tokens": 500,
          "server_tool_use": { "web_search_requests": 10 },
          "account_id": "user_01...",
          "service_account_id": "svac_01...",
          "inference_geo": "global"
        }
      ]
    }
  ],
  "has_more": true,
  "next_page": "page_..."
}
```

- One `results[]` entry per group-by combination present in a time bucket. Dimension fields
  (`api_key_id`, `workspace_id`, `model`, `service_tier`, `context_window`, …) are `null`
  unless you group by them.
- `api_key_id` is `null` for Console/Workbench traffic; `workspace_id` is `null` for the
  default workspace.
- Pagination: `has_more` + `next_page` → pass `next_page` back as the `page` query param.

### `GET /cost_report`

```json
{
  "data": [
    {
      "starting_at": "2025-08-01T00:00:00Z",
      "ending_at": "2025-08-02T00:00:00Z",
      "results": [
        {
          "amount": "123.78912",
          "currency": "USD",
          "cost_type": "tokens",
          "description": "Claude Sonnet 4 Usage - Input Tokens",
          "model": "claude-opus-4-8",
          "token_type": "uncached_input_tokens",
          "service_tier": "standard",
          "context_window": "0-200k",
          "workspace_id": "wrkspc_01JwQvzr7rXLA5AGx3HKfFUJ",
          "inference_geo": "global"
        }
      ]
    }
  ],
  "has_more": true,
  "next_page": "page_..."
}
```

- `amount` is a **decimal string in cents** (`"123.78912"` = `$1.2378912`) — divide by 100 for USD.
- `cost_type` ∈ `tokens` | `web_search` | `code_execution` | `session_usage`. The
  `model` / `token_type` / `service_tier` / `context_window` fields are populated only for
  `cost_type: "tokens"`, and only when grouping by `description`.
- `token_type` strings match the usage fields 1:1 — that's the join that powers per-key cost.
- Group-by is limited to `workspace_id` and `description` (there is **no** `api_key_id`).
- Same `has_more` + `next_page` pagination as the usage report.

### `GET /api_keys`

```json
{
  "data": [
    {
      "id": "apikey_01Rj2N8SVvo6BePZj99NhmiT",
      "name": "Developer Key",
      "workspace_id": "wrkspc_01JwQvzr7rXLA5AGx3HKfFUJ",
      "status": "active",
      "partial_key_hint": "sk-ant-api03-R2D...igAA",
      "type": "api_key",
      "created_at": "2024-10-30T23:58:27.427722Z",
      "created_by": { "id": "user_01...", "type": "user" },
      "expires_at": null
    }
  ],
  "first_id": "apikey_...",
  "has_more": true,
  "last_id": "apikey_..."
}
```

- `status` ∈ `active` | `inactive` | `archived` | `expired`; `workspace_id` is `null` for the
  default workspace.
- Pagination: cursor-based — pass `last_id` as `after_id` for the next page (**different** from
  the reports' `next_page`/`page`).

### `GET /workspaces`

```json
{
  "data": [
    {
      "id": "wrkspc_01JwQvzr7rXLA5AGx3HKfFUJ",
      "name": "Workspace Name",
      "type": "workspace",
      "created_at": "2024-10-30T23:58:27.427722Z",
      "archived_at": null,
      "display_color": "#6C5BB9"
    }
  ],
  "first_id": "wrkspc_...",
  "has_more": true,
  "last_id": "wrkspc_..."
}
```

- The script uses only `id` and `name`. Other fields exist (`data_residency`, `tags`, and the
  CMEK fields `compartment_id` / `external_key_id`); pass `include_archived=true` to include
  archived workspaces.
- Same cursor pagination as API keys (`last_id` → `after_id`).

## Notes

- The API is read-only here; the script never creates or modifies keys.
- All money is handled as `Decimal` cents internally and only converted to USD for display.
- The cost-attribution math lives in a pure `compute_key_costs()` function, separate from the
  HTTP calls, so it's straightforward to test offline.

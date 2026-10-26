# STATUS — pantry-inventory-mcp-server

Date: 2026-10-26
Owner: Asdale Dynamics, LLC

## What was built

A small, offline-first MCP (Model Context Protocol) server over stdio that
manages a local pantry and freezer inventory:

- **10 tools**: `add_item`, `consume_item`, `remove_item`, `list_items`,
  `expiring_soon`, `add_recipe`, `remove_recipe`, `list_recipes`,
  `what_can_i_cook`, `shopping_gaps`.
- **Add/consume with quantities, units, locations, and ISO dates.** Merging on
  re-add; consume refuses to overdraw and removes the item at zero; a capped
  history log records add/consume/remove events.
- **Expiry logic**: `expiring_soon(n)` (default 7 days) always includes
  already-expired items marked `EXPIRED`, oldest first. Expired stock is not
  counted as "usable" for cooking or gap lists.
- **Recipe notes paired to the pantry**: name + ingredients (optional
  quantity/unit) + optional instructions. `what_can_i_cook` buckets recipes
  into ready / almost there / not started, with plural matching
  (`onions` covers `onion`, `eggs` covers `egg`, `tomatoes` covers `tomato`).
- **Shopping-gap list**: aggregates missing ingredients across all recipes
  (by number of recipes affected) plus an "expired — replace or discard"
  section.
- **Storage**: single JSON file (`pantry-inventory/1` format) with:
  - atomic writes (temp file + rename);
  - automatic timestamped backups before every change, pruned to N (default 10),
    skipped when content is unchanged;
  - corrupt-file recovery (original archived as `pantry-corrupt-*.json`,
    warn on stderr, start fresh).
- **Path safety**: storage location comes only from local CLI flags — never
  tool input. `..` segments rejected; system directories (`/etc`, `/usr`,
  `/var`, `/root`, ...) and `/` refused; symlinks resolving into forbidden
  areas refused; `pantry.json`/`backups/` stay inside the one resolved data
  directory regardless of item names.
- **Sample pantry**: `python3 pantry_mcp.py --load-sample` seeds 15 items
  (pantry/fridge/freezer, a few expiring soon) and 3 recipes with expiry
  dates relative to today; shipped static reference snapshot at
  `samples/sample_pantry.json`. First run against a fresh data dir auto-seeds
  the same sample (`--no-sample` to opt out).
- **MCP protocol subset (JSON-RPC 2.0, line-delimited, stdio)**:
  `initialize` (with version negotiation among 2024-11-05 / 2025-03-26 /
  2025-06-18), `notifications/initialized` and `*` notifications, `ping`,
  `tools/list`, `tools/call` with full JSON-Schema `inputSchema`s; unknown
  method → `-32601`, unknown tool/bad params → `-32602`, bad JSON → `-32700`,
  expected errors (overdraw, missing item) → `isError: true` results, not
  protocol errors.

No third-party dependencies, no network calls, no secrets, no personal data.

## How to run it

```bash
python3 pantry_mcp.py --load-sample      # optional: seed sample data with today-dated expiries
python3 pantry_mcp.py                    # serve on stdio (what your MCP client runs)
```

Point an MCP client at `python3 /path/to/pantry_mcp.py` (config snippet in
README). Helpful flags: `--data-dir DIR` (default `./data` next to the
script), `--backup-keep N`, `--no-sample`, `--load-sample [--force]`.

Manual protocol exercise without an MCP client (echo JSON-RPC lines into
stdin; see README for a copy-paste `printf` one-liner).

## Verification results (all run 2026-10-26)

- `python3 -m py_compile pantry_mcp.py test_pantry.py smoke_test.py` — clean.
- `python3 -m unittest test_pantry.py` — **58 tests, OK**. Coverage includes:
  add/consume/remove round-trips across save/load, merge + earliest-expiry
  rules, overdraw/unknown-item errors, location filter + sorting,
  expiring windows (0/7/30 days) and expired-first ordering, recipe
  validation, recipe-vs-stock matching (plural/quantity/expired),
  shopping-gap aggregation and ordering, backup creation/pruning/no-op
  skipping, corrupt-file recovery with archive, invalid-row skipping,
  JSON date serialization round-trips, sample-data integrity (code sample and
  shipped file), all server protocol paths (initialize negotiation, ping,
  tools/list, tool calls, isError results, -32601/-32602/-32700, notifications),
  and the CLI (first-run seeding, --no-sample, --force gate, refused system
  directory).
- `python3 smoke_test.py` — **12-step end-to-end**: real subprocess over
  stdio; initialize/initialized/tools/list; add/consume/expiring flows;
  recipe matching; shopping gaps; data-file + auto-backup on disk checks;
  `-32602`/`-32700` error paths; clean exit on EOF.
- Manual stdio pipecheck: initialize → correct serverInfo + capabilities;
  notification produces no stdout line.
- `pantry_mcp.py --version` → `pantry-inventory-mcp-server 1.0.0`.
- Security cases exercised by tests: `..` in data dir rejected; system paths
  and `/` rejected; symlink escape into `/etc` rejected (symlink to a valid
  local dir resolves fine); hostile item names (`../../outside/red`) can
  neither escape nor move `pantry.json`.

## Known gaps / honest notes

- Protocol subset only: `initialize`, `ping`, `tools/list`, `tools/call`, and
  notifications. No resources, prompts, logging, sampling, or completions
  (those surface as `-32601`, which is the standard "not found" signal).
- Single-process, single-user storage: no cross-process file locking, so two
  pantry servers pointed at the same `pantry.json` could race. Point one
  client at one data dir.
- Quantities are plain floats with no unit conversion ("1 cup" + "1 gallon"
  will happily add up); expiry is day-resolution with no timezone awareness
  (local time).
- Plural matching is heuristic (`buses` → `bus`, `berries` → `berry`); odd
  ingredient spellings still need exact synonyms or renaming.
- The shipped `samples/sample_pantry.json` has frozen dates and will read as
  "expired" if loaded months later — use `--load-sample` for today-dated
  demo data instead.
- `tools/call` with an unknown `protocolVersion`-style junk params are
  tolerated rather than strictly validated everywhere; tool `inputSchema`s
  are minimal but real (`type`/`required`/`minimum` where it matters).

## Complements, no overlap

Alongside notes-, flashcards-, bookshelf-, and checklists-mcp-server, this
server keeps one narrow job — physical food inventory — with its own storage
and tool namespace. Nothing in those projects' data formats or domains is
duplicated here.

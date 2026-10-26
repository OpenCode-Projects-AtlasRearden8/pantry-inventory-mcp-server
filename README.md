# pantry-inventory-mcp-server

A small, offline-first **Model Context Protocol (MCP) server** that turns any
MCP-capable AI assistant into a friendly helper for your pantry and freezer.
Tell it what you bought, what you cooked, and it answers questions like
"what expires soon?" and "what can I cook tonight?" — then hands you a
shopping-gap list for the store.

**Beginner-friendly by design:** one Python file, Python standard library only
(no pip installs, no accounts, no network calls, no secrets). Your data is a
single JSON file on your own disk, with automatic backups.

## What it does

| Tool | What it does for you |
| --- | --- |
| `add_item` | Add food to the pantry, freezer, or fridge — with quantity, unit, and an expiry date. Adding the same food again merges the amounts. |
| `consume_item` | Record using some of an item while cooking; the item disappears when it hits zero (and it refuses to consume more than you have). |
| `remove_item` | Delete an item outright (typos, spills, regrets). |
| `list_items` | See everything in stock, number of items per location. |
| `expiring_soon` | What expires within N days (default 7). Already-expired items are always included and marked `EXPIRED`. |
| `add_recipe` / `list_recipes` / `remove_recipe` | Save simple recipe notes paired to your pantry: a name, an ingredient list (optional quantities), and optional instructions. |
| `what_can_i_cook` | Matches your recipes against **unexpired** stock: *ready to cook*, *almost there*, or *missing several*. Plurals match (`onions` covers `onion`, `eggs` covers `egg`). |
| `shopping_gaps` | Aggregates every ingredient you are missing across all recipes into one shopping list, plus a "replace or discard" section for expired stock. |

### How it fits with the other servers

This server deliberately **does one job**: physical food inventory.
It complements, without overlap:

- **notes-mcp-server** — freeform writing and note recall.
- **flashcards-mcp-server** — spaced-repetition study decks.
- **bookshelf-mcp-server** — books read, owned, or queued.
- **checklists-mcp-server** — to-dos and routines.

You can run them side by side in the same MCP client; each keeps its own data
and its own tools.

## Quick start

Requirements: Python 3.10+ (tested on 3.13). No dependencies to install.

1. Get a sample pantry so there is something to look at (recommended first run):

   ```bash
   python3 pantry_mcp.py --load-sample
   ```

   This writes a friendly sample inventory (rice, beans, milk, eggs,
   chicken thighs for the freezer, three recipes) with expiry dates set
   **relative to today**, then exits.

2. Point your MCP client at the server. Most clients use a JSON config like:

   ```json
   {
     "mcpServers": {
       "pantry": {
         "command": "python3",
         "args": ["/full/path/to/pantry-inventory-mcp-server/pantry_mcp.py"]
       }
     }
   }
   ```

   First connection to a fresh data directory automatically seeds the sample
   too (use `--no-sample` to start truly empty).

3. Talk to your assistant: *"add 2 onions to the pantry, they expire Friday"*,
   *"what expires soon?"*, *"what can I cook?"*, *"what should I buy?"*.

### CLI options

```
--data-dir DIR    where pantry.json and backups/ live (default: ./data next to the script)
--backup-keep N   how many automatic backups to keep (default 10)
--load-sample     write the sample pantry, then exit (add --force to replace existing data; old data is auto-backed up)
--no-sample       never auto-seed the sample on first run
```

### Manual smoke test (no MCP client needed)

The server speaks newline-delimited JSON-RPC 2.0 on stdin/stdout. Try:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"me"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"add_item","arguments":{"name":"rice","quantity":2,"unit":"cup"}}}' \
  | python3 pantry_mcp.py --no-sample --data-dir /tmp/pantry-demo
```

Each response comes back as one line of JSON. Logs (none sensitive) go to
stderr only.

## Where your data lives, and how it stays safe

- **One JSON file**: `data/pantry.json` by default (change with `--data-dir`).
  Format marker: `pantry-inventory/1`.
- **Automatic backups**: every change snapshots the previous file into
  `backups/pantry-backup-<timestamp>.json`; the last 10 are kept
  (`--backup-keep` to change). No backup is made when nothing changed.
- **Crash resilience**: writes are atomic (write to a temp file, then rename),
  so a crash mid-write cannot leave a half-file. If the data file is ever
  corrupt, the server archives the original as `pantry-corrupt-<timestamp>.json`,
  warns on stderr, and starts empty — your backups are untouched.
- **Path safety** (this server never trusts remote input with a filename):
  - Only the local CLI/user picks the storage directory; tools never do.
  - `--data-dir` arguments containing `..` are rejected.
  - System directories (`/etc`, `/usr`, `/var`, `/root`, ...) and `/` are refused.
  - Symlinks that escape into forbidden system locations are refused; the
    real, resolved path is always what gets used.
  - `pantry.json` and `backups/` stay inside that one directory — tool input
    (even an item named `../../etc/passwd`) can never move the file elsewhere.

## Tests and smoke test

```bash
python3 -m unittest -v test_pantry.py   # 58 offline unit tests (round-trips, expiry logic, backups, path safety, protocol errors)
python3 smoke_test.py                   # 12-step end-to-end run: real subprocess, every tool, seed data, error cases
```

Both are fully offline and need no network, keys, or personal data.
Run all of this yourself before trusting it with your groceries — it is a
demo-grade inventory, not insurance.

## The shipped sample

`samples/sample_pantry.json` is a readable snapshot of the sample pantry
(shown as an example of the storage format; its dates are frozen at
generation time). `--load-sample` regenerates the same pantry with dates
relative to *today*, which is what you usually want.

## Privacy

Everything stays on your machine: the data file is local JSON, there are no
network calls in the code, and nothing in it asks for or needs keys, tokens,
or personal data. Delete the folder, delete the data.

## Credits and license

Built as a practical local-first tool for beginner-friendly AI education by
**Asdale Dynamics, LLC** — *The Massive AI Manual* and
[dylanasdale.github.io](https://dylanasdale.github.io) explain AI "like a
friend would": plain English, step by step, never condescending.

Licensed under the MIT License — see `LICENSE`. Copyright (c) 2026 Asdale
Dynamics, LLC.

#!/usr/bin/env python3
"""pantry-inventory-mcp-server: a tiny MCP server for pantry and freezer inventory.

Runs over stdio (newline-delimited JSON-RPC 2.0, as used by Model Context Protocol).
Storage is a single JSON file with automatic backups. Python standard library only.

Copyright (c) 2026 Asdale Dynamics, LLC. MIT licensed.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta

SERVER_NAME = "pantry-inventory-mcp-server"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
DATA_FORMAT = "pantry-inventory/1"
DATA_FILENAME = "pantry.json"
BACKUPS_DIRNAME = "backups"
DEFAULT_BACKUP_KEEP = 10
MAX_HISTORY = 200
FORBIDDEN_PREFIXES = (
    "/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64",
    "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/usr", "/var",
)


class StoreError(Exception):
    pass


class UsageError(StoreError):
    pass


class ProtocolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def norm_name(value):
    return " ".join(str(value).split()).lower()


def singularize(word):
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith(("oes", "ses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def canonical_ingredient(value):
    return singularize(norm_name(value))


def same_ingredient(a, b):
    return canonical_ingredient(a) == canonical_ingredient(b)


def fmt_qty(value):
    value = float(value)
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def with_unit(quantity, unit):
    return fmt_qty(quantity) + (f" {unit}" if unit else "")


def _as_qty(value, what="quantity"):
    try:
        qty = float(value)
    except (TypeError, ValueError):
        raise UsageError(f"{what} must be a number, got {value!r}")
    if not math.isfinite(qty) or qty <= 0:
        raise UsageError(f"{what} must be a positive finite number, got {value!r}")
    return qty


def parse_iso_date(value, what="date"):
    text = str(value).strip()
    if not text:
        raise UsageError(f"{what} must not be empty (use an ISO date like 2026-10-31)")
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        raise UsageError(f"{what} must be an ISO date like 2026-10-31, got {value!r}")


def _opt_date(value, what="date"):
    if value in (None, ""):
        return None
    return parse_iso_date(value, what)


def safe_data_dir(raw):
    if raw is None or not str(raw).strip():
        raise UsageError("data directory must not be empty")
    candidate = os.path.expanduser(str(raw).strip())
    if ".." in candidate.replace("\\", "/").split("/"):
        raise UsageError("data directory must not contain '..' path segments")
    if not os.path.isabs(candidate):
        candidate = os.path.join(os.getcwd(), candidate)
    resolved = os.path.realpath(candidate)
    if resolved == "/":
        raise UsageError("refusing to use '/' as the data directory")
    for prefix in FORBIDDEN_PREFIXES:
        if resolved == prefix or resolved.startswith(prefix + "/"):
            raise UsageError(
                f"refusing to use this system directory as the data directory: {resolved}"
            )
    return resolved


def _item_key(name):
    return norm_name(name)


class PantryStore:
    """Local inventory storage: items, recipe notes, expiry checks, shopping gaps."""

    def __init__(self, data_dir, backup_keep=DEFAULT_BACKUP_KEEP,
                 seed_on_first_run=False):
        if backup_keep is None or int(backup_keep) < 1:
            backup_keep = DEFAULT_BACKUP_KEEP
        self.data_dir = safe_data_dir(data_dir)
        self.path = os.path.join(self.data_dir, DATA_FILENAME)
        self.backups_dir = os.path.join(self.data_dir, BACKUPS_DIRNAME)
        self.backup_keep = int(backup_keep)
        self.items = {}
        self.recipes = {}
        self.history = []
        self.had_data_file = os.path.exists(self.path)
        self._load()
        if not self.had_data_file and seed_on_first_run:
            self.seed_sample(sample_data())

    def is_empty(self):
        return not self.items and not self.recipes

    def _load(self):
        had_file = self.had_data_file
        self.items = {}
        self.recipes = {}
        self.history = []
        if not had_file:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise StoreError(f"cannot read {self.path}: {exc}")
        try:
            data = json.loads(text)
        except ValueError:
            self._recover_corrupt_file(text)
            return
        if not isinstance(data, dict):
            self._recover_corrupt_file(text)
            return
        self._ingest(data)

    def _ingest(self, data):
        rows = data.get("items")
        for row in rows if isinstance(rows, list) else []:
            try:
                item = self._parse_item(row)
            except (UsageError, TypeError, AttributeError):
                continue
            if item:
                self.items[_item_key(item["name"])] = item
        rows = data.get("recipes")
        for row in rows if isinstance(rows, list) else []:
            try:
                recipe = self._parse_recipe(row)
            except (UsageError, TypeError, AttributeError):
                continue
            if recipe:
                self.recipes[norm_name(recipe["name"])] = recipe
        rows = data.get("history")
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                self.history.append({
                    "date": _opt_date(row.get("date")).isoformat(),
                    "action": str(row.get("action", "")),
                    "item": str(row.get("item", "")),
                    "detail": str(row.get("detail", "")),
                })
            except (UsageError, TypeError):
                continue
        if len(self.history) > MAX_HISTORY:
            del self.history[:-MAX_HISTORY]

    def _recover_corrupt_file(self, text):
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            corrupt_path = os.path.join(self.data_dir, f"pantry-corrupt-{stamp}.json")
            with open(corrupt_path, "w", encoding="utf-8") as handle:
                handle.write(text)
            print(
                f"warning: {self.path} was not valid JSON; the original was saved "
                f"as {corrupt_path} and the inventory starts fresh",
                file=sys.stderr,
            )
        except OSError:
            print(
                f"warning: {self.path} was not valid JSON and could not be archived",
                file=sys.stderr,
            )
        self.items = {}
        self.recipes = {}
        self.history = []

    def _parse_item(self, row):
        if not isinstance(row, dict):
            raise TypeError("item row must be an object")
        name = str(row.get("name", "")).strip()
        if not name:
            return None
        return {
            "name": name,
            "quantity": _as_qty(row.get("quantity", 1.0)),
            "unit": norm_name(row.get("unit", "") or ""),
            "location": norm_name(row.get("location", "") or "pantry") or "pantry",
            "added_date": _opt_date(row.get("added_date")),
            "expiry_date": _opt_date(row.get("expiry_date")),
        }

    def _parse_recipe(self, row):
        if not isinstance(row, dict):
            raise TypeError("recipe row must be an object")
        name = str(row.get("name", "")).strip()
        if not name:
            return None
        raw_ingredients = row.get("ingredients")
        if not isinstance(raw_ingredients, list):
            raise UsageError(f"recipe {name!r} ingredients must be a list")
        ingredients = []
        for entry in raw_ingredients:
            if not isinstance(entry, dict):
                continue
            ing_name = str(entry.get("name", "")).strip()
            if not ing_name:
                continue
            qty = entry.get("quantity")
            ingredients.append({
                "name": ing_name,
                "quantity": _as_qty(qty) if qty not in (None, "") else None,
                "unit": norm_name(entry.get("unit", "") or ""),
            })
        if not ingredients:
            return None
        recipe = {"name": name, "ingredients": ingredients}
        instructions = row.get("instructions")
        if instructions not in (None, ""):
            recipe["instructions"] = str(instructions)
        return recipe

    def to_dict(self):
        return {
            "format": DATA_FORMAT,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "items": [self.items[key] for key in sorted(self.items)],
            "recipes": [self.recipes[key] for key in sorted(self.recipes)],
            "history": list(self.history[-MAX_HISTORY:]),
        }

    @staticmethod
    def _json_default(obj):
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        raise TypeError(f"not JSON serializable: {type(obj).__name__}")

    def save(self):
        text = json.dumps(self.to_dict(), indent=2, ensure_ascii=False,
                          default=self._json_default) + "\n"
        new_bytes = text.encode("utf-8")
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.backups_dir, exist_ok=True)
        backup_path = None
        if os.path.exists(self.path):
            with open(self.path, "rb") as handle:
                old_bytes = handle.read()
            if old_bytes == new_bytes:
                return None
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup_path = os.path.join(self.backups_dir, f"pantry-backup-{stamp}.json")
            shutil.copy2(self.path, backup_path)
        fd, tmp_path = tempfile.mkstemp(dir=self.data_dir, prefix=".pantry-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        self._prune_backups()
        return self.path if backup_path else self.path

    def _prune_backups(self):
        try:
            names = [
                name for name in os.listdir(self.backups_dir)
                if name.startswith("pantry-backup-") and name.endswith(".json")
            ]
        except OSError:
            return
        names.sort(reverse=True)
        for stale in names[self.backup_keep:]:
            try:
                os.unlink(os.path.join(self.backups_dir, stale))
            except OSError:
                pass

    def read_backup_files(self):
        try:
            return sorted(
                name for name in os.listdir(self.backups_dir)
                if name.startswith("pantry-backup-") and name.endswith(".json")
            )
        except OSError:
            return []

    def log_event(self, action, item, detail, event_date=None):
        self.history.append({
            "date": (event_date or date.today()).isoformat(),
            "action": action,
            "item": item,
            "detail": detail,
        })
        if len(self.history) > MAX_HISTORY:
            del self.history[:-MAX_HISTORY]

    def seed_sample(self, payload):
        self.items = {}
        self.recipes = {}
        self.history = []
        records = {"items": payload.get("items", []), "recipes": payload.get("recipes", [])}
        self._ingest(records)
        if not self.items or not self.recipes:
            raise StoreError("sample data could not be parsed")
        self.save()

    def add_item(self, name, quantity, unit="", location="pantry",
                 expiry_date=None, added_date=None, today=None):
        clean_name = str(name).strip()
        if not clean_name:
            raise UsageError("item name must not be empty")
        qty = _as_qty(quantity)
        unit = norm_name(unit or "")
        location = norm_name(location or "pantry") or "pantry"
        expiry = _opt_date(expiry_date, "expiry_date")
        added = _opt_date(added_date, "added_date") or (today or date.today())
        key = _item_key(clean_name)
        existing = self.items.get(key)
        if existing is None:
            self.items[key] = {
                "name": clean_name,
                "quantity": qty,
                "unit": unit,
                "location": location,
                "added_date": added,
                "expiry_date": expiry,
            }
            message = (f"Added {with_unit(qty, unit)} {clean_name} to the {location}.")
        else:
            existing["quantity"] += qty
            if unit and not existing["unit"]:
                existing["unit"] = unit
            if expiry is not None:
                current = existing["expiry_date"]
                existing["expiry_date"] = expiry if current is None else min(current, expiry)
            message = (f"Updated {clean_name}: inventory now holds "
                       f"{with_unit(existing['quantity'], existing['unit'])}.")
        self.log_event("add", clean_name, f"+{fmt_qty(qty)}", added)
        if expiry is not None:
            message += f" Expires {expiry.isoformat()}."
        self.save()
        return message

    def consume_item(self, name, amount, event_date=None, reason=None, today=None):
        key = _item_key(str(name)) if isinstance(name, str) else ""
        existing = self.items.get(key)
        if existing is None:
            raise UsageError(f"No item named {name!r} in inventory.")
        qty = _as_qty(amount, "amount")
        stock = existing["quantity"]
        if qty > stock:
            raise UsageError(
                f"Cannot use {fmt_qty(qty)} {existing['name']}: you only have "
                f"{with_unit(stock, existing['unit'])}."
            )
        when = _opt_date(event_date, "date") or (today or date.today())
        remaining = stock - qty
        detail = f"-{fmt_qty(qty)}" + (f" ({reason})" if reason else "")
        self.log_event("consume", existing["name"], detail, when)
        if remaining <= 1e-9:
            del self.items[key]
            message = (f"Used {fmt_qty(qty)} {existing['name']}; it hit zero and was "
                       "removed from inventory.")
        else:
            existing["quantity"] = remaining
            message = (f"Used {fmt_qty(qty)} {existing['name']}; "
                       f"{with_unit(remaining, existing['unit'])} left.")
        self.save()
        return message

    def remove_item(self, name, today=None):
        key = _item_key(str(name)) if isinstance(name, str) else ""
        existing = self.items.get(key)
        if existing is None:
            raise UsageError(f"No item named {name!r} in inventory.")
        del self.items[key]
        self.log_event("remove", existing["name"], "-all", today or date.today())
        self.save()
        return f"Removed {existing['name']} from inventory."

    def list_items(self, location=None, today=None):
        rows = list(self.items.values())
        if location:
            wanted = norm_name(location)
            rows = [row for row in rows if norm_name(row["location"]) == wanted]
        rows.sort(key=lambda row: norm_name(row["name"]))
        return rows

    def usable_items(self, today=None):
        today = today or date.today()
        return [
            row for row in self.items.values()
            if row["expiry_date"] is None or row["expiry_date"] >= today
        ]

    def expiring_soon(self, days=7, today=None):
        today = today or date.today()
        if days is None or days < 0:
            days = 7
        rows = []
        for row in self.items.values():
            expiry = row["expiry_date"]
            if expiry is None:
                continue
            delta = (expiry - today).days
            if delta < 0 or delta <= days:
                rows.append((row, delta))
        rows.sort(key=lambda pair: pair[1])
        return rows

    def add_recipe(self, name, ingredients, instructions=None):
        clean_name = str(name).strip()
        if not clean_name:
            raise UsageError("recipe name must not be empty")
        if not isinstance(ingredients, list) or not ingredients:
            raise UsageError("recipe needs at least one ingredient")
        recipe = {"name": clean_name, "ingredients": []}
        for entry in ingredients:
            if isinstance(entry, dict):
                ing_name = str(entry.get("name", "")).strip()
                qty = entry.get("quantity")
                unit = entry.get("unit", "")
            else:
                ing_name = str(entry).strip()
                qty = None
                unit = ""
            if not ing_name:
                continue
            recipe["ingredients"].append({
                "name": ing_name,
                "quantity": _as_qty(qty) if qty not in (None, "") else None,
                "unit": norm_name(unit or ""),
            })
        if not recipe["ingredients"]:
            raise UsageError("recipe needs at least one named ingredient")
        if instructions not in (None, ""):
            recipe["instructions"] = str(instructions)
        self.recipes[norm_name(clean_name)] = recipe
        self.save()
        return (f"Saved recipe {clean_name!r} with "
                f"{len(recipe['ingredients'])} ingredient(s).")

    def remove_recipe(self, name):
        key = norm_name(str(name)) if isinstance(name, str) else ""
        recipe = self.recipes.pop(key, None)
        if recipe is None:
            raise UsageError(f"No recipe named {name!r}.")
        self.save()
        return f"Removed recipe {recipe['name']!r}."

    def list_recipes(self):
        keys = sorted(self.recipes)
        return [self.recipes[key] for key in keys]

    def can_cook(self, today=None):
        today = today or date.today()
        usable = self.usable_items(today=today)
        results = []
        for recipe in self.list_recipes():
            missing = []
            present_any = False
            for ingredient in recipe["ingredients"]:
                have_qty = sum(
                    row["quantity"]
                    for row in usable if same_ingredient(row["name"], ingredient["name"])
                )
                if have_qty <= 0:
                    missing.append((ingredient["name"], None, None))
                else:
                    present_any = True
                    if (ingredient["quantity"] is not None
                            and have_qty < ingredient["quantity"]):
                        missing.append((ingredient["name"], have_qty,
                                        ingredient["quantity"]))
            results.append({
                "name": recipe["name"],
                "missing": missing,
                "total": len(recipe["ingredients"]),
                "status": ("ready" if not missing else
                           "not started" if not present_any else "partial"),
            })
        return results

    def shopping_gaps(self, today=None):
        today = today or date.today()
        counts = {}
        for report in self.can_cook(today=today):
            for name, _have, _need in report["missing"]:
                canon = canonical_ingredient(name)
                entry = counts.get(canon)
                if entry is None:
                    entry = counts[canon] = {"name": name, "recipes": []}
                if report["name"] not in entry["recipes"]:
                    entry["recipes"].append(report["name"])
        gaps = [
            (entry["name"], len(entry["recipes"]), list(entry["recipes"]))
            for _canon, entry in sorted(counts.items(), key=lambda kv: kv[0])
        ]
        gaps.sort(key=lambda item: (-item[1], norm_name(item[0])))
        expired = []
        for row in self.items.values():
            expiry = row["expiry_date"]
            if expiry is not None and expiry < today:
                expired.append((row, (expiry - today).days))
        expired.sort(key=lambda pair: pair[1])
        return gaps, expired


def format_list_report(store, location=None, today=None):
    today = today or date.today()
    rows = store.list_items(location=location, today=today)
    header = f"Inventory — {len(rows)} item(s)"
    if location:
        header += f" in {norm_name(location)}"
    if not rows:
        return header + (". Add some with add_item." if not location
                         else f". Nothing stored in {norm_name(location)}.")
    lines = [header]
    for row in rows:
        expiry = row["expiry_date"]
        if expiry is None:
            expiry_text = "no expiry date"
        else:
            delta = (expiry - today).days
            expiry_text = (f"EXPIRED ({expiry.isoformat()})" if delta < 0
                           else f"expires {expiry.isoformat()}")
        lines.append(f"- {row['name']}: {with_unit(row['quantity'], row['unit'])} "
                     f"({row['location']}, {expiry_text})")
    return "\n".join(lines)


def format_expiring_report(store, days=7, today=None):
    today = today or date.today()
    rows = store.expiring_soon(days=days, today=today)
    if not rows:
        return (f"Nothing expires within the next {days} day(s) "
                f"as of {today.isoformat()}. Good stock control.")
    lines = [f"Expiring within {days} day(s), as of {today.isoformat()}:"]
    for row, delta in rows:
        when = (f"EXPIRED {-delta} day(s) ago ({row['expiry_date'].isoformat()})"
                if delta < 0 else f"in {delta} day(s) ({row['expiry_date'].isoformat()})")
        lines.append(f"- {row['name']}: {with_unit(row['quantity'], row['unit'])} "
                     f"({row['location']}, {when})")
    lines.append("Tip: use expiring items first, or note them on a shopping gap list "
                 "to restock.")
    return "\n".join(lines)


def format_recipes_report(store, today=None):
    recipes = store.list_recipes()
    if not recipes:
        return "No recipe notes saved yet. Use add_recipe to record one."
    cooking = {report["name"]: report for report in store.can_cook()}
    lines = [f"Saved recipe notes — {len(recipes)}:"]
    for recipe in recipes:
        report = cooking.get(recipe["name"])
        total = len(recipe["ingredients"])
        if report is None:
            lines.append(f"- {recipe['name']} — {total} ingredient(s)")
        elif report["missing"]:
            names = ", ".join(name for name, _h, _n in report["missing"])
            lines.append(f"- {recipe['name']} — {total} ingredient(s); missing: {names}")
        else:
            lines.append(f"- {recipe['name']} — {total} ingredient(s); all on hand")
    return "\n".join(lines)


def format_cook_report(store, today=None):
    today = today or date.today()
    reports = store.can_cook(today=today)
    if not reports:
        return "No recipe notes saved yet. Use add_recipe to record one, then ask again."
    lines = [f"What can I cook, as of {today.isoformat()}?"]
    buckets = {"ready": [], "partial": [], "not started": []}
    for report in reports:
        buckets[report["status"]].append(report)
    if buckets["ready"]:
        lines.append("Ready to cook now:")
        for report in buckets["ready"]:
            lines.append(f"- {report['name']} "
                         f"(all {report['total']} ingredients available)")
    if buckets["partial"]:
        lines.append("Almost there:")
        for report in buckets["partial"]:
            names = ", ".join(name for name, _h, _n in report["missing"])
            lines.append(f"- {report['name']} — missing: {names}")
    if buckets["not started"]:
        lines.append("Missing several ingredients:")
        for report in buckets["not started"]:
            names = ", ".join(name for name, _h, _n in report["missing"])
            lines.append(f"- {report['name']} — missing: {names}")
    expired = [row["name"] for row in store.items.values()
               if row["expiry_date"] is not None and row["expiry_date"] < today]
    if expired:
        lines.append(f"Not counted as available (expired): {', '.join(sorted(expired))}")
    return "\n".join(lines)


def format_gaps_report(store, today=None):
    today = today or date.today()
    gaps, expired = store.shopping_gaps(today=today)
    if not gaps and not expired:
        return ("No shopping gaps: every saved recipe note is fully covered by current "
                "unexpired stock.")
    lines = []
    if gaps:
        lines.append(f"Shopping gaps across {len(store.recipes)} saved recipe note(s):")
        for name, count, recipes in gaps:
            shown = ", ".join(recipes[:3])
            more = f" +{len(recipes) - 3} more" if len(recipes) > 3 else ""
            lines.append(f"- {name} — needed for {count} recipe(s) ({shown}{more})")
    if expired:
        lines.append("Expired — replace or discard:")
        for row, delta in expired:
            lines.append(f"- {row['name']} (EXPIRED {-delta} day(s) ago)")
    return "\n".join(lines)


TOOL_SCHEMAS = {
    "add_item": {
        "name": "add_item",
        "title": "Add pantry item",
        "description": (
            "Add to the pantry/freezer/fridge inventory, or add to the quantity of an "
            "item already stored. Dates use ISO format (YYYY-MM-DD)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1,
                         "description": "Food name, e.g. 'eggs'"},
                "quantity": {"type": "number", "exclusiveMinimum": 0},
                "unit": {"type": "string", "description": "Optional, e.g. 'cup', 'lb'"},
                "location": {"type": "string", "default": "pantry",
                             "description": "pantry, freezer, fridge, shelf, cabinet, "
                                            "or other"},
                "expiry_date": {"type": "string",
                                "description": "Optional ISO date (YYYY-MM-DD)"},
                "added_date": {"type": "string", "description": "Optional ISO date"},
            },
            "required": ["name", "quantity"],
        },
    },
    "consume_item": {
        "name": "consume_item",
        "title": "Consume pantry item",
        "description": (
            "Record using some of an item (cooking, snacks). Reduces the stored "
            "quantity; the item is removed when it reaches zero."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "amount": {"type": "number", "exclusiveMinimum": 0,
                           "description": "Amount used"},
                "date": {"type": "string", "description": "Optional ISO date used"},
                "reason": {"type": "string", "description": "Optional note, e.g. 'tacos'"},
            },
            "required": ["name", "amount"],
        },
    },
    "remove_item": {
        "name": "remove_item",
        "title": "Delete pantry item",
        "description": "Delete an item from inventory entirely "
                       "(prefer consume_item for cooking).",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 1}},
            "required": ["name"],
        },
    },
    "list_items": {
        "name": "list_items",
        "title": "List inventory",
        "description": "List inventory items, optionally filtered by location "
                       "(pantry/freezer/fridge/shelf/cabinet/other).",
        "inputSchema": {
            "type": "object",
            "properties": {"location": {"type": "string"}},
        },
    },
    "expiring_soon": {
        "name": "expiring_soon",
        "title": "What expires soon",
        "description": ("Items expiring within N days (default 7). Already-expired "
                        "items are always included and marked EXPIRED."),
        "inputSchema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "minimum": 0, "default": 7}},
        },
    },
    "add_recipe": {
        "name": "add_recipe",
        "title": "Save recipe note",
        "description": ("Save a recipe note paired to the pantry: name, ingredient list "
                        "(each with optional quantity/unit), optional instructions."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "ingredients": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "quantity": {"type": "number"},
                            "unit": {"type": "string"},
                        },
                        "required": ["name"],
                    },
                },
                "instructions": {"type": "string"},
            },
            "required": ["name", "ingredients"],
        },
    },
    "remove_recipe": {
        "name": "remove_recipe",
        "title": "Delete recipe note",
        "description": "Delete a saved recipe note.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 1}},
            "required": ["name"],
        },
    },
    "list_recipes": {
        "name": "list_recipes",
        "title": "List recipe notes",
        "description": "List saved recipe notes, including which ingredients are "
                       "currently missing.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "what_can_i_cook": {
        "name": "what_can_i_cook",
        "title": "What can I cook",
        "description": ("Match saved recipe notes against unexpired pantry stock: what "
                        "is ready now, what is almost there, what is missing."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    "shopping_gaps": {
        "name": "shopping_gaps",
        "title": "Shopping gap list",
        "description": ("Aggregate ingredients missing from saved recipes (expired stock "
                        "does not count) into a shopping list, plus expired items to "
                        "replace."),
        "inputSchema": {"type": "object", "properties": {}},
    },
}


def _arg_str(args, key, default=None, required=False):
    if key not in args or args[key] is None:
        if required:
            raise UsageError(f"argument {key!r} is required")
        return default
    value = args[key]
    if not isinstance(value, str):
        raise UsageError(f"argument {key!r} must be text")
    value = value.strip()
    if required and not value:
        raise UsageError(f"argument {key!r} must not be empty")
    return value


def _arg_num(args, key, default=None, required=False):
    if key not in args or args[key] is None:
        if required:
            raise UsageError(f"argument {key!r} is required")
        return default
    value = args[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UsageError(f"argument {key!r} must be a number")
    return value


def tool_add_item(store, args):
    return store.add_item(
        name=_arg_str(args, "name", required=True),
        quantity=_arg_num(args, "quantity", required=True),
        unit=_arg_str(args, "unit", default="") or "",
        location=_arg_str(args, "location", default="pantry") or "pantry",
        expiry_date=_arg_str(args, "expiry_date", default=None),
        added_date=_arg_str(args, "added_date", default=None),
    )


def tool_consume_item(store, args):
    return store.consume_item(
        name=_arg_str(args, "name", required=True),
        amount=_arg_num(args, "amount", required=True),
        event_date=_arg_str(args, "date", default=None),
        reason=_arg_str(args, "reason", default=None),
    )


def tool_remove_item(store, args):
    return store.remove_item(name=_arg_str(args, "name", required=True))


def tool_list_items(store, args):
    return format_list_report(store, location=_arg_str(args, "location", default=None))


def tool_expiring_soon(store, args):
    days = _arg_num(args, "days", default=None)
    if days is None:
        days = 7
    if days < 0:
        raise UsageError("days must be zero or a positive number")
    return format_expiring_report(store, days=int(days))


def tool_add_recipe(store, args):
    ingredients = args.get("ingredients")
    if not isinstance(ingredients, list) or not ingredients:
        raise UsageError("add_recipe needs a non-empty 'ingredients' list")
    return store.add_recipe(
        name=_arg_str(args, "name", required=True),
        ingredients=ingredients,
        instructions=_arg_str(args, "instructions", default=None),
    )


def tool_remove_recipe(store, args):
    return store.remove_recipe(name=_arg_str(args, "name", required=True))


def tool_list_recipes(store, args):
    return format_recipes_report(store)


def tool_what_can_i_cook(store, args):
    return format_cook_report(store)


def tool_shopping_gaps(store, args):
    return format_gaps_report(store)


TOOL_HANDLERS = {
    "add_item": tool_add_item,
    "consume_item": tool_consume_item,
    "remove_item": tool_remove_item,
    "list_items": tool_list_items,
    "expiring_soon": tool_expiring_soon,
    "add_recipe": tool_add_recipe,
    "remove_recipe": tool_remove_recipe,
    "list_recipes": tool_list_recipes,
    "what_can_i_cook": tool_what_can_i_cook,
    "shopping_gaps": tool_shopping_gaps,
}


def sample_data(today=None):
    today = today or date.today()

    def day(offset):
        return (today + timedelta(days=offset)).isoformat()

    added = today.isoformat()
    return {
        "format": DATA_FORMAT,
        "items": [
            {"name": "rice", "quantity": 3, "unit": "cup", "location": "pantry",
             "added_date": added},
            {"name": "pasta", "quantity": 1, "unit": "box", "location": "pantry",
             "added_date": added},
            {"name": "canned black beans", "quantity": 2, "unit": "can",
             "location": "pantry", "added_date": added},
            {"name": "canned tomatoes", "quantity": 3, "unit": "can", "location": "pantry",
             "added_date": added},
            {"name": "oatmeal", "quantity": 1, "unit": "box", "location": "pantry",
             "added_date": added},
            {"name": "peanut butter", "quantity": 1, "unit": "jar", "location": "pantry",
             "added_date": added},
            {"name": "flour", "quantity": 4, "unit": "cup", "location": "pantry",
             "added_date": added},
            {"name": "garlic", "quantity": 5, "unit": "clove", "location": "pantry",
             "added_date": added},
            {"name": "onions", "quantity": 2, "unit": "head", "location": "pantry",
             "added_date": added, "expiry_date": day(2)},
            {"name": "milk", "quantity": 1, "unit": "quart", "location": "fridge",
             "added_date": added, "expiry_date": day(3)},
            {"name": "eggs", "quantity": 6, "unit": "each", "location": "fridge",
             "added_date": added, "expiry_date": day(10)},
            {"name": "cheddar cheese", "quantity": 8, "unit": "oz", "location": "fridge",
             "added_date": added, "expiry_date": day(14)},
            {"name": "chicken thighs", "quantity": 4, "unit": "each", "location": "freezer",
             "added_date": added, "expiry_date": day(45)},
            {"name": "frozen peas", "quantity": 1, "unit": "bag", "location": "freezer",
             "added_date": added, "expiry_date": day(120)},
            {"name": "butter", "quantity": 1, "unit": "stick", "location": "fridge",
             "added_date": added},
        ],
        "recipes": [
            {
                "name": "Weeknight rice and bean bowl",
                "ingredients": [
                    {"name": "rice", "quantity": 1, "unit": "cup"},
                    {"name": "canned black beans", "quantity": 1, "unit": "can"},
                    {"name": "onions", "quantity": 1},
                    {"name": "garlic", "quantity": 2, "unit": "clove"},
                    {"name": "canned tomatoes", "quantity": 1, "unit": "can"},
                    {"name": "lime", "quantity": 1},
                ],
                "instructions": ("Cook rice. Warm beans with chopped onion, garlic, and "
                                 "tomatoes. Serve over rice and finish with lime."),
            },
            {
                "name": "Emergency pasta and peas",
                "ingredients": [
                    {"name": "pasta", "quantity": 1},
                    {"name": "frozen peas", "quantity": 1},
                    {"name": "butter", "quantity": 1},
                    {"name": "garlic", "quantity": 2, "unit": "clove"},
                    {"name": "parmesan", "quantity": 2, "unit": "oz"},
                ],
                "instructions": ("Boil pasta with the peas for the last minute. Toss with "
                                 "melted butter and minced garlic; finish with parmesan."),
            },
            {
                "name": "Sheet-pan chicken thighs",
                "ingredients": [
                    {"name": "chicken thighs", "quantity": 4, "unit": "each"},
                    {"name": "potatoes", "quantity": 3, "unit": "each"},
                    {"name": "garlic", "quantity": 3, "unit": "clove"},
                    {"name": "olive oil", "quantity": 2, "unit": "tbsp"},
                ],
                "instructions": ("Toss thighs and halved potatoes with oil and garlic; "
                                 "roast at 425F about 35 minutes."),
            },
        ],
        "history": [],
    }


class PantryServer:
    def __init__(self, store):
        self.store = store

    def handle_line(self, line):
        raw = line.strip()
        if not raw:
            return None
        try:
            message = json.loads(raw)
        except ValueError:
            return json.dumps(
                self._error(None, -32700, "Parse error: request was not valid JSON"),
                ensure_ascii=False)
        if not isinstance(message, dict):
            return json.dumps(
                self._error(None, -32600, "Invalid Request: expected a JSON object"),
                ensure_ascii=False)
        response = self.handle_request(message)
        if response is None:
            return None
        return json.dumps(response, ensure_ascii=False)

    def handle_request(self, message):
        method = message.get("method")
        request_id = message.get("id")
        is_notification = "id" not in message
        if not isinstance(method, str):
            return (None if is_notification
                    else self._error(request_id, -32600, "Invalid Request: missing method"))
        if method.startswith("notifications/"):
            return None
        try:
            result = self._dispatch(method, message.get("params") or {})
        except ProtocolError as exc:
            return (None if is_notification
                    else self._error(request_id, exc.code, exc.message))
        except Exception as exc:
            if is_notification:
                return None
            return self._error(request_id, -32603, f"Internal error: {exc}")
        if is_notification:
            return None
        return self._result(request_id, result)

    def _dispatch(self, method, params):
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [TOOL_SCHEMAS[name] for name in sorted(TOOL_HANDLERS)]}
        if method == "tools/call":
            return self._tools_call(params)
        raise ProtocolError(-32601, f"Method not found: {method}")

    def _initialize(self, params):
        requested = params.get("protocolVersion")
        version = (requested if requested in SUPPORTED_PROTOCOL_VERSIONS
                   else PROTOCOL_VERSION)
        client_info = params.get("clientInfo")
        who = client_info.get("name") if isinstance(client_info, dict) else None
        print(f"Connected MCP client: {who or 'unknown'}; protocol {version}",
              file=sys.stderr)
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _tools_call(self, params):
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ProtocolError(-32602, "Invalid params: 'name' must be a tool name")
        name = name.strip()
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            raise ProtocolError(-32602, f"Unknown tool: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ProtocolError(-32602, "Invalid params: 'arguments' must be an object")
        try:
            text = handler(self.store, arguments)
            is_error = False
        except UsageError as exc:
            text, is_error = str(exc), True
        except Exception as exc:
            text, is_error = f"Internal error in tool {name}: {exc}", True
        return {"content": [{"type": "text", "text": text}], "isError": is_error}

    @staticmethod
    def _result(request_id, result):
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id, code, message):
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


def build_parser():
    parser = argparse.ArgumentParser(
        prog=SERVER_NAME,
        description=("MCP stdio server for pantry and freezer inventory. Storage is "
                     "one JSON file with automatic backups. Talk to it over stdio with "
                     "an MCP client; it is not interactive by itself."),
        epilog=("First run: python3 pantry_mcp.py --load-sample, then start it from an "
                "MCP client. Data is a local JSON file; no accounts, no network, no "
                "secrets."),
    )
    parser.add_argument("--data-dir", default=None, metavar="DIR",
                        help="directory for pantry.json and backups/ "
                             "(default: ./data next to this script)")
    parser.add_argument("--backup-keep", type=int, default=DEFAULT_BACKUP_KEEP,
                        metavar="N", help="number of backups to keep (default 10)")
    parser.add_argument("--load-sample", action="store_true",
                        help="write the friendly sample pantry into the data file, "
                             "then exit (use --force to replace existing data)")
    parser.add_argument("--no-sample", action="store_true",
                        help="do not auto-seed the sample pantry on very first run")
    parser.add_argument("--force", action="store_true",
                        help="with --load-sample: replace existing data (previous "
                             "version is auto-backed up)")
    parser.add_argument("--version", action="version",
                        version=f"{SERVER_NAME} {SERVER_VERSION}")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    script_dir = os.path.dirname(os.path.realpath(os.path.abspath(__file__)))
    data_dir = args.data_dir or os.path.join(script_dir, "data")
    store = PantryStore(data_dir, backup_keep=args.backup_keep,
                        seed_on_first_run=not args.no_sample)
    if args.load_sample:
        if not store.is_empty() and store.had_data_file and not args.force:
            print(f"data exists at {store.path}; use --load-sample --force to replace "
                  "it (the old data is backed up automatically)", file=sys.stderr)
            return 2
        seeded_from_first_run = (not store.had_data_file
                                 and not args.no_sample and store.is_empty())
        if not seeded_from_first_run:
            store.seed_sample(sample_data())
            print(f"Sample pantry written to {store.path}", file=sys.stderr)
        else:
            print(f"Sample pantry freshly seeded at {store.path}", file=sys.stderr)
        return 0
    server = PantryServer(store)
    print(f"{SERVER_NAME} {SERVER_VERSION} serving inventory at {store.path}",
          file=sys.stderr)
    for line in sys.stdin:
        response = server.handle_line(line)
        if response is not None:
            sys.stdout.write(response + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)

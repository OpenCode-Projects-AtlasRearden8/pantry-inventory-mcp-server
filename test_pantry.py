"""Offline unit tests for pantry-inventory-mcp-server (no network, stdlib only).

Run with: python3 -m unittest -v test_pantry.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta

import pantry_mcp as pm

HERE = os.path.dirname(os.path.abspath(__file__))


class PantryTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pantry-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def store(self, seed=False, backup_keep=pm.DEFAULT_BACKUP_KEEP, name="pantry"):
        data_dir = os.path.join(self.tmp, name)
        return pm.PantryStore(data_dir, backup_keep=backup_keep, seed_on_first_run=seed)

    def yesterday(self):
        return date.today() - timedelta(days=1)

    def in_days(self, n):
        return (date.today() + timedelta(days=n)).isoformat()


class StoreRoundTripTests(PantryTestBase):
    def test_add_then_reload_round_trip(self):
        st = self.store()
        st.add_item("Eggs", 12, unit="each", location="fridge",
                    expiry_date=self.in_days(10))
        st.add_item("frozen peas", 1, unit="bag", location="freezer")
        st.add_recipe("Pea soup", [{"name": "frozen peas", "quantity": 1},
                                   {"name": "mint", "quantity": 1}])
        again = self.store()
        self.assertEqual(len(again.items), 2)
        eggs = again.items["eggs"]
        self.assertEqual(eggs["quantity"], 12)
        self.assertEqual(eggs["unit"], "each")
        self.assertEqual(eggs["location"], "fridge")
        self.assertEqual(str(eggs["expiry_date"]), self.in_days(10))
        self.assertIsNone(again.items["frozen peas"]["expiry_date"])
        self.assertEqual(len(again.recipes), 1)
        self.assertEqual(again.recipes["pea soup"]["name"], "Pea soup")

    def test_add_same_name_merges_and_earliest_expiry_wins(self):
        st = self.store()
        st.add_item("milk", 1, unit="quart", expiry_date=self.in_days(5))
        st.add_item("Milk", 1, unit="quart", expiry_date=self.in_days(2))
        milk = st.items["milk"]
        self.assertEqual(milk["quantity"], 2)
        self.assertEqual(str(milk["expiry_date"]), self.in_days(2))
        st.add_item("MILK ", 1, expiry_date=None)
        self.assertEqual(st.items["milk"]["quantity"], 3)
        self.assertEqual(str(st.items["milk"]["expiry_date"]), self.in_days(2))

    def test_add_item_validation(self):
        st = self.store()
        for bad in (0, -2, "three", float("inf"), float("nan")):
            with self.assertRaises(pm.UsageError):
                st.add_item("rice", bad)
        with self.assertRaises(pm.UsageError):
            st.add_item("   ", 1)
        with self.assertRaises(pm.UsageError):
            st.add_item("rice", 1, expiry_date="not-a-date")
        st.add_item("rice", 1, expiry_date="")
        self.assertIsNone(st.items["rice"]["expiry_date"])

    def test_consume_subtracts_and_removes_at_zero(self):
        st = self.store()
        st.add_item("rice", 3, unit="cup")
        message = st.consume_item("Rice", 1.5)
        self.assertIn("1.5 cup left", message)
        self.assertEqual(st.items["rice"]["quantity"], 1.5)
        message = st.consume_item("Rice", 1.5, reason="dinner")
        self.assertIn("removed from inventory", message)
        self.assertNotIn("rice", st.items)

    def test_consume_blocks_overdraw(self):
        st = self.store()
        st.add_item("eggs", 2)
        with self.assertRaises(pm.UsageError) as ctx:
            st.consume_item("eggs", 3)
        self.assertIn("only have 2", str(ctx.exception))
        self.assertEqual(st.items["eggs"]["quantity"], 2)

    def test_consume_unknown_item(self):
        st = self.store(seed=True)
        with self.assertRaises(pm.UsageError):
            st.consume_item("dragon fruit", 1)

    def test_remove_item(self):
        st = self.store()
        st.add_item("hover beans", 1)
        st.remove_item("Hover Beans")
        self.assertNotIn("hover beans", st.items)
        with self.assertRaises(pm.UsageError):
            st.remove_item("hover beans")

    def test_list_items_filter_and_sort(self):
        st = self.store()
        st.add_item("zebra rice", 1)
        st.add_item("applesauce", 1, location="fridge")
        st.add_item("butter", 1, location="freezer")
        names = [row["name"] for row in st.list_items()]
        self.assertEqual(names, ["applesauce", "butter", "zebra rice"])
        freezer = [row["name"] for row in st.list_items(location="Freezer")]
        self.assertEqual(freezer, ["butter"])
        self.assertEqual(st.list_items(location="garage"), [])

    def test_history_logged(self):
        st = self.store()
        st.add_item("rice", 3)
        st.consume_item("Rice", 1, reason="lunch")
        actions = [(entry["action"], entry["item"]) for entry in st.history]
        self.assertIn(("add", "rice"), actions)
        self.assertIn(("consume", "rice"), actions)


class ExpiryAndCookingTests(PantryTestBase):
    def _seeded(self):
        st = self.store()
        st.add_item("onions", 2, unit="head", location="pantry",
                    expiry_date=self.in_days(2))
        st.add_item("garlic", 5, unit="clove", location="pantry",
                    expiry_date=self.in_days(30))
        st.add_item("milk", 1, unit="quart", location="fridge",
                    expiry_date=self.yesterday().isoformat())
        st.add_item("rice", 3, unit="cup", location="pantry")
        return st

    def test_expiring_soon_windows(self):
        st = self._seeded()
        rows = st.expiring_soon(days=7)
        names = [row[0]["name"] for row in rows]
        self.assertEqual(names, ["milk", "onions"])
        self.assertEqual(rows[0][1], -1)
        self.assertEqual(rows[1][1], 2)
        wide = st.expiring_soon(days=30)
        self.assertIn("garlic", [row[0]["name"] for row in wide])
        zero = st.expiring_soon(days=0)
        self.assertEqual([row[0]["name"] for row in zero], ["milk"])

    def test_expiring_soon_expired_sorted_first(self):
        st = self.store()
        st.add_item("fresh thing", 1, expiry_date=self.in_days(1))
        st.add_item("ancient thing", 1, expiry_date=self.in_days(-10))
        rows = st.expiring_soon(days=7)
        self.assertEqual([row[0]["name"] for row in rows],
                         ["ancient thing", "fresh thing"])

    def test_usable_items_excludes_expired(self):
        st = self._seeded()
        usable = {row["name"] for row in st.usable_items()}
        self.assertIn("rice", usable)
        self.assertIn("onions", usable)
        self.assertNotIn("milk", usable)

    def test_can_cook_ready_and_partial(self):
        st = self._seeded()
        st.add_recipe("Rice bowl", [{"name": "rice", "quantity": 1},
                                    {"name": "onion", "quantity": 1},
                                    {"name": "garlic", "quantity": 2, "unit": "clove"}])
        st.add_recipe("Lite curry", [{"name": "rice", "quantity": 1},
                                     {"name": "onion", "quantity": 1},
                                     {"name": "coconut milk", "quantity": 1}])
        by_name = {report["name"]: report for report in st.can_cook()}
        self.assertEqual(by_name["Rice bowl"]["status"], "ready")
        self.assertEqual(by_name["Lite curry"]["status"], "partial")
        missing = [name for name, _have, _need in by_name["Lite curry"]["missing"]]
        self.assertEqual(missing, ["coconut milk"])

    def test_can_cook_respects_quantities(self):
        st = self.store()
        st.add_item("pasta", 1, unit="box")
        st.add_recipe("Big pasta night", [{"name": "pasta", "quantity": 2}])
        report = st.can_cook()[0]
        self.assertEqual(report["status"], "partial")
        name, have, need = report["missing"][0]
        self.assertEqual((name, have, need), ("pasta", 1, 2))

    def test_can_cook_status_buckets(self):
        st = self.store()
        st.add_item("rice", 1)
        st.add_item("beans", 1)
        st.add_recipe("Nothing here", [{"name": "unicorn pepper", "quantity": 1}])
        st.add_recipe("Halfway house", [{"name": "rice", "quantity": 1},
                                        {"name": "tofu", "quantity": 1}])
        st.add_recipe("Full bowl", [{"name": "rice", "quantity": 1},
                                    {"name": "beans", "quantity": 1}])
        by_name = {report["name"]: report for report in st.can_cook()}
        self.assertEqual(by_name["Full bowl"]["status"], "ready")
        self.assertEqual(by_name["Halfway house"]["status"], "partial")
        self.assertEqual(by_name["Nothing here"]["status"], "not started")

    def test_recipe_validation(self):
        st = self.store()
        with self.assertRaises(pm.UsageError):
            st.add_recipe("", [{"name": "rice"}])
        with self.assertRaises(pm.UsageError):
            st.add_recipe("Empty", [])
        with self.assertRaises(pm.UsageError):
            st.add_recipe("Bad", "rice")
        with self.assertRaises(pm.UsageError):
            st.add_recipe("Bad qty", [{"name": "rice", "quantity": -3}])
        message = st.add_recipe("Onions plus", [{"name": "onion"}, {"name": ""},
                                               "   "])
        self.assertIn("1 ingredient", message)

    def test_recipe_ignores_expired_stock(self):
        st = self.store()
        st.add_item("onion", 2, expiry_date=self.yesterday().isoformat())
        st.add_recipe("Onion freebie", [{"name": "onion", "quantity": 1}])
        self.assertEqual(st.can_cook()[0]["status"], "not started")


class ShoppingGapTests(PantryTestBase):
    def test_gaps_aggregate_and_sort(self):
        st = self.store()
        st.add_item("garlic", 5)
        st.add_item("rice", 3)
        st.add_recipe("Stir fry", [{"name": "garlic", "quantity": 2},
                                   {"name": "soy sauce", "quantity": 1}])
        st.add_recipe("Pickled pepper stir fry", [{"name": "garlic"},
                                                  {"name": "bell pepper"},
                                                  {"name": "soy sauce"}])
        gaps, expired = st.shopping_gaps()
        names = [name for name, _count, _recipes in gaps]
        self.assertEqual(names, ["soy sauce", "bell pepper"])
        soy = gaps[0]
        self.assertEqual(soy[1], 2)
        self.assertEqual(set(soy[2]), {"Stir fry", "Pickled pepper stir fry"})
        self.assertFalse(expired)

    def test_gaps_include_expired_replacements(self):
        st = self.store()
        st.add_item("spinach", 1, expiry_date=self.yesterday().isoformat())
        st.add_recipe("Salad", [{"name": "spinach", "quantity": 1},
                                {"name": "feta", "quantity": 1}])
        gaps, expired = st.shopping_gaps()
        names = [name for name, _count, _recipes in gaps]
        self.assertIn("feta", names)
        self.assertIn("spinach", names)
        exp_names = [row[0]["name"] for row in expired]
        self.assertEqual(exp_names, ["spinach"])

    def test_gaps_none_when_all_covered(self):
        st = self.store()
        st.add_item("egg", 2)
        st.add_recipe("Fried egg", [{"name": "eggs", "quantity": 1}])
        gaps, expired = st.shopping_gaps()
        self.assertEqual(gaps, [])
        self.assertEqual(expired, [])


class StorageIntegrityTests(PantryTestBase):
    def test_save_creates_pretty_json_with_format(self):
        st = self.store()
        st.add_item("rice", 2)
        with open(st.path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["format"], pm.DATA_FORMAT)
        self.assertIn("updated_at", data)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["name"], "rice")

    def test_backup_created_on_change_not_on_noop(self):
        st = self.store()
        st.add_item("rice", 2)
        self.assertEqual(st.read_backup_files(), [])
        st.add_item("rice", 1)
        self.assertEqual(len(st.read_backup_files()), 1)
        with open(st.path, "r", encoding="utf-8") as handle:
            current = handle.read()
        st.save()
        self.assertEqual(len(st.read_backup_files()), 1)
        backup_path = os.path.join(st.backups_dir, st.read_backup_files()[0])
        with open(backup_path, "r", encoding="utf-8") as handle:
            backup = handle.read()
        self.assertNotEqual(backup, current)
        self.assertIn('"rice"', backup)

    def test_backup_pruned_to_keep_limit(self):
        st = self.store(backup_keep=3)
        for idx in range(8):
            st.add_item(f"thing{idx}", 1)
        self.assertEqual(len(st.read_backup_files()), 3)
        backup_path = os.path.join(st.backups_dir, st.read_backup_files()[-1])
        with open(backup_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertTrue(any(row["name"] == "thing6" for row in data["items"]))

    def test_corrupt_file_recovered_with_archive(self):
        st = self.store()
        st.add_item("rice", 2)
        with open(st.path, "w", encoding="utf-8") as handle:
            handle.write("{not json!!")
        again = self.store()
        self.assertEqual(again.items, {})
        archives = [name for name in os.listdir(again.data_dir)
                    if name.startswith("pantry-corrupt-")]
        self.assertEqual(len(archives), 1)
        with open(os.path.join(again.data_dir, archives[0]), "r",
                  encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{not json!!")

    def test_load_skips_invalid_rows(self):
        st = self.store()
        st.add_item("good rice", 2)
        with open(st.path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        data["items"].append({"name": "", "quantity": 5})
        data["items"].append({"name": "bad qty", "quantity": -1})
        data["items"].append({"name": "bad date", "expiry_date": "hoi"})
        data["items"].append({"name": "keeper", "quantity": 3})
        with open(st.path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        again = self.store()
        self.assertEqual(sorted(again.items), ["good rice", "keeper"])

    def test_item_names_cannot_escape_storage(self):
        st = self.store()
        sneaky = "../../outside/red"
        st.add_item(sneaky, 1)
        self.assertEqual(os.path.basename(st.path), pm.DATA_FILENAME)
        self.assertEqual(st.data_dir, os.path.realpath(os.path.join(self.tmp,
                                                                    "pantry")))
        again = self.store()
        self.assertIn(sneaky, again.items)
        outside = os.path.join(self.tmp, "outside")
        self.assertFalse(os.path.exists(outside))


class PathSafetyTests(PantryTestBase):
    def test_plain_tempdir_accepted(self):
        st = self.store()
        self.assertEqual(st.data_dir, os.path.realpath(os.path.join(self.tmp, "pantry")))
        st.add_item("rice", 1)
        self.assertTrue(os.path.exists(st.path))

    def test_empty_dir_rejected(self):
        with self.assertRaises(pm.UsageError):
            pm.safe_data_dir("   ")
        with self.assertRaises(pm.UsageError):
            pm.safe_data_dir(None)

    def test_dotdot_rejected(self):
        with self.assertRaises(pm.UsageError):
            pm.safe_data_dir(os.path.join(self.tmp, "..", "elsewhere"))
        with self.assertRaises(pm.UsageError):
            pm.safe_data_dir("../elsewhere")

    def test_system_directories_rejected(self):
        for raw in ("/", "/etc", "/usr/share/data", "/var/tmp/pantry", "/proc/1"):
            with self.assertRaises(pm.UsageError):
                pm.safe_data_dir(raw)

    def test_lookalike_paths_allowed(self):
        pm.safe_data_dir(os.path.join(self.tmp, "various"))
        pm.safe_data_dir(os.path.join(self.tmp, "etcetera"))

    def test_symlink_escape_rejected(self):
        os.makedirs(os.path.join(self.tmp, "linkfarm"), exist_ok=True)
        link = os.path.join(self.tmp, "linkfarm", "badlink")
        os.symlink("/etc", link)
        with self.assertRaises(pm.UsageError):
            pm.safe_data_dir(link)

    def test_symlink_to_valid_location_resolves(self):
        target = os.path.join(self.tmp, "real-dir")
        os.makedirs(target, exist_ok=True)
        link = os.path.join(self.tmp, "goodlink")
        os.symlink(target, link)
        resolved = pm.safe_data_dir(link)
        self.assertEqual(resolved, os.path.realpath(target))

    def test_relative_path_resolves_against_cwd(self):
        cwd = os.getcwd()
        try:
            os.chdir(self.tmp)
            resolved = pm.safe_data_dir("pantry-data")
            self.assertEqual(resolved, os.path.realpath(os.path.join(self.tmp,
                                                                     "pantry-data")))
        finally:
            os.chdir(cwd)


class DateParserTests(PantryTestBase):
    def test_parse_iso(self):
        self.assertEqual(pm.parse_iso_date("2026-10-31"), date(2026, 10, 31))
        self.assertEqual(pm.parse_iso_date("2026-10-31T05:00:00"), date(2026, 10, 31))
        self.assertIsNone(pm._opt_date(None))
        self.assertIsNone(pm._opt_date(""))
        with self.assertRaises(pm.UsageError):
            pm.parse_iso_date("10/31/2026")
        with self.assertRaises(pm.UsageError):
            pm.parse_iso_date("")


class MatchingTests(PantryTestBase):
    def test_singularize(self):
        cases = {
            "eggs": "egg",
            "tomatoes": "tomato",
            "potatoes": "potato",
            "berries": "berry",
            "classes": "class",
            "buses": "bus",
            "rice": "rice",
        }
        for raw, expected in cases.items():
            self.assertEqual(pm.singularize(raw), expected)

    def test_same_ingredient(self):
        self.assertTrue(pm.same_ingredient("Eggs", "  egg "))
        self.assertTrue(pm.same_ingredient("Tomatoes", "tomato"))
        self.assertFalse(pm.same_ingredient("beans", "bees"))


class SampleDataTests(PantryTestBase):
    def test_sample_loads_and_serves(self):
        st = self.store()
        st.seed_sample(pm.sample_data())
        self.assertGreaterEqual(len(st.items), 10)
        self.assertEqual(len(st.recipes), 3)
        by_name = {report["name"]: report for report in st.can_cook()}
        self.assertEqual(by_name["Weeknight rice and bean bowl"]["status"], "partial")
        gaps, _expired = st.shopping_gaps()
        gap_names = [name for name, _count, _recipes in gaps]
        self.assertIn("parmesan", gap_names)
        self.assertIn("potatoes", gap_names)
        self.assertIn("olive oil", gap_names)

    def test_sample_dates_are_recent(self):
        sample = pm.sample_data()
        expiries = [row["expiry_date"] for row in sample["items"]
                    if row.get("expiry_date")]
        first_expiring = min(expiries)
        self.assertGreaterEqual(date.fromisoformat(first_expiring), date.today())


class ServerProtocolTests(PantryTestBase):
    def server(self):
        st = self.store()
        return pm.PantryServer(st)

    def call(self, server, payload):
        response = server.handle_request(payload)
        return json.loads(json.dumps(response)) if response is not None else None

    def test_initialize(self):
        server = self.server()
        response = self.call(server, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18",
                       "capabilities": {}, "clientInfo": {"name": "test-client"}},
        })
        self.assertEqual(response["id"], 1)
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(response["result"]["serverInfo"]["name"], pm.SERVER_NAME)
        self.assertIn("tools", response["result"]["capabilities"])

    def test_initialize_unknown_version_falls_back(self):
        server = self.server()
        response = self.call(server, {
            "jsonrpc": "2.0", "id": 2, "method": "initialize",
            "params": {"protocolVersion": "1999-01-01"},
        })
        self.assertEqual(response["result"]["protocolVersion"], pm.PROTOCOL_VERSION)

    def test_ping(self):
        server = self.server()
        response = self.call(server, {"jsonrpc": "2.0", "id": 3, "method": "ping"})
        self.assertEqual(response["result"], {})

    def test_tools_list_names(self):
        server = self.server()
        response = self.call(server, {"jsonrpc": "2.0", "id": 4,
                                      "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(names, set(pm.TOOL_HANDLERS))
        self.assertGreaterEqual(len(names), 10)
        for tool in response["result"]["tools"]:
            self.assertIn("description", tool)
            self.assertIn("inputSchema", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_tools_call_round_trip(self):
        server = self.server()
        response = self.call(server, {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "add_item",
                       "arguments": {"name": "eggs", "quantity": 6, "unit": "each",
                                     "location": "fridge",
                                     "expiry_date": self.in_days(9)}},
        })
        content = response["result"]["content"][0]["text"]
        self.assertIn("Added 6 each eggs", content)
        response = self.call(server, {
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "expiring_soon", "arguments": {"days": 14}},
        })
        text = response["result"]["content"][0]["text"]
        self.assertIn("eggs", text)
        self.assertFalse(response["result"]["isError"])

    def test_tools_call_full_flow(self):
        server = self.server()

        def call(tool, arguments=None, req_id=99):
            return self.call(server, {
                "jsonrpc": "2.0", "id": req_id, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments or {}},
            })

        call("add_item", {"name": "rice", "quantity": 3, "unit": "cup"})
        call("add_item", {"name": "canned beans", "quantity": 2, "unit": "can"})
        call("add_item", {"name": "lime", "quantity": 1, "expiry_date":
                          self.in_days(4)})
        call("add_recipe", {"name": "Bean bowl",
                            "ingredients": [{"name": "rice", "quantity": 1},
                                            {"name": "canned beans", "quantity": 1},
                                            {"name": "lime", "quantity": 1}]})
        cook = call("what_can_i_cook")["result"]["content"][0]["text"]
        self.assertIn("Ready to cook now:", cook)
        self.assertIn("Bean bowl", cook)
        call("consume_item", {"name": "lime", "amount": 1})
        cook = call("what_can_i_cook")["result"]["content"][0]["text"]
        self.assertNotIn("Ready to cook now:", cook)
        self.assertIn("missing: lime", cook)
        gaps = call("shopping_gaps")["result"]["content"][0]["text"]
        self.assertIn("lime", gaps)

    def test_tools_call_usage_error_is_result_error(self):
        server = self.server()
        response = self.call(server, {
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "consume_item",
                       "arguments": {"name": "ghost peppers", "amount": 1}},
        })
        self.assertIsInstance(response["result"]["isError"], bool)
        self.assertTrue(response["result"]["isError"])
        self.assertIn("No item named", response["result"]["content"][0]["text"])

    def test_tools_call_bad_arguments(self):
        server = self.server()
        response = self.call(server, {
            "jsonrpc": "2.0", "id": 8, "method": "tools/call",
            "params": {"name": "add_item",
                       "arguments": {"name": "rice", "quantity": "lots"}},
        })
        self.assertTrue(response["result"]["isError"])
        self.assertIn("must be a number", response["result"]["content"][0]["text"])
        response = self.call(server, {
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "add_item", "arguments": {"quantity": 1}},
        })
        self.assertTrue(response["result"]["isError"])
        self.assertIn("required", response["result"]["content"][0]["text"])

    def test_unknown_tool_is_protocol_error(self):
        server = self.server()
        response = self.call(server, {
            "jsonrpc": "2.0", "id": 10, "method": "tools/call",
            "params": {"name": "launch_missiles", "arguments": {}},
        })
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("Unknown tool", response["error"]["message"])

    def test_unknown_method(self):
        server = self.server()
        response = self.call(server, {"jsonrpc": "2.0", "id": 11,
                                      "method": "resources/list"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_notifications_get_no_response(self):
        server = self.server()
        self.assertIsNone(
            server.handle_request({"jsonrpc": "2.0", "method":
                                   "notifications/initialized"}))
        self.assertIsNone(
            server.handle_request({"jsonrpc": "2.0", "method":
                                   "notifications/cancelled",
                                   "params": {"requestId": 1}}))

    def test_handle_line_variants(self):
        server = self.server()
        self.assertIsNone(server.handle_line("   "))
        self.assertIsNone(server.handle_line(""))
        response = json.loads(server.handle_line("this is not json"))
        self.assertEqual(response["error"]["code"], -32700)
        response = json.loads(server.handle_line('{"jsonrpc":"2.0","id":1,'
                                                 '"method":"ping"}'))
        self.assertEqual(response["result"], {})
        self.assertIsNone(server.handle_line('{"jsonrpc":"2.0","method":'
                                             '"notifications/x"}'))
        response = json.loads(server.handle_line('"just a string"'))
        self.assertEqual(response["error"]["code"], -32600)


class SampleFileTests(PantryTestBase):
    def test_shipped_sample_file_exists_and_parses(self):
        sample_path = os.path.join(HERE, "samples", "sample_pantry.json")
        self.assertTrue(os.path.exists(sample_path))
        with open(sample_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["format"], pm.DATA_FORMAT)
        self.assertGreaterEqual(len(data["items"]), 10)
        self.assertGreaterEqual(len(data["recipes"]), 3)

    def test_shipped_sample_loads_into_store(self):
        sample_path = os.path.join(HERE, "samples", "sample_pantry.json")
        data_dir = os.path.join(self.tmp, "pantry")
        os.makedirs(data_dir, exist_ok=True)
        shutil.copy(sample_path, os.path.join(data_dir, pm.DATA_FILENAME))
        st = pm.PantryStore(data_dir, seed_on_first_run=False)
        self.assertGreaterEqual(len(st.items), 10)
        self.assertGreaterEqual(len(st.recipes), 3)

    def test_load_sample_cli_first_run_then_force_gate(self):
        data_dir = os.path.join(self.tmp, "pantry")
        result = subprocess.run(
            [sys.executable, os.path.join(HERE, "pantry_mcp.py"),
             "--data-dir", data_dir, "--load-sample"],
            capture_output=True, text=True, timeout=60, input="",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(data_dir, pm.DATA_FILENAME), "r",
                  encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertGreaterEqual(len(data["items"]), 10)
        result = subprocess.run(
            [sys.executable, os.path.join(HERE, "pantry_mcp.py"),
             "--data-dir", data_dir, "--load-sample"],
            capture_output=True, text=True, timeout=60, input="",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--force", result.stderr)


class CliTests(PantryTestBase):
    def run_cli(self, *extra, input_text=""):
        return subprocess.run(
            [sys.executable, os.path.join(HERE, "pantry_mcp.py"), *extra],
            capture_output=True, text=True, timeout=60, input=input_text,
        )

    def test_version_flag(self):
        result = self.run_cli("--version")
        self.assertEqual(result.returncode, 0)
        self.assertIn(pm.SERVER_VERSION, result.stdout)

    def test_first_run_auto_seeds_sample(self):
        data_dir = os.path.join(self.tmp, "pantry")
        result = self.run_cli("--data-dir", data_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.exists(os.path.join(data_dir, pm.DATA_FILENAME)))
        with open(os.path.join(data_dir, pm.DATA_FILENAME), "r",
                  encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertGreaterEqual(len(data["items"]), 10)

    def test_no_sample_flag_skips_seeding(self):
        data_dir = os.path.join(self.tmp, "pantry")
        result = self.run_cli("--data-dir", data_dir, "--no-sample")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(os.path.join(data_dir, pm.DATA_FILENAME)))

    def test_forbidden_data_dir_refused_by_cli(self):
        result = self.run_cli("--data-dir", "/etc")
        self.assertEqual(result.returncode, 2)
        self.assertIn("refusing", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

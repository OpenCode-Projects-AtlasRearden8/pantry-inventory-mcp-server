#!/usr/bin/env python3
"""End-to-end smoke test for pantry-inventory-mcp-server.

Spawns the real server as a subprocess over stdio, exercises every tool once,
and checks the data file, automatic backups, and JSON-RPC error handling.

Run with: python3 smoke_test.py
"""
import json
import os
import select
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "pantry_mcp.py")


class SmokeFailure(Exception):
    pass


class ServerClient:
    def __init__(self, data_dir):
        self.err_path = os.path.join(data_dir, "server-smoke.err")
        self.err_file = open(self.err_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, SERVER, "--data-dir", data_dir, "--no-sample"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.err_file, text=True, bufsize=1,
        )
        self._buf = ""

    def send(self, method, params=None, request_id=None):
        message = {"jsonrpc": "2.0", "method": method}
        if request_id is not None:
            message["id"] = request_id
        if params is not None:
            message["params"] = params
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def recv_line(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while "\n" not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SmokeFailure(
                    f"timeout waiting for server stdout; stderr so far: "
                    f"{self.stderr()!r}")
            ready, _, _ = select.select([self.proc.stdout], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise SmokeFailure(f"server exited early; stderr: {self.stderr()!r}")
            self._buf += chunk.decode("utf-8")
        line, self._buf = self._buf.split("\n", 1)
        return line

    def stderr(self):
        try:
            self.err_file.flush()
        except ValueError:
            return ""
        with open(self.err_path, "r", encoding="utf-8") as handle:
            return handle.read()

    def request(self, method, params=None, request_id=1):
        self.send(method, params, request_id)
        line = self.recv_line()
        if not line:
            raise SmokeFailure(f"no response for {method}")
        return json.loads(line)

    def result_text(self, method, params, request_id):
        response = self.request(method, params, request_id)
        result = response.get("result")
        if result is None:
            raise SmokeFailure(f"{method} returned error: {response}")
        texts = "".join(block.get("text", "") for block in result.get("content", []))
        return texts, result

    def expect(self, label, text, fragments):
        for fragment in fragments:
            if fragment not in text:
                raise SmokeFailure(f"{label}: expected {fragment!r} in response: "
                                   f"{text!r}")

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            code = self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return 1
        self.err_file.close()
        return code


def main():
    counter = [0]

    def step(name):
        counter[0] += 1
        print(f"[{counter[0]:2d}] {name}")

    data_dir = tempfile.mkdtemp(prefix="pantry-smoke-")
    client = ServerClient(data_dir)
    try:
        step("initialize + notifications/initialized + tools/list")
        response = client.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smoke-test", "version": "1.0.0"},
        }, request_id=1)
        info = response.get("result", {}).get("serverInfo", {})
        if info.get("name") != "pantry-inventory-mcp-server":
            raise SmokeFailure(f"bad serverInfo: {info}")
        client.send("notifications/initialized", {})
        tools = client.request("tools/list", {}, request_id=2)
        tool_names = [tool["name"] for tool in tools["result"]["tools"]]
        if len(tool_names) < 10:
            raise SmokeFailure(f"expected at least 10 tools, got {tool_names}")
        print(f"      server OK; {len(tool_names)} tools: "
              + ", ".join(sorted(tool_names)))

        expiry_day = time.strftime(
            "%Y-%m-%d", time.localtime(time.time() + 2 * 86400))

        step("add_item: rice, onions (expiring soon), canned beans")
        text, _ = client.result_text("tools/call", {
            "name": "add_item", "arguments": {"name": "rice", "quantity": 3,
                                              "unit": "cup", "location": "pantry"}},
            request_id=3)
        client.expect("add rice", text, ["rice"])
        text, _ = client.result_text("tools/call", {
            "name": "add_item", "arguments": {
                "name": "onions", "quantity": 2, "unit": "head",
                "location": "pantry", "expiry_date": expiry_day}},
            request_id=4)
        client.expect("add onions", text, ["onions", expiry_day])
        client.result_text("tools/call", {
            "name": "add_item", "arguments": {"name": "canned beans",
                                              "quantity": 2, "unit": "can"}},
            request_id=5)

        step("expiring_soon shows the onions")
        text, _ = client.result_text("tools/call",
                                     {"name": "expiring_soon", "arguments": {}},
                                     request_id=6)
        client.expect("expiring", text, ["onions", expiry_day])

        step("add_recipe: Bean bowl (rice + beans + onion + lime)")
        text, _ = client.result_text("tools/call", {
            "name": "add_recipe", "arguments": {
                "name": "Bean bowl",
                "ingredients": [
                    {"name": "rice", "quantity": 1, "unit": "cup"},
                    {"name": "canned beans", "quantity": 1, "unit": "can"},
                    {"name": "onion", "quantity": 1},
                    {"name": "lime", "quantity": 1}],
                "instructions": "Warm everything and pile it in a bowl."}},
            request_id=7)
        client.expect("add recipe", text, ["Bean bowl", "4"])

        step("what_can_i_cook: plural 'onions' covers 'onion'; lime missing")
        text, _ = client.result_text("tools/call",
                                     {"name": "what_can_i_cook", "arguments": {}},
                                     request_id=8)
        client.expect("cook report", text, ["Almost there:", "Bean bowl", "lime"])

        step("shopping_gaps lists the lime")
        text, _ = client.result_text("tools/call",
                                     {"name": "shopping_gaps", "arguments": {}},
                                     request_id=9)
        client.expect("gap list", text, ["lime"])

        step("consume_item refuses to overdraw (isError, 'only have')")
        response = client.request("tools/call", {
            "name": "consume_item", "arguments": {"name": "onions", "amount": 5}},
            request_id=10)
        result = response.get("result", {})
        overdraw_text = "".join(
            block.get("text", "") for block in result.get("content", []))
        if not result.get("isError") or "only have" not in overdraw_text:
            raise SmokeFailure(f"overdraw should be an isError result: {response}")

        step("consume_item to zero removes the item")
        text, _ = client.result_text("tools/call", {
            "name": "consume_item", "arguments": {"name": "onions", "amount": 2}},
            request_id=11)
        client.expect("consume", text, ["removed from inventory"])

        step("list_items shows stock, onions gone")
        text, _ = client.result_text("tools/call", {"name": "list_items",
                                                    "arguments": {}}, request_id=12)
        if "onions:" in text:
            raise SmokeFailure(f"onions should be gone but appear in: {text!r}")
        client.expect("list", text, ["rice"])

        step("data file + auto-backup exist on disk")
        pantry_path = os.path.join(data_dir, "pantry.json")
        if not os.path.exists(pantry_path):
            raise SmokeFailure("pantry.json was not written")
        with open(pantry_path, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        if stored.get("format") != "pantry-inventory/1":
            raise SmokeFailure(f"bad format marker: {stored.get('format')}")
        backups = os.listdir(os.path.join(data_dir, "backups"))
        if len(backups) < 1:
            raise SmokeFailure("no automatic backup was created")

        step("unknown tool -> -32602; bad JSON -> -32700")
        response = client.request("tools/call", {"name": "no_such_tool",
                                                 "arguments": {}}, request_id=13)
        if response.get("error", {}).get("code") != -32602:
            raise SmokeFailure(f"unknown tool should be -32602: {response}")
        client.proc.stdin.write("this is not json\n")
        client.proc.stdin.flush()
        bad = json.loads(client.recv_line())
        if bad.get("error", {}).get("code") != -32700:
            raise SmokeFailure(f"bad JSON should be -32700: {bad}")

        step("graceful shutdown on EOF")
        code = client.close()
        if code != 0:
            raise SmokeFailure(
                f"server exited with {code}; stderr: {client.stderr()!r}")

        print(f"\nSMOKE TEST PASSED ({counter[0]} steps). "
              f"Scratch data dir: {data_dir}")
        return 0
    except SmokeFailure as failure:
        print(f"\nSMOKE TEST FAILED: {failure}", file=sys.stderr)
        print("server stderr:\n" + client.stderr(), file=sys.stderr)
        return 1
    finally:
        try:
            client.proc.kill()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())

import json
import os
import subprocess
import sys
import tempfile
import unittest

import support
from chrome_session_mcp import server
from chrome_session_mcp.util import ToolError

CONF_RO = {"type": "confluence", "base_url": "https://c.example.com"}


def tool_names():
    return [t.name for t in server.visible_tools()]


class ToolList(unittest.TestCase):
    def test_only_builtins_without_connectors(self):
        support.load({})
        self.assertEqual(tool_names(), ["list_connectors", "auth_status", "login", "open_in_browser"])

    def test_tools_appear_per_configured_type(self):
        support.load({"g": {"type": "grafana", "base_url": "https://g.example.com"}})
        names = tool_names()
        self.assertIn("grafana_run_panel", names)
        self.assertFalse(any(n.startswith("slack_") for n in names))

    def test_write_tools_need_allow_write(self):
        support.load({"c": CONF_RO})
        self.assertNotIn("confluence_update_page", tool_names())
        support.load({"c": dict(CONF_RO, allow_write=True)})
        self.assertIn("confluence_update_page", tool_names())

    def test_annotations(self):
        support.load({"c": dict(CONF_RO, allow_write=True)})
        d = {t["name"]: t for t in map(server.describe, server.visible_tools())}
        self.assertTrue(d["confluence_get_page"]["annotations"]["readOnlyHint"])
        upd = d["confluence_update_page"]["annotations"]
        self.assertFalse(upd["readOnlyHint"])
        self.assertTrue(upd["destructiveHint"])

    def test_instance_argument(self):
        support.load({"g1": {"type": "grafana", "base_url": "https://g1.example.com"},
                      "g2": {"type": "grafana", "base_url": "https://g2.example.com"}})
        s = server.schema(server.TOOLS["grafana_get"])
        self.assertEqual(s["properties"]["instance"]["enum"], ["g1", "g2"])
        self.assertEqual(s["required"][0], "instance")
        # A tool with a url argument can pick the instance from the URL instead.
        self.assertNotIn("instance", server.schema(server.TOOLS["grafana_resolve"])["required"])


class Dispatch(unittest.TestCase):
    def setUp(self):
        support.load({"j": {"type": "jenkins", "base_url": "https://j.example.com"}})

    def test_missing_argument(self):
        with self.assertRaisesRegex(ToolError, "missing required argument"):
            server.run_tool("open_in_browser", {})

    def test_unknown_argument(self):
        with self.assertRaisesRegex(ToolError, "unknown argument"):
            server.run_tool("jenkins_pr_status", {"pr": 1, "colour": "red"})

    def test_hidden_tool_is_unknown(self):
        with self.assertRaises(LookupError):
            server.run_tool("slack_search", {"query": "x"})

    def test_missing_job_path_is_a_tool_error(self):
        with self.assertRaisesRegex(ToolError, "no job_path"):
            server.run_tool("jenkins_pr_status", {"pr": 12, "build": 3})


class JsonRpc(unittest.TestCase):
    """Run the real server as a subprocess, the way an MCP client does."""

    def test_initialize_list_and_call(self):
        cfg = os.path.join(tempfile.mkdtemp(), "config.json")
        with open(cfg, "w") as fh:
            json.dump({"connectors": {"k": {"type": "kibana", "base_url": "https://k.example.com"}}}, fh)
        env = dict(os.environ, PYTHONPATH=os.path.join(support.ROOT, "src"),
                   CHROME_SESSION_MCP_CONFIG=cfg)
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "list_connectors", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "nope"},
        ]
        out = subprocess.run([sys.executable, "-m", "chrome_session_mcp"], env=env, timeout=60,
                             input="".join(json.dumps(m) + "\n" for m in msgs),
                             capture_output=True, text=True).stdout
        got = {m["id"]: m for m in map(json.loads, out.splitlines())}
        self.assertEqual(got[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(got[1]["result"]["serverInfo"]["name"], "chrome-session-mcp")
        names = [t["name"] for t in got[2]["result"]["tools"]]
        self.assertEqual(names[0], "list_connectors")
        self.assertIn("kibana_search", names)
        listed = json.loads(got[3]["result"]["content"][0]["text"])
        self.assertEqual(listed["connectors"]["k"]["type"], "kibana")
        self.assertEqual(got[4]["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()

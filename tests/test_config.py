import unittest

import support
from chrome_session_mcp.config import RUNTIME
from chrome_session_mcp.util import ToolError

G1 = {"type": "grafana", "base_url": "https://g1.example.com"}
G2 = {"type": "grafana", "base_url": "https://g2.example.com"}


class Loading(unittest.TestCase):
    def test_unknown_type_is_reported_not_fatal(self):
        support.load({"x": {"type": "nope", "base_url": "https://x"}, "g": G1})
        self.assertIn("unknown type", RUNTIME.errors["x"])
        self.assertIn("g", RUNTIME.instances)

    def test_unknown_setting(self):
        support.load({"g": dict(G1, colour="red")})
        self.assertIn("unknown setting(s) colour", RUNTIME.errors["g"])

    def test_type_specific_option_is_allowed(self):
        support.load({"j": {"type": "jenkins", "base_url": "https://j", "job_path": "job/a"}})
        self.assertEqual(RUNTIME.errors, {})

    def test_base_url_must_be_http(self):
        support.load({"g": {"type": "grafana", "base_url": "grafana.example.com"}})
        self.assertIn("base_url must be an http(s) URL", RUNTIME.errors["g"])

    def test_slack_needs_no_base_url(self):
        support.load({"s": {"type": "slack", "workspace": "acme.slack.com"}})
        self.assertEqual(RUNTIME.errors, {})
        self.assertEqual(RUNTIME.instances["s"].login_url, "https://acme.slack.com/")

    def test_broken_plugin_is_reported(self):
        support.load({}, plugins=["does_not_exist_xyz"])
        self.assertIn("<plugin does_not_exist_xyz>", RUNTIME.errors)

    def test_connectors_must_be_an_object(self):
        RUNTIME.load({"connectors": []})
        self.assertIn("<config>", RUNTIME.errors)


class Resolve(unittest.TestCase):
    def test_single_instance_needs_no_name(self):
        support.load({"g1": G1})
        self.assertEqual(RUNTIME.resolve("grafana").name, "g1")

    def test_several_instances_need_a_name_or_url(self):
        support.load({"g1": G1, "g2": G2})
        with self.assertRaisesRegex(ToolError, "pass instance"):
            RUNTIME.resolve("grafana")
        self.assertEqual(RUNTIME.resolve("grafana", "g2").name, "g2")
        self.assertEqual(RUNTIME.resolve("grafana", url="https://g2.example.com/d/abc").name, "g2")

    def test_unknown_instance(self):
        support.load({"g1": G1})
        with self.assertRaisesRegex(ToolError, "no grafana connector named 'zz'"):
            RUNTIME.resolve("grafana", "zz")

    def test_write_needs_allow_write(self):
        support.load({"c": {"type": "confluence", "base_url": "https://c.example.com"}})
        with self.assertRaisesRegex(ToolError, "writes are disabled"):
            RUNTIME.resolve("confluence", "c", write=True)
        with self.assertRaisesRegex(ToolError, "with allow_write"):
            RUNTIME.resolve("confluence", write=True)


class CustomConnector(unittest.TestCase):
    def test_example_plugin_registers_its_type_and_tools(self):
        support.load({"flags": {"type": "acme-flags", "base_url": "https://flags.example.com",
                                "environment": "staging"}},
                     plugin_paths=[support.EXAMPLES + "/custom_connectors"],
                     plugins=["acme_connectors.feature_flags"])
        self.assertEqual(RUNTIME.errors, {})
        self.assertEqual(RUNTIME.instances["flags"].environment, "staging")
        from chrome_session_mcp.server import visible_tools
        names = [t.name for t in visible_tools()]
        self.assertIn("flags_get", names)
        self.assertNotIn("flags_set", names)  # write tool hidden without allow_write


if __name__ == "__main__":
    unittest.main()

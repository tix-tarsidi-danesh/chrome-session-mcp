import re
import time
import unittest

import support
from chrome_session_mcp.config import RUNTIME
from chrome_session_mcp.connectors import confluence, grafana, jenkins, slack
from chrome_session_mcp.util import ToolError


class Jenkins(unittest.TestCase):
    def test_parse_blue_ocean_url(self):
        url = ("https://ci.example.com/blue/organizations/jenkins/team%2Fmy-service/detail/"
               "PR-262/11/pipeline")
        self.assertEqual(jenkins.parse_url(url), ("job/team/job/my-service", "262", "11"))

    def test_parse_classic_url(self):
        url = "https://ci.example.com/job/team/job/my-service/job/PR-262/11/console"
        self.assertEqual(jenkins.parse_url(url), ("job/team/job/my-service", "262", "11"))

    def test_parse_without_build(self):
        url = "https://ci.example.com/job/a/job/PR-5/"
        self.assertEqual(jenkins.parse_url(url), ("job/a", "5", None))

    def test_parse_rejects_non_pr_url(self):
        with self.assertRaises(ToolError):
            jenkins.parse_url("https://ci.example.com/job/a/job/main/3/")

    def test_target_normalises_pr(self):
        support.load({"j": {"type": "jenkins", "base_url": "https://ci.example.com", "job_path": "job/a"}})
        j = RUNTIME.instances["j"]
        self.assertEqual(jenkins.target(j, pr="PR-7", build=2), ("job/a", "7", "2"))
        self.assertEqual(jenkins.target(j, pr="#7", build=2), ("job/a", "7", "2"))
        with self.assertRaisesRegex(ToolError, "invalid PR number"):
            jenkins.target(j, pr="seven", build=2)


class Slack(unittest.TestCase):
    def test_permalink(self):
        self.assertEqual(slack.parse_permalink("https://acme.slack.com/archives/C0123/p1727851234123456"),
                         ("C0123", "1727851234.123456"))

    def test_permalink_inside_a_thread(self):
        url = "https://acme.slack.com/archives/C0123/p1727851299000001?thread_ts=1727851234.123456&cid=C0123"
        self.assertEqual(slack.parse_permalink(url), ("C0123", "1727851234.123456"))

    def test_timestamps(self):
        self.assertEqual(slack.slack_ts("1727851234.5"), "1727851234.5")
        self.assertAlmostEqual(float(slack.slack_ts("now-1h")), time.time() - 3600, delta=5)
        self.assertIsNone(slack.slack_ts(""))
        with self.assertRaises(ToolError):
            slack.slack_ts("yesterday")

    def test_token_is_matched_to_the_workspace(self):
        support.load({"a": {"type": "slack", "team_id": "T1"},
                      "b": {"type": "slack", "workspace": "acme.slack.com"}})
        a, b = RUNTIME.instances["a"], RUNTIME.instances["b"]
        self.assertTrue(a._matches({"team_id": "T1"}))
        self.assertFalse(a._matches({"team_id": "T2"}))
        self.assertTrue(b._matches({"url": "https://acme.slack.com/"}))
        self.assertFalse(b._matches({"url": "https://other.slack.com/"}))
        self.assertTrue(b.matches_url("https://acme.slack.com/archives/C1/p1"))


class Grafana(unittest.TestCase):
    def test_uid(self):
        self.assertEqual(grafana.uid_from("https://g.example.com/d/abc123/my-dash?orgId=1"), "abc123")
        self.assertEqual(grafana.uid_from("abc123"), "abc123")

    def test_time(self):
        self.assertEqual(grafana.parse_ts("1700000000000", 0), "1700000000000")
        self.assertEqual(grafana.parse_ts(None, 42), "42")
        now = int(time.time() * 1000)
        self.assertAlmostEqual(int(grafana.parse_ts("now-30m", 0)), now - 1800000, delta=5000)

    def test_interpolate(self):
        q = 'rate(x{svc="$service", env="${env}", pod=~"[[pods]]"}[$__rate_interval])'
        out = grafana.interpolate(q, {"service": "api", "env": "prod", "pods": ["a", "b"]})
        self.assertEqual(out, 'rate(x{svc="api", env="prod", pod=~"a,b"}[$__rate_interval])')


class Confluence(unittest.TestCase):
    BODY = ("<p>x</p>" * 400 + '<ac:image><ri:attachment ri:filename="a.png"/></ac:image>'
            '<ac:structured-macro ac:name="toc"/>')

    def test_safe_edit(self):
        self.assertEqual(confluence.check_update(self.BODY, self.BODY.replace("<p>x</p>", "<p>y</p>", 1)), [])

    def test_empty_body(self):
        self.assertEqual(confluence.check_update(self.BODY, "  "), ["the new body is empty"])

    def test_shrink_and_lost_image(self):
        problems = confluence.check_update(self.BODY, "<p>short</p>")
        self.assertTrue(any("shrinks" in p for p in problems))
        self.assertTrue(any(p.startswith("images (ac:image)") for p in problems))
        self.assertTrue(any(p.startswith("macros") for p in problems))

    def test_overrides(self):
        self.assertEqual(confluence.check_update(self.BODY, "<p>short</p>", True, True), [])

    def test_page_id(self):
        self.assertEqual(confluence.page_id("https://x.atlassian.net/wiki/spaces/S/pages/123/T"), "123")
        self.assertEqual(confluence.page_id("https://x/wiki/pages/viewpage.action?pageId=456"), "456")
        self.assertEqual(confluence.page_id(789), "789")
        with self.assertRaises(ToolError):
            confluence.page_id("home")


class CookieScope(unittest.TestCase):
    def test_cookies_never_go_to_another_host(self):
        support.load({"h": {"type": "http", "base_url": "https://admin.example.com"}})
        h = RUNTIME.instances["h"]
        self.assertEqual(h.url("/api/x"), "https://admin.example.com/api/x")
        self.assertEqual(h.url("api/x"), "https://admin.example.com/api/x")
        self.assertEqual(h.url("https://admin.example.com/y"), "https://admin.example.com/y")
        with self.assertRaisesRegex(ToolError, "refusing to send its cookies"):
            h.url("https://evil.example.net/y")
        self.assertTrue(re.match(r"https://admin\.example\.com/", h.url("@evil.example.net/x")))


if __name__ == "__main__":
    unittest.main()

import os
import unittest
from unittest import mock

import support  # noqa: F401
from chrome_session_mcp import auth
from chrome_session_mcp.util import AuthExpired, ToolError


class FakeSite(auth.Site):
    name, host, login_url = "fake", "fake.example.com", "https://fake.example.com/login"

    def __init__(self, valid_after=1):
        self.reloads = 0
        self.valid_after = valid_after

    def reload(self):
        self.reloads += 1

    def probe(self):
        return self.reloads > self.valid_after


class WithLogin(unittest.TestCase):
    def test_no_browser_reports_the_expiry(self):
        def call():
            raise AuthExpired("fake", "HTTP 401")
        with self.assertRaisesRegex(ToolError, "session expired .*Log in to https://fake"):
            auth.with_login(FakeSite(), call)

    def test_renews_the_session_and_retries_once(self):
        calls = []

        def call():
            calls.append(1)
            if len(calls) == 1:
                raise AuthExpired("fake", "HTTP 401")
            return "ok"

        site = FakeSite(valid_after=2)
        with mock.patch.dict(os.environ, {"CHROME_SESSION_MCP_NO_BROWSER": "0"}), \
                mock.patch.object(auth, "POLL", 0), \
                mock.patch.object(auth.chrome, "open_url", return_value="Default") as opened:
            self.assertEqual(auth.with_login(site, call), "ok")
        opened.assert_called_once_with(site.login_url, site.host)
        self.assertEqual(len(calls), 2)

    def test_login_times_out(self):
        site = FakeSite(valid_after=10 ** 6)
        with mock.patch.object(auth, "POLL", 0):
            with self.assertRaisesRegex(ToolError, "still not logged in after 0s"):
                auth.login(site, wait=0)

    def test_still_rejected_after_login(self):
        def call():
            raise AuthExpired("fake", "HTTP 403")
        with mock.patch.dict(os.environ, {"CHROME_SESSION_MCP_NO_BROWSER": "0"}), \
                mock.patch.object(auth, "POLL", 0), \
                mock.patch.object(auth.chrome, "open_url", return_value="Default"), \
                mock.patch.object(auth.chrome, "profile_for", return_value="Default"):
            with self.assertRaisesRegex(ToolError, "still rejected"):
                auth.with_login(FakeSite(valid_after=0), call)


if __name__ == "__main__":
    unittest.main()

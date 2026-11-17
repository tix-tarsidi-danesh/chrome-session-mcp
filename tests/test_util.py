import os
import unittest

import support  # noqa: F401
from chrome_session_mcp import util
from chrome_session_mcp.util import Response, host_of, looks_like_login


def resp(status=200, url="https://app.example.com/x", body=b"{}", headers=None):
    return Response(status, url, body, headers or {})


class LooksLikeLogin(unittest.TestCase):
    def test_normal_answer(self):
        self.assertFalse(looks_like_login(resp()))

    def test_landed_on_google_sign_in(self):
        self.assertTrue(looks_like_login(resp(url="https://accounts.google.com/o/oauth2/auth?x")))

    def test_iap_redirect_on_post(self):
        r = resp(302, body=b"Auth Cookie is invalid: Cookie Expired", headers={"Location": ""})
        self.assertTrue(looks_like_login(r))

    def test_redirect_to_login_page(self):
        self.assertTrue(looks_like_login(resp(302, headers={"Location": "/login?next=/"})))

    def test_redirect_elsewhere_is_not_login(self):
        self.assertFalse(looks_like_login(resp(302, headers={"Location": "/dashboards/home"})))

    def test_marker_in_body(self):
        self.assertTrue(looks_like_login(resp(body=b"<html>Sign in with Google</html>")))


class Helpers(unittest.TestCase):
    def test_host_of(self):
        self.assertEqual(host_of("https://u@a.example.com:8443/p?q"), "a.example.com:8443")
        self.assertEqual(host_of("a.example.com/x"), "a.example.com")

    def test_fit_spills_large_output_to_a_private_file(self):
        old = util.MAX_INLINE
        util.MAX_INLINE = 100
        try:
            out = util.fit("x" * 500, "t")
        finally:
            util.MAX_INLINE = old
        self.assertIn("full output written to", out)
        path = out.split("written to ", 1)[1].split(". Read", 1)[0]
        self.assertTrue(path.startswith(util.CACHE_DIR))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_fit_returns_small_output_as_is(self):
        self.assertEqual(util.fit("abc"), "abc")


if __name__ == "__main__":
    unittest.main()

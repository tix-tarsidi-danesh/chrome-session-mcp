"""Recover from an expired browser session by sending the user to Chrome.

Flow when a connector raises AuthExpired:

1. Open Chrome on the site's login URL (at most once per connector per
   REOPEN_COOLDOWN seconds, so a retrying agent cannot pile up tabs).
2. Poll every POLL seconds: re-read the cookies from Chrome's on-disk database
   and probe the site with them. Chrome flushes new cookies to disk on a delay
   (up to ~30 s after the login finishes), which is why we poll the database
   rather than wait a fixed time.
3. When the probe passes, retry the original call once. If the wait runs out,
   return an error telling the agent to retry after the user has logged in.

Settings (env wins over config): CHROME_SESSION_MCP_LOGIN_WAIT (seconds,
default 180; 0 = never wait) and CHROME_SESSION_MCP_NO_BROWSER=1 (never open
Chrome, only report the expiry).
"""
import os
import threading
import time

from . import chrome
from .util import AuthExpired, ToolError, log

POLL = 3
REOPEN_COOLDOWN = 120

_settings = {"login_wait": 180, "open_browser": True}
_last_open = {}
_site_locks = {}
_locks_guard = threading.Lock()


def configure(login_wait=None, open_browser=None):
    if login_wait is not None:
        _settings["login_wait"] = int(login_wait)
    if open_browser is not None:
        _settings["open_browser"] = bool(open_browser)


def login_wait():
    env = os.environ.get("CHROME_SESSION_MCP_LOGIN_WAIT")
    return int(env) if env else _settings["login_wait"]


def browser_enabled():
    return os.environ.get("CHROME_SESSION_MCP_NO_BROWSER") != "1" and _settings["open_browser"]


def _lock(name):
    with _locks_guard:
        return _site_locks.setdefault(name, threading.Lock())


class Site:
    """What session recovery needs to know about one site."""

    name = ""        # shown in messages, e.g. the connector instance name
    host = ""        # cookie host, e.g. "grafana.example.com"
    login_url = ""   # page that starts / completes the login

    def reload(self):
        """Drop cached credentials and re-read them from Chrome."""
        raise NotImplementedError

    def probe(self):
        """Return True when the current credentials work. Must not raise AuthExpired."""
        raise NotImplementedError


def login(site, wait=None, force_open=False):
    """Open Chrome for `site` and block until its session works (or time out).

    Returns a short status string. Raises ToolError on timeout.
    """
    wait = login_wait() if wait is None else wait
    with _lock(site.name):
        # Another call may have finished the login while we waited for the lock.
        site.reload()
        if site.probe() and not force_open:
            return f"{site.name}: session is valid."
        opened = ""
        if browser_enabled():
            last = _last_open.get(site.name, 0)
            if force_open or time.time() - last > REOPEN_COOLDOWN:
                prof = chrome.open_url(site.login_url, site.host)
                _last_open[site.name] = time.time()
                opened = f"Opened {site.login_url} in Chrome (profile '{prof}'). "
            else:
                opened = f"Chrome was already opened on {site.login_url} moments ago. "
        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(POLL)
            try:
                site.reload()
                if site.probe():
                    log(f"{site.name}: login detected")
                    return f"{opened}{site.name}: logged in, session is valid."
            except ToolError as e:  # e.g. a Keychain hiccup; keep waiting
                log(f"{site.name}: probe error while waiting: {e}")
        raise ToolError(
            f"{opened}{site.name}: still not logged in after {wait}s. Finish logging in to "
            f"{site.login_url} in Chrome (same profile), then call the tool again. "
            "Chrome can take up to ~30 s to save new cookies after the login completes.")


def with_login(site, fn):
    """Run fn(); on AuthExpired, renew the session through Chrome and retry once."""
    try:
        return fn()
    except AuthExpired as e:
        log(str(e))
        if login_wait() <= 0 or not browser_enabled():
            raise ToolError(f"{site.name}: session expired ({e.detail}). Log in to "
                            f"{site.login_url} in Chrome, then retry.")
        login(site)
    try:
        return fn()
    except AuthExpired as e:
        raise ToolError(f"{site.name}: the login looked valid but the call was still rejected "
                        f"({e.detail}). Check you logged in with the right account in Chrome "
                        f"profile '{chrome.profile_for(site.host)}', then retry.")

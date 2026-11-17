"""Slack through your Chrome session: no Slack app, bot, or token setup.

Config:  {"type": "slack", "workspace": "acme.slack.com"}     (or "team_id": "T0123ABCD")
         add "allow_write": true to enable slack_post.

The Slack web client needs two things, neither of which is an app token:

1. the `d` (+ `d-s`) session cookie for slack.com, decrypted from Chrome, and
2. an `xoxc-...` session token, which Slack keeps in the browser's localStorage
   (a LevelDB directory). We scan those files for it.

The `d` cookie covers every workspace you are signed in to, but each workspace
has its own xoxc token: the connector keeps the token whose `auth.test` answer
matches the configured workspace / team_id.
"""
import datetime
import glob
import json
import os
import re
import threading
import time
import urllib.parse

from .. import chrome
from ..auth import with_login
from ..registry import B, Connector, I, S, connector_type, tool
from ..util import AuthExpired, ToolError, cache_path, dumps, fit, http, write_private
from ..util import host_of as util_host

API = "https://slack.com/api"
READ_METHODS = {
    "auth.test", "team.info", "conversations.list", "conversations.history",
    "conversations.replies", "conversations.info", "conversations.members",
    "users.list", "users.info", "users.lookupByEmail", "users.conversations",
    "search.messages", "pins.list", "bookmarks.list", "reactions.get",
    "usergroups.list", "files.list", "files.info",
}
WRITE_METHODS = {"chat.postMessage"}
AUTH_ERRORS = {"invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive"}
CHANNELS_TTL = 86400


def _scan_tokens():
    """Candidate xoxc- tokens from Chrome's Local Storage LevelDB, newest files first."""
    prof = chrome.pinned_profile()
    files = []
    for p in ([prof] if prof else ["*"]):
        base = os.path.join(chrome.user_data_dir(), p, "Local Storage", "leveldb")
        files += glob.glob(base + "/*.ldb") + glob.glob(base + "/*.log")
    files.sort(key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0, reverse=True)
    seen, tokens = set(), []
    for f in files:
        try:
            with open(f, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        for m in re.findall(rb"xoxc-[0-9A-Za-z-]+", data):
            t = m.decode()
            if t.count("-") >= 3 and t not in seen:
                seen.add(t)
                tokens.append(t)
    return tokens


def fmt_ts(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return ts


@connector_type("slack")
class Slack(Connector):
    requires_base_url = False
    options = ("workspace", "team_id")

    def __init__(self, name, cfg):
        super().__init__(name, cfg)
        self.workspace = (cfg.get("workspace") or "").replace("https://", "").strip("/")
        self.team_id = cfg.get("team_id")
        self.host = cfg.get("cookie_host") or "app.slack.com"
        if not cfg.get("login_url"):
            self.login_url = (f"https://{self.workspace}/" if self.workspace else
                              f"https://app.slack.com/client/{self.team_id}" if self.team_id else
                              "https://app.slack.com/")
        self.base_url = API
        self._token = None
        self._users = {}
        self._lock = threading.Lock()
        self._token_file = cache_path("slack", name, "token")
        self._channels_file = cache_path("slack", name, "channels.json")

    def matches_url(self, url):
        return bool(self.workspace) and util_host(url).lower() == self.workspace.lower()

    def describe(self):
        d = super().describe()
        d.update(workspace=self.workspace or None, team_id=self.team_id, base_url=None)
        return d

    # -- auth -----------------------------------------------------------------
    def _post(self, method, token, params):
        body = {"token": token}
        body.update({k: v for k, v in params.items() if v is not None})
        r = http("POST", f"{API}/{method}",
                 {"Content-Type": "application/x-www-form-urlencoded", "Cookie": self._cookie,
                  "User-Agent": "chrome-session-mcp"},
                 urllib.parse.urlencode(body).encode(), timeout=30)
        if r.status == 429:
            raise ToolError(f"Slack rate-limited {method}; retry after "
                            f"{r.headers.get('Retry-After', 'a few')} s.")
        return r.json()

    def _matches(self, res):
        if self.team_id:
            return res.get("team_id") == self.team_id
        if self.workspace:
            return self.workspace.lower() in (res.get("url") or "").lower()
        return True

    def reload(self):
        self._cookie = chrome.cookie_header(self.host, only=("d", "d-s"))
        self._token = None
        if "d=" not in self._cookie.split("; d-s=")[0]:
            self._cookie = ""
            return
        cached = None
        try:
            with open(self._token_file) as fh:
                cached = fh.read().strip() or None
        except OSError:
            pass
        for cand in [cached] + _scan_tokens():
            if not cand:
                continue
            try:
                res = self._post("auth.test", cand, {})
            except ToolError:
                continue
            if res.get("ok") and self._matches(res):
                self._token = cand
                write_private(self._token_file, cand)
                return

    def probe(self):
        return bool(self._cookie and self._token)

    def _call(self, method, params):
        if self._cookie is None:
            self.reload()
        if not self._cookie:
            raise AuthExpired(self.name, "no Slack 'd' session cookie in Chrome")
        if not self._token:
            raise AuthExpired(self.name, "no xoxc- token for this workspace in Chrome localStorage")
        res = self._post(method, self._token, params)
        if not res.get("ok") and res.get("error") in AUTH_ERRORS:
            self._token = self._cookie = None
            raise AuthExpired(self.name, res.get("error"))
        return res

    def api(self, method, params, write=False):
        allowed = READ_METHODS | (WRITE_METHODS if write else set())
        if method not in allowed:
            raise ToolError(f"refused: '{method}' is not an allowed Slack method here.")
        res = with_login(self, lambda: self._call(method, params))
        if not res.get("ok"):
            raise ToolError(f"Slack {method}: {res.get('error')}")
        return res

    # -- helpers --------------------------------------------------------------
    def user_name(self, uid):
        if not uid:
            return uid
        if uid not in self._users:
            try:
                u = self.api("users.info", {"user": uid})["user"]
                self._users[uid] = (u.get("profile", {}).get("display_name")
                                    or u.get("real_name") or u.get("name") or uid)
            except ToolError:
                self._users[uid] = uid
        return self._users[uid]

    def channel_index(self, force=False):
        with self._lock:
            if not force:
                try:
                    if time.time() - os.stat(self._channels_file).st_mtime < CHANNELS_TTL:
                        with open(self._channels_file) as fh:
                            return json.load(fh)
                except (OSError, ValueError):
                    pass
            chans, cursor = [], None
            while True:
                res = self.api("conversations.list", {"types": "public_channel,private_channel",
                                                      "limit": 1000, "exclude_archived": "false",
                                                      "cursor": cursor})
                for c in res.get("channels", []):
                    chans.append({"id": c.get("id"), "name": c.get("name"),
                                  "is_private": c.get("is_private"), "is_archived": c.get("is_archived"),
                                  "is_member": c.get("is_member"), "num_members": c.get("num_members"),
                                  "topic": (c.get("topic") or {}).get("value")})
                cursor = (res.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
                time.sleep(1.2)  # stay under the Tier-2 rate limit while paging
            write_private(self._channels_file, json.dumps(chans))
            return chans

    def resolve_channel(self, name):
        if re.fullmatch(r"[CGD][A-Z0-9]{6,}", name):
            return name
        n = name.lstrip("#")
        for force in (False, True):  # a miss may be a new channel: refresh once
            for c in self.channel_index(force=force):
                if c.get("name") == n:
                    return c["id"]
        raise ToolError(f"channel '#{n}' not found (or you are not a member). Pass the channel ID instead.")

    def simplify(self, m, names=True):
        out = {"ts": m.get("ts"), "time": fmt_ts(m.get("ts")),
               "user": m.get("user") or m.get("bot_id") or m.get("username"), "text": m.get("text")}
        if names and m.get("user"):
            out["user_name"] = self.user_name(m["user"])
        for k in ("thread_ts", "reply_count", "subtype"):
            if m.get(k):
                out[k] = m[k]
        if m.get("files"):
            out["files"] = [f.get("name") or f.get("title") for f in m["files"]]
        if m.get("attachments"):
            out["attachments"] = [a.get("fallback") or a.get("text") for a in m["attachments"]]
        return out


def slack_ts(v):
    """unix ts | 'now-2h' | ISO date/datetime (local time) -> unix ts string."""
    if v in (None, ""):
        return None
    s = str(v).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return s
    m = re.fullmatch(r"now-(\d+)([smhd])", s)
    if m:
        return str(time.time() - int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)])
    try:
        return str(datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        raise ToolError(f"unrecognised time '{v}': use a unix ts, 'now-2h', or '2026-10-01T09:00'.")


def parse_permalink(url):
    """https://x.slack.com/archives/C123/p1727851234123456?thread_ts=... -> (channel, ts)."""
    m = re.search(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})", url)
    if not m:
        raise ToolError(f"not a Slack message link: {url}")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return m.group(1), q.get("thread_ts", [f"{m.group(2)}.{m.group(3)}"])[0]


# ----------------------------------------------------------------------- tools
@tool("slack_channels", "List Slack channels",
      "List Slack channels (cached for 24 h). Use `grep` to filter by name.",
      {"grep": S("Regex on the channel name."),
       "limit": I("Max channels (default 50).", default=50),
       "include_archived": B("Include archived channels."),
       "refresh": B("Rebuild the channel cache (slow on a big workspace).")},
      type="slack")
def slack_channels(s, grep=None, limit=50, include_archived=False, refresh=False):
    chans = s.channel_index(force=refresh)
    if not include_archived:
        chans = [c for c in chans if not c.get("is_archived")]
    if grep:
        rx = re.compile(grep, re.I)
        chans = [c for c in chans if c.get("name") and rx.search(c["name"])]
    return fit(dumps({"total": len(chans), "channels": chans[:limit] if limit else chans}), "slack-channels")


@tool("slack_history", "Read a Slack channel",
      "Messages of a channel, oldest first. Messages with replies carry reply_count; read them with slack_thread.",
      {"channel": S("#name or channel ID (C.../G.../D...)."),
       "limit": I("Max messages (default 50).", default=50),
       "oldest": S("Lower bound: unix ts, 'now-6h', or ISO time like '2026-10-01T09:00' (local)."),
       "latest": S("Upper bound, same formats."),
       "resolve_names": B("Resolve user IDs to display names.", default=True)},
      required=["channel"], type="slack")
def slack_history(s, channel, limit=50, oldest=None, latest=None, resolve_names=True):
    cid = s.resolve_channel(channel)
    msgs, cursor = [], None
    while True:
        res = s.api("conversations.history", {
            "channel": cid, "limit": min(limit - len(msgs), 200) if limit else 200,
            "oldest": slack_ts(oldest), "latest": slack_ts(latest), "cursor": cursor})
        msgs += res.get("messages", [])
        cursor = (res.get("response_metadata") or {}).get("next_cursor")
        if not res.get("has_more") or not cursor or (limit and len(msgs) >= limit):
            break
        time.sleep(1.2)
    msgs = list(reversed(msgs[:limit] if limit else msgs))
    return fit(dumps({"channel": cid, "count": len(msgs),
                      "messages": [s.simplify(m, resolve_names) for m in msgs]}), "slack-history")


@tool("slack_thread", "Read a Slack thread",
      "A thread: the parent message and its replies. Pass a message link (`url`), or `channel` + `ts`.",
      {"url": S("Slack message link, e.g. https://acme.slack.com/archives/C.../p1727851234123456."),
       "channel": S("#name or channel ID."),
       "ts": S("Parent message ts, e.g. 1727851234.123456."),
       "limit": I("Max messages (default 200).", default=200),
       "resolve_names": B("Resolve user IDs to display names.", default=True)},
      type="slack")
def slack_thread(s, url=None, channel=None, ts=None, limit=200, resolve_names=True):
    if url:
        channel, ts = parse_permalink(url)
    if not channel or not ts:
        raise ToolError("pass url, or both channel and ts.")
    cid = s.resolve_channel(channel)
    msgs = s.api("conversations.replies", {"channel": cid, "ts": ts, "limit": limit}).get("messages", [])
    return fit(dumps({"channel": cid, "thread_ts": ts, "count": len(msgs),
                      "messages": [s.simplify(m, resolve_names) for m in msgs]}), "slack-thread")


@tool("slack_search", "Search Slack",
      "Search messages with Slack search syntax, e.g. 'in:#incidents timeout after:2026-09-01 from:@name'.",
      {"query": S("Slack search query."),
       "count": I("Max results (default 20, max 100).", default=20),
       "sort": S("timestamp or score.", enum=["timestamp", "score"], default="timestamp"),
       "sort_dir": S("desc or asc.", enum=["desc", "asc"], default="desc")},
      required=["query"], type="slack")
def slack_search(s, query, count=20, sort="timestamp", sort_dir="desc"):
    res = s.api("search.messages", {"query": query, "count": min(count, 100), "sort": sort,
                                    "sort_dir": sort_dir})
    msgs = res.get("messages", {})
    matches = [{"time": fmt_ts(m.get("ts")), "ts": m.get("ts"),
                "channel": (m.get("channel") or {}).get("name"),
                "channel_id": (m.get("channel") or {}).get("id"),
                "user": m.get("username"), "text": m.get("text"), "permalink": m.get("permalink")}
               for m in msgs.get("matches", [])]
    return fit(dumps({"total": msgs.get("total"), "matches": matches}), "slack-search")


@tool("slack_user", "Look up a Slack user", "Profile of a Slack user by ID or email.",
      {"id": S("User ID (U...)."), "email": S("Email address.")}, type="slack")
def slack_user(s, id=None, email=None):
    if email:
        u = s.api("users.lookupByEmail", {"email": email})["user"]
    elif id:
        u = s.api("users.info", {"user": id})["user"]
    else:
        raise ToolError("pass id or email.")
    p = u.get("profile", {})
    return dumps({"id": u.get("id"), "name": u.get("name"), "real_name": u.get("real_name"),
                  "display_name": p.get("display_name"), "email": p.get("email"),
                  "title": p.get("title"), "tz": u.get("tz"), "is_bot": u.get("is_bot"),
                  "deleted": u.get("deleted")})


@tool("slack_post", "Post to Slack",
      "Post a message as the user, to a channel or as a thread reply. Slack mrkdwn is supported. "
      "Only available on connectors with allow_write. Read the thread first so you do not repeat "
      "a message that already went out.",
      {"channel": S("#name or channel ID."),
       "text": S("Message text (Slack mrkdwn)."),
       "thread_ts": S("Reply in this thread (parent message ts)."),
       "url": S("Reply in the thread of this message link instead of channel + thread_ts.")},
      required=["text"], type="slack", write=True)
def slack_post(s, text, channel=None, thread_ts=None, url=None):
    if url:
        channel, thread_ts = parse_permalink(url)
    if not channel:
        raise ToolError("pass channel (or a message url to reply in its thread).")
    cid = s.resolve_channel(channel)
    res = s.api("chat.postMessage", {"channel": cid, "text": text, "thread_ts": thread_ts}, write=True)
    return dumps({"ok": True, "channel": res.get("channel"), "ts": res.get("ts"),
                  "thread_ts": thread_ts})

"""Grafana: dashboards, panel queries, and the HTTP API, read-only.

Config:  {"type": "grafana", "base_url": "https://grafana.example.com"}

Grafana rotates its own session cookie (grafana_session). On HTTP 401 the
connector first asks Grafana to rotate the token (POST
/api/user/auth-tokens/rotate) and retries; only if that fails is the session
treated as expired (re-read Chrome, then open it for a login).
"""
import json
import re
import time
import urllib.parse

from ..registry import B, Connector, I, S, connector_type, tool
from ..util import AuthExpired, ToolError, cache_path, dumps, fit, write_private

ROTATE_PATH = "/api/user/auth-tokens/rotate"


def _merge_cookie(cookie, set_cookies):
    jar = {}
    for part in cookie.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            jar[k] = v
    changed = False
    for h in set_cookies:
        first = h.split(";")[0].strip()
        if "=" in first:
            k, v = first.split("=", 1)
            if v and v not in ("deleted", '""'):
                jar[k.strip()] = v
                changed = True
    return "; ".join(f"{k}={v}" for k, v in jar.items()), changed


@connector_type("grafana")
class Grafana(Connector):
    probe_path = "/api/user"

    def __init__(self, name, cfg):
        super().__init__(name, cfg)
        self._datasources = None

    def _rotate(self):
        from ..util import http
        r = http("POST", self.base_url + ROTATE_PATH,
                 {"Cookie": self.cookie(), "Accept": "application/json"}, b"")
        if r.status >= 400:
            return False
        cookie, changed = _merge_cookie(self._cookie, r.headers.get_all("Set-Cookie") or [])
        if changed:
            self._cookie = cookie
        return changed

    def request(self, method, path, headers=None, data=None, json_body=None, timeout=120):
        try:
            return super().request(method, path, headers, data, json_body, timeout)
        except AuthExpired as e:
            # A stale grafana_session can be renewed without a browser login.
            if "401" in e.detail and self._rotate():
                return super().request(method, path, headers, data, json_body, timeout)
            raise

    # -- dashboards -----------------------------------------------------------
    def load_dashboard(self, url_or_uid):
        ref = url_or_uid
        if url_or_uid.startswith("http") and "/goto/" in url_or_uid:
            ref = self.call("GET", url_or_uid).url
        uid = uid_from(ref)
        if not uid:
            raise ToolError(f"no dashboard uid in '{url_or_uid}'. Pass a /d/<uid>/... URL, a /goto/ "
                            "link, or the uid.")
        db = self.get_json(f"/api/dashboards/uid/{uid}").get("dashboard", {})
        return uid, ref, db

    def datasources(self):
        if self._datasources is None:
            by_uid, by_name = {}, {}
            for d in self.get_json("/api/datasources"):
                e = {"uid": d.get("uid"), "type": d.get("type")}
                by_uid[d.get("uid")] = e
                by_name[d.get("name")] = e
            self._datasources = (by_uid, by_name)
        return self._datasources


def uid_from(url_or_uid):
    if not url_or_uid.startswith("http"):
        return url_or_uid
    m = re.search(r"/d/([^/?]+)", url_or_uid)
    return m.group(1) if m else None


def parse_ts(v, default_ms):
    """epoch-ms, 'now', or 'now-1h' / 'now-30m' / 'now-2d' -> epoch-ms string."""
    if v is None or v == "":
        return str(default_ms)
    v = str(v).strip()
    if v.isdigit():
        return v
    if v == "now":
        return str(int(time.time() * 1000))
    m = re.fullmatch(r"now-(\d+)([smhd])", v)
    if m:
        secs = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        return str(int((time.time() - secs) * 1000))
    return v


def interpolate(text, variables):
    if not isinstance(text, str):
        return text
    for name, val in variables.items():
        if isinstance(val, list):
            val = ",".join(str(x) for x in val)
        val = str(val)
        text = text.replace("${%s}" % name, val).replace("[[%s]]" % name, val)
        text = re.sub(r"\$%s\b" % re.escape(name), lambda _m: val, text)
    return text


def interpolate_obj(obj, variables):
    if isinstance(obj, str):
        return interpolate(obj, variables)
    if isinstance(obj, list):
        return [interpolate_obj(x, variables) for x in obj]
    if isinstance(obj, dict):
        return {k: interpolate_obj(v, variables) for k, v in obj.items()}
    return obj


def var_current(v):
    val = (v.get("current") or {}).get("value")
    if isinstance(val, list):
        return val[0] if len(val) == 1 else val
    return val


def iter_panels(panels):
    for p in panels:
        if p.get("type") == "row":
            yield from iter_panels(p.get("panels", []))
        else:
            yield p


def _ds_label(panel):
    ds = panel.get("datasource")
    if isinstance(ds, dict):
        return ds.get("uid") or ds.get("type") or "?"
    return ds or "default"


def _resolve_ds(ds, by_uid, by_name, variables):
    if isinstance(ds, dict):
        uid = ds.get("uid")
        if uid and uid.startswith("$"):
            uid = interpolate(uid, variables)
        if uid in by_uid:
            return by_uid[uid]
        if uid in by_name:
            return by_name[uid]
        if uid and ds.get("type"):
            return {"uid": uid, "type": ds["type"]}
        return ds
    if isinstance(ds, str):
        name = interpolate(ds, variables)
        return by_name.get(name) or by_uid.get(name) or {"uid": name}
    return None


def summarize(resp):
    results = []
    for ref in sorted(resp.get("results", {})):
        res = resp["results"][ref]
        entry = {"refId": ref}
        if res.get("error"):
            entry["error"] = res["error"]
            results.append(entry)
            continue
        series = []
        for fr in res.get("frames", []):
            fields = fr.get("schema", {}).get("fields", [])
            values = fr.get("data", {}).get("values", [])
            if not fields:
                continue
            vi = len(fields) - 1
            labels = fields[vi].get("labels") or {}
            label = ", ".join(f"{k}={v}" for k, v in labels.items()) or fields[vi].get("name") or ref
            nums = [x for x in (values[vi] if len(values) > vi else []) if isinstance(x, (int, float))]
            if nums:
                series.append({"series": label, "last": nums[-1], "min": min(nums), "max": max(nums),
                               "avg": round(sum(nums) / len(nums), 6), "points": len(nums)})
            else:
                series.append({"series": label, "points": 0})
        entry["series"] = series
        results.append(entry)
    return results


# ----------------------------------------------------------------------- tools
@tool("grafana_resolve", "Resolve a Grafana link",
      "Follow a Grafana /goto/ short link and return the final dashboard URL, its uid, and its "
      "var-*/from/to parameters.",
      {"url": S("Grafana URL or /goto/ short link.")}, required=["url"], type="grafana")
def grafana_resolve(g, url):
    final = g.call("GET", url).url
    q = urllib.parse.parse_qs(urllib.parse.urlparse(final).query)
    return dumps({"final_url": final, "uid": uid_from(final),
                  "params": {k: v if len(v) > 1 else v[0] for k, v in q.items()
                             if k.startswith("var-") or k in ("from", "to", "orgId")}})


@tool("grafana_dashboard", "Describe a Grafana dashboard",
      "A dashboard's title, template variables (with current values), and panels with their index. "
      "The full dashboard JSON is saved to a file. Use a panel's index or title with grafana_run_panel.",
      {"url": S("Dashboard URL, /goto/ short link, or uid.")}, required=["url"], type="grafana")
def grafana_dashboard(g, url):
    uid, ref, db = g.load_dashboard(url)
    panels, idx = [], 0
    for p in db.get("panels", []):
        group = [p] if p.get("type") != "row" else p.get("panels", [])
        row = p.get("title") if p.get("type") == "row" else None
        for sp in group:
            panels.append({"index": idx, "title": sp.get("title"), "type": sp.get("type"),
                           "datasource": _ds_label(sp), **({"row": row} if row else {})})
            idx += 1
    dump = cache_path("grafana", f"{g.name}-{uid}.json")
    write_private(dump, json.dumps(db, indent=2))
    return fit(dumps({
        "title": db.get("title"), "uid": uid, "instance": g.name, "url": ref, "tags": db.get("tags"),
        "variables": [{"name": v.get("name"), "current": var_current(v), "type": v.get("type"),
                       "label": v.get("label")} for v in db.get("templating", {}).get("list", [])],
        "panels": panels, "dashboard_json_file": dump,
    }), "grafana-dashboard")


@tool("grafana_run_panel", "Run a Grafana panel",
      "Execute a panel's queries and return each series' last/min/max/avg. Template variables come "
      "from the dashboard, then the URL's var-* parameters, then `vars`.",
      {"url": S("Dashboard URL, /goto/ short link, or uid."),
       "panel": S("Panel index (from grafana_dashboard) or a title substring."),
       "from": S("Start: epoch-ms, 'now', or 'now-1h' / 'now-30m' / 'now-2d' (default now-1h)."),
       "to": S("End: epoch-ms or 'now' (default now)."),
       "vars": {"type": "object", "additionalProperties": {"type": "string"},
                "description": 'Template variable overrides, e.g. {"service": "checkout"}.'},
       "max_points": I("Max data points per series (default 300).", default=300),
       "raw": B("Return the raw /api/ds/query response instead of the summary.")},
      required=["url", "panel"], type="grafana")
def grafana_run_panel(g, url, panel, vars=None, max_points=300, raw=False, **kw):
    uid, ref, db = g.load_dashboard(url)
    panels = list(iter_panels(db.get("panels", [])))
    key = str(panel)
    chosen = panels[int(key)] if key.isdigit() and int(key) < len(panels) else None
    if chosen is None:
        matches = [p for p in panels if key.lower() in (p.get("title") or "").lower()]
        if len(matches) > 1:
            exact = [p for p in matches if (p.get("title") or "").lower() == key.lower()]
            if len(exact) != 1:
                raise ToolError("panel matched several: " + "; ".join(repr(p.get("title")) for p in matches))
            matches = exact
        chosen = matches[0] if matches else None
    if chosen is None:
        raise ToolError(f"no panel matched '{panel}'. Call grafana_dashboard to list panels.")

    variables = {v.get("name"): var_current(v) for v in db.get("templating", {}).get("list", [])}
    if ref.startswith("http"):
        for k, vals in urllib.parse.parse_qs(urllib.parse.urlparse(ref).query).items():
            if k.startswith("var-"):
                variables[k[4:]] = vals if len(vals) > 1 else vals[0]
    variables.update(vars or {})
    # Grafana expands "All" server-side; raw targets need it done here. `.*` is safe
    # in the regex contexts these variables appear in (InfluxQL =~ /.../, PromQL =~"...").
    for k, v in list(variables.items()):
        if v == "$__all" or (isinstance(v, list) and "$__all" in v):
            variables[k] = ".*"

    by_uid, by_name = g.datasources()
    f = parse_ts(kw.get("from"), int((time.time() - 3600) * 1000))
    t = parse_ts(kw.get("to"), int(time.time() * 1000))
    queries = []
    for i, tgt in enumerate(chosen.get("targets", [])):
        if tgt.get("hide"):
            continue
        q = interpolate_obj(tgt, variables)
        ds = _resolve_ds(tgt.get("datasource") or chosen.get("datasource"), by_uid, by_name, variables)
        if ds:
            q["datasource"] = ds
        q["refId"] = tgt.get("refId") or chr(ord("A") + i)
        q.setdefault("intervalMs", 60000)
        q.setdefault("maxDataPoints", max_points)
        queries.append(q)
    if not queries:
        raise ToolError("the panel has no runnable targets.")

    resp = g.call("POST", "/api/ds/query", json_body={"queries": queries, "from": f, "to": t},
                  timeout=120).json()
    out = {"panel": chosen.get("title"), "dashboard": db.get("title"), "window_ms": [f, t],
           "queries": [{"refId": q["refId"], "expr": str(q.get("expr") or q.get("target")
                                                        or q.get("rawSql") or q.get("query"))[:500]}
                       for q in queries]}
    if raw:
        out["raw"] = resp
    else:
        out["results"] = summarize(resp)
    return fit(dumps(out), "grafana-panel")


@tool("grafana_get", "Grafana API GET",
      "GET any Grafana HTTP API path, e.g. /api/search?query=checkout or /api/datasources.",
      {"path": S("API path starting with /api/.")}, required=["path"], type="grafana")
def grafana_get(g, path):
    if not path.startswith("/api/"):
        raise ToolError("path must start with /api/")
    r = g.call("GET", path)
    try:
        return fit(dumps(r.json()), "grafana-get")
    except ToolError:
        return fit(r.text(), "grafana-get")

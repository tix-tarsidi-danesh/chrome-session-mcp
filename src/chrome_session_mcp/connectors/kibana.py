"""Kibana log search, read-only.

Config:  {"type": "kibana", "base_url": "https://kibana.example.com"}

Searches go through Kibana's own async search API (/internal/search/ese), which
works for users who can open Discover even when the Dev Tools console proxy is
blocked. Tested against Kibana 7.17.
"""
import json
import re
import time
import urllib.parse

from ..registry import B, Connector, I, S, connector_type, tool
from ..util import ToolError, dumps, fit


@connector_type("kibana")
class Kibana(Connector):
    probe_path = "/api/status"
    login_path = "/app/discover"

    def request(self, method, path, headers=None, data=None, json_body=None, timeout=90):
        return super().request(method, path, {"kbn-xsrf": "true", **(headers or {})}, data, json_body, timeout)

    def search(self, index, es_body):
        resp = self.call("POST", "/internal/search/ese",
                         json_body={"params": {"index": index, "body": es_body}}).json()
        sid, tries = resp.get("id"), 0
        while (resp.get("isRunning") or resp.get("isPartial")) and tries < 120:
            time.sleep(1)
            tries += 1
            resp = self.call("POST", "/internal/search/ese/" + urllib.parse.quote(sid), json_body={}).json()
        return resp.get("rawResponse", {})

    def resolve_index(self, index):
        """An index-pattern id (uuid) -> its title; a title passes through."""
        if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{20,}", index):
            title = self.get_json("/api/saved_objects/index-pattern/" + index).get("attributes", {}).get("title")
            if not title:
                raise ToolError(f"index-pattern {index} has no title")
            return title
        return index


def build_query(time_field, frm=None, to=None, q=None, phrase=None, field="message", dsl=None):
    filt = []
    if frm or to:
        filt.append({"range": {time_field: {k: v for k, v in (("gte", frm), ("lte", to)) if v}}})
    if dsl:
        filt.append(json.loads(dsl) if isinstance(dsl, str) else dsl)
    if q:
        filt.append({"query_string": {"query": q}})
    if phrase:
        filt.append({"match_phrase": {field: phrase}})
    return {"bool": {"filter": filt}} if filt else {"match_all": {}}


def extract_message(src):
    m = src.get("message")
    if isinstance(m, str) and m.lstrip().startswith("{"):
        try:
            inner = json.loads(m)
            return inner.get("message") or inner.get("msg") or m
        except ValueError:
            return m
    if m:
        return m if isinstance(m, str) else json.dumps(m)
    return src.get("msg") or src.get("raw") or ""


def _total(raw):
    t = raw.get("hits", {}).get("total")
    return t.get("value") if isinstance(t, dict) else t


QUERY_PROPS = {
    "index": S("Index-pattern title (e.g. 'logs-checkout*') or its saved-object id. Find it with kibana_indices."),
    "from": S("Start: ISO-8601 in UTC (e.g. 2026-10-02T03:00:00.000Z) or ES date math like 'now-1h'."),
    "to": S("End, same formats."),
    "query": S("Lucene query_string, e.g. 'level:ERROR AND message:*timeout*'."),
    "phrase": S("Exact phrase to match on `field`."),
    "field": S("Field for `phrase` (default message).", default="message"),
    "dsl": {"type": "object", "description": "Raw Elasticsearch query clause, ANDed into the filter."},
    "time_field": S("Time field (default @timestamp).", default="@timestamp"),
}


@tool("kibana_resolve", "Resolve a Kibana link",
      "Resolve a Kibana /goto/ short link to its index pattern, KQL query, and time range.",
      {"url": S("Kibana /goto/<id> link or id.")}, required=["url"], type="kibana")
def kibana_resolve(k, url):
    m = re.search(r"/goto/([0-9a-f-]+)", url)
    sid = m.group(1) if m else url.split("?")[0].rstrip("/").split("/")[-1]
    obj = k.get_json("/api/short_url/" + sid)
    dec = urllib.parse.unquote(obj.get("locator", {}).get("state", {}).get("url", "")
                               or (obj.get("attributes") or {}).get("url", ""))
    idx = re.search(r"index:'([^']+)'", dec)
    tfrom = re.search(r"from:'?([^',)]+)", dec)
    tto = re.search(r"to:'?([^',)]+)", dec)
    q = (re.search(r"language:kuery,query:'(.*)'\),sort", dec, re.DOTALL)
         or re.search(r"query:'(.*?)'\)", dec, re.DOTALL))
    out = {"slug": obj.get("slug"), "index_id": idx.group(1) if idx else None,
           "from": tfrom.group(1) if tfrom else None, "to": tto.group(1) if tto else None,
           "kql": q.group(1) if q else None, "url": dec}
    if idx:
        try:
            out["index_title"] = k.resolve_index(idx.group(1))
        except ToolError:
            pass
    return dumps(out)


@tool("kibana_indices", "List Kibana index patterns",
      "List index patterns (title + id), optionally filtered by a substring such as a service name.",
      {"filter": S("Case-insensitive substring of the title.")}, type="kibana")
def kibana_indices(k, filter=None):
    d = k.get_json("/api/saved_objects/_find?type=index-pattern&per_page=10000&fields=title")
    rows = sorted(({"title": o["attributes"].get("title", ""), "id": o["id"]}
                   for o in d.get("saved_objects", [])), key=lambda r: r["title"])
    if filter:
        rows = [r for r in rows if filter.lower() in r["title"].lower()]
    return fit(dumps({"count": len(rows), "index_patterns": rows}), "kibana-indices")


@tool("kibana_search", "Search logs in Kibana",
      "Search logs and return timestamp + message per hit (or full documents with fields / raw).",
      dict(QUERY_PROPS, **{
          "size": I("Max hits (default 20).", default=20),
          "order": S("Sort by time: asc or desc (default desc, newest first).", enum=["asc", "desc"], default="desc"),
          "fields": {"type": "array", "items": {"type": "string"},
                     "description": "Return only these _source fields (implies full documents)."},
          "width": I("Truncate each message to this many chars (default 500).", default=500),
          "raw": B("Return full _source documents instead of timestamp + message.")}),
      required=["index"], type="kibana")
def kibana_search(k, index, query=None, phrase=None, field="message", dsl=None, time_field="@timestamp",
                  size=20, order="desc", fields=None, width=500, raw=False, **kw):
    title = k.resolve_index(index)
    es = {"size": size, "sort": [{time_field: order}],
          "query": build_query(time_field, kw.get("from"), kw.get("to"), query, phrase, field, dsl)}
    if fields:
        es["_source"] = fields
    res = k.search(title, es)
    hits = res.get("hits", {}).get("hits", [])
    if raw or fields:
        docs = [h.get("_source", {}) for h in hits]
    else:
        docs = [{"ts": h.get("_source", {}).get(time_field),
                 "message": extract_message(h.get("_source", {}))[:width]} for h in hits]
    return fit(dumps({"index": title, "total": _total(res), "returned": len(docs), "hits": docs}), "kibana-search")


@tool("kibana_histogram", "Count logs over time",
      "Count matching log lines per time bucket (date_histogram), to see when an error started or spiked.",
      dict(QUERY_PROPS, **{
          "interval": S("Bucket size, e.g. 5m, 1h (default 1h).", default="1h"),
          "include_empty": B("Include empty buckets.")}),
      required=["index"], type="kibana")
def kibana_histogram(k, index, query=None, phrase=None, field="message", dsl=None, time_field="@timestamp",
                     interval="1h", include_empty=False, **kw):
    title = k.resolve_index(index)
    es = {"size": 0,
          "query": build_query(time_field, kw.get("from"), kw.get("to"), query, phrase, field, dsl),
          "aggs": {"h": {"date_histogram": {"field": time_field, "fixed_interval": interval,
                                            "min_doc_count": 0 if include_empty else 1}}}}
    res = k.search(title, es)
    buckets = [{"time": b["key_as_string"], "count": b["doc_count"]}
               for b in res.get("aggregations", {}).get("h", {}).get("buckets", [])]
    return fit(dumps({"index": title, "total": _total(res), "buckets": buckets}), "kibana-histogram")

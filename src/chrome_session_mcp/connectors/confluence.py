"""Confluence (Cloud, or Server / Data Center with api_path) through your Chrome session.

Config:  {"type": "confluence", "base_url": "https://acme.atlassian.net"}
         "api_path" defaults to /wiki/rest/api (Cloud); Server/DC is usually /rest/api.
         add "allow_write": true to enable update / create / upload / appearance.

Page bodies are Confluence *storage format* (XHTML), never markdown: a markdown
round-trip turns every embedded image into a temporary blob: URL and silently
breaks it. Bodies move through local files (out_file / body_file), not tool
arguments: arguments past ~130k characters abort, and a long page's storage
body is often 250k+. Every update backs up the current body first and refuses
a write that would lose images, macros, or inline-comment anchors.
"""
import json
import os
import re
import time
import uuid

from ..registry import B, Connector, I, S, connector_type, tool
from ..util import AuthExpired, ToolError, cache_path, dumps, fit, resolve_path, write_private

# Things a bad write destroys silently. An update may not lower any of these
# counts unless the caller says so explicitly.
GUARDED = {
    "images (ri:attachment)": "<ri:attachment",
    "images (ac:image)": "<ac:image",
    "macros": "<ac:structured-macro",
    "inline comment anchors": "<ac:inline-comment-marker",
}


def integrity(body):
    return {name: body.count(tag) for name, tag in GUARDED.items()}


def check_update(cur_body, new_body, allow_shrink=False, allow_loss=False):
    """Problems that should stop an update ([] = safe)."""
    if not new_body.strip():
        return ["the new body is empty"]
    problems = []
    if not allow_shrink and len(cur_body) > 2000 and len(new_body) < 0.5 * len(cur_body):
        problems.append(f"body shrinks from {len(cur_body):,} to {len(new_body):,} chars (more than 50%)")
    before, after = integrity(cur_body), integrity(new_body)
    if not allow_loss:
        problems += [f"{k}: {before[k]} -> {after[k]}" for k in GUARDED if after[k] < before[k]]
    return problems


@connector_type("confluence")
class Confluence(Connector):
    options = ("api_path",)
    login_path = "/wiki/"

    def __init__(self, name, cfg):
        super().__init__(name, cfg)
        self.api = cfg.get("api_path") or "/wiki/rest/api"
        if not cfg.get("login_url") and cfg.get("api_path"):
            self.login_url = self.base_url + "/"
        self.probe_path = self.api + "/user/current"

    def _whoami(self):
        r = self.request("GET", self.probe_path)
        if r.status != 200:
            return None
        try:
            me = json.loads(r.body)
        except ValueError:
            return None
        # An expired Cloud session still answers 200, as the anonymous user.
        if me.get("type") == "anonymous" or not (me.get("accountId") or me.get("username")):
            return None
        return me

    def probe(self):
        try:
            return self._whoami() is not None
        except (AuthExpired, ToolError):
            return False

    def is_expired(self, r):
        if super().is_expired(r):
            return True
        is_html = r.body[:200].lower().lstrip().startswith(b"<")
        return r.status == 403 or (r.status == 200 and is_html and "/rest/" in r.url)

    def request(self, method, path, headers=None, data=None, json_body=None, timeout=180):
        try:
            return super().request(method, path, {"X-Atlassian-Token": "no-check", **(headers or {})},
                                   data, json_body, timeout)
        except AuthExpired:
            # A 403 can also be a real permission error: only treat it as an expired
            # session when we are anonymous.
            if path != self.probe_path:
                try:
                    r = super().request("GET", self.probe_path)
                    me = json.loads(r.body) if r.status == 200 else {}
                    if me.get("accountId") or me.get("username"):
                        raise ToolError(f"{self.name}: {method} {path} was refused (permission denied).")
                except (ValueError, AuthExpired):
                    pass
            raise

    def page_url(self, out):
        links = out.get("_links", {})
        return (links.get("base") or self.base_url + "/wiki") + (links.get("webui") or "")


def page_id(v):
    s = str(v).strip()
    m = re.search(r"/pages/(\d+)", s) or re.search(r"pageId=(\d+)", s)
    if m:
        return m.group(1)
    if s.isdigit():
        return s
    raise ToolError(f"cannot find a page id in '{v}': pass the number or a .../pages/<id>/... URL.")


def _load_body(body, body_file):
    if body_file:
        p = resolve_path(body_file)
        try:
            with open(p, encoding="utf-8") as fh:
                return fh.read()
        except OSError as e:
            raise ToolError(f"cannot read body_file {p}: {e}")
    if body is None:
        raise ToolError("pass body_file (preferred for anything large) or body.")
    return body


def _multipart(path, filename, comment=None):
    boundary = "----chrome-session-mcp" + uuid.uuid4().hex
    with open(path, "rb") as f:
        content = f.read()
    parts = [f"--{boundary}\r\n".encode(),
             (f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
              "Content-Type: application/octet-stream\r\n\r\n").encode(),
             content, b"\r\n",
             f"--{boundary}\r\n".encode(),
             b'Content-Disposition: form-data; name="minorEdit"\r\n\r\ntrue\r\n']
    if comment:
        parts += [f"--{boundary}\r\n".encode(),
                  ('Content-Disposition: form-data; name="comment"\r\n\r\n' + comment + "\r\n").encode()]
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def _attachments(c, pid):
    res, start = [], 0
    while True:
        page = c.get_json(f"{c.api}/content/{pid}/child/attachment?limit=100&start={start}&expand=version")
        for r in page.get("results", []):
            res.append({"id": r.get("id"), "title": r.get("title"),
                        "media_type": (r.get("metadata") or {}).get("mediaType"),
                        "size": (r.get("extensions") or {}).get("fileSize"),
                        "version": (r.get("version") or {}).get("number")})
        if page.get("size", 0) < 100:
            return res
        start += 100


# ----------------------------------------------------------------------- tools
PAGE = S("Page id or page URL (.../pages/<id>/...).")


@tool("confluence_get_page", "Read a Confluence page",
      "Fetch a page's body to a local file and return its title, version, size, and integrity counts "
      "(images, macros, inline-comment anchors). format=storage (default) is the editable XHTML; "
      "format=view is rendered HTML for checking a result. The body is returned inline only with "
      "include_body and when small, so large pages never pass through the context window.",
      {"page": PAGE,
       "version": I("Read an older version (for restoring)."),
       "format": S("storage or view.", enum=["storage", "view"], default="storage"),
       "out_file": S("Where to write the body (default under ~/.cache/chrome-session-mcp/confluence/)."),
       "include_body": B("Also return the body inline when it is under 20k chars.")},
      required=["page"], type="confluence")
def confluence_get_page(c, page, version=None, format="storage", out_file=None, include_body=False):
    pid = page_id(page)
    rep = "storage" if format == "storage" else "view"
    path = f"{c.api}/content/{pid}?expand=body.{rep},version,space,ancestors"
    if version:
        path += f"&version={int(version)}"
    p = c.get_json(path)
    body = p["body"][rep]["value"]
    ver = p["version"]["number"]
    target = resolve_path(out_file) or cache_path("confluence", f"{c.name}-{pid}-v{ver}.{'xml' if rep == 'storage' else 'html'}")
    write_private(target, body)
    out = {"id": p.get("id"), "title": p.get("title"), "version": ver,
           "space": (p.get("space") or {}).get("key"),
           "parent_id": (p.get("ancestors") or [{}])[-1].get("id") if p.get("ancestors") else None,
           "url": c.page_url(p), "format": rep, "chars": len(body),
           "integrity": integrity(body) if rep == "storage" else None, "file": target,
           "note": ("Edit the file, then call confluence_update_page with body_file and "
                    f"expected_version={ver}. Keep the integrity counts unless you mean to remove those elements.")}
    if include_body:
        if len(body) <= 20000:
            out["body"] = body
        else:
            out["body_omitted"] = f"{len(body):,} chars; read the file instead."
    return dumps(out)


@tool("confluence_update_page", "Update a Confluence page",
      "Replace a page's whole body with storage-format XHTML from body_file (use this for anything large) "
      "or body. It refuses when the page moved past expected_version, when the body would shrink by more "
      "than half, or when images / macros / inline-comment anchors would be lost. The previous body is "
      "saved to a backup file first, and its path is returned so the change can be undone.",
      {"page": PAGE,
       "body_file": S("Path to the new storage-format body (preferred)."),
       "body": S("The new body inline. Only for small pages; past ~100k chars use body_file."),
       "expected_version": I("Version you read and edited (from confluence_get_page). Strongly recommended."),
       "title": S("New title (default: keep)."),
       "message": S("Version comment shown in page history."),
       "minor_edit": B("Mark as a minor edit (no watcher notifications)."),
       "allow_shrink": B("Allow the body to shrink by more than 50%."),
       "allow_loss": B("Allow fewer images / macros / inline-comment anchors than before.")},
      required=["page"], type="confluence", write=True, destructive=True)
def confluence_update_page(c, page, body_file=None, body=None, expected_version=None, title=None,
                           message=None, minor_edit=False, allow_shrink=False, allow_loss=False):
    pid = page_id(page)
    new_body = _load_body(body, body_file)
    cur = c.get_json(f"{c.api}/content/{pid}?expand=body.storage,version")
    cur_ver = cur["version"]["number"]
    cur_body = cur["body"]["storage"]["value"]
    if expected_version is not None and int(expected_version) != cur_ver:
        raise ToolError(f"refused: the page is at version {cur_ver}, not {expected_version}. Someone "
                        "edited it since you read it. Re-read with confluence_get_page and redo the edit.")
    problems = check_update(cur_body, new_body, allow_shrink, allow_loss)
    if problems:
        raise ToolError("refused, the update would destroy content: " + "; ".join(problems)
                        + ". If that is intended, pass allow_shrink / allow_loss = true.")
    backup = cache_path("confluence", "backup", f"{c.name}-{pid}-v{cur_ver}-{time.strftime('%Y%m%d-%H%M%S')}.xml")
    write_private(backup, cur_body)
    out = c.call("PUT", f"{c.api}/content/{pid}", json_body={
        "id": str(pid), "type": cur.get("type", "page"), "status": "current", "title": title or cur["title"],
        "version": {"number": cur_ver + 1, "message": message or "Edited via chrome-session-mcp",
                    "minorEdit": bool(minor_edit)},
        "body": {"storage": {"value": new_body, "representation": "storage"}}}).json()
    stored = c.get_json(f"{c.api}/content/{pid}?expand=body.storage,version")
    stored_body = stored["body"]["storage"]["value"]
    return dumps({"ok": True, "id": out.get("id"), "title": out.get("title"),
                  "version": stored["version"]["number"], "previous_version": cur_ver,
                  "url": c.page_url(out), "chars_sent": len(new_body), "chars_stored": len(stored_body),
                  "integrity_before": integrity(cur_body), "integrity_after": integrity(stored_body),
                  "backup_of_previous_body": backup,
                  "restore_hint": f"To undo, call confluence_update_page with body_file={backup}."})


@tool("confluence_create_page", "Create a Confluence page",
      "Create a page from storage-format XHTML (body_file preferred for anything large).",
      {"space_key": S("Space key."), "title": S("Page title."),
       "parent": S("Parent page id or URL (optional)."),
       "body_file": S("Path to the storage-format body."), "body": S("The body inline (small pages only).")},
      required=["space_key", "title"], type="confluence", write=True)
def confluence_create_page(c, space_key, title, parent=None, body_file=None, body=None):
    payload = {"type": "page", "status": "current", "title": title, "space": {"key": space_key},
               "body": {"storage": {"value": _load_body(body, body_file), "representation": "storage"}}}
    if parent:
        payload["ancestors"] = [{"id": page_id(parent)}]
    out = c.call("POST", f"{c.api}/content", json_body=payload).json()
    return dumps({"ok": True, "id": out.get("id"), "title": out.get("title"),
                  "version": (out.get("version") or {}).get("number"), "url": c.page_url(out)})


@tool("confluence_list_attachments", "List Confluence attachments",
      "List a page's attachments (name, id, type, size, version).",
      {"page": PAGE}, required=["page"], type="confluence")
def confluence_list_attachments(c, page):
    return fit(dumps(_attachments(c, page_id(page))), "confluence-attachments")


@tool("confluence_upload_attachment", "Upload a Confluence attachment",
      "Upload a local file (e.g. a rendered diagram PNG) to a page, replacing an attachment with the same "
      "name. Returns the <ac:image> snippet that embeds it in the storage body.",
      {"page": PAGE, "file_path": S("Local file to upload."),
       "name": S("Attachment name (default: the file's name)."), "comment": S("Attachment comment.")},
      required=["page", "file_path"], type="confluence", write=True)
def confluence_upload_attachment(c, page, file_path, name=None, comment=None):
    pid = page_id(page)
    p = resolve_path(file_path)
    if not os.path.isfile(p):
        raise ToolError(f"file not found: {p}")
    name = name or os.path.basename(p)
    existing = {a["title"]: a["id"] for a in _attachments(c, pid)}
    data, boundary = _multipart(p, name, comment)
    hdr = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if name in existing:
        path, action = f"{c.api}/content/{pid}/child/attachment/{existing[name]}/data", "updated"
    else:
        path, action = f"{c.api}/content/{pid}/child/attachment", "created"
    c.call("POST", path, headers=hdr, data=data)
    return dumps({"ok": True, "action": action, "name": name, "bytes": os.path.getsize(p),
                  "embed": f'<ac:image ac:align="center"><ri:attachment ri:filename="{name}"/></ac:image>'})


@tool("confluence_set_appearance", "Set Confluence page width",
      "Switch a page between full-width and fixed-width layout (Confluence Cloud).",
      {"page": PAGE, "mode": S("full-width or fixed.", enum=["full-width", "fixed"])},
      required=["page", "mode"], type="confluence", write=True)
def confluence_set_appearance(c, page, mode):
    pid = page_id(page)
    done = {}
    for key in ("content-appearance-published", "content-appearance-draft"):
        try:
            cur = c.get_json(f"{c.api}/content/{pid}/property/{key}")
            out = c.call("PUT", f"{c.api}/content/{pid}/property/{key}",
                         json_body={"key": key, "value": mode,
                                    "version": {"number": cur["version"]["number"] + 1}}).json()
        except ToolError as e:
            if "HTTP 404" not in str(e):
                raise
            out = c.call("POST", f"{c.api}/content/{pid}/property",
                         json_body={"key": key, "value": mode}).json()
        done[key] = out.get("value")
    return dumps({"ok": True, "page_id": pid, "appearance": done})

"""chrome-session-mcp — MCP server (stdio) for web apps you are logged in to in Chrome.

Standard library only (Python 3.9+). The MCP stdio transport is newline-delimited
JSON-RPC 2.0; the handful of methods it needs (initialize, tools/list,
tools/call, ping) are implemented here directly, so there is nothing to install.

Run:   chrome-session-mcp            (after pip/pipx/uvx install)
       python3 -m chrome_session_mcp (from a checkout: PYTHONPATH=src)
"""
import json
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import __version__
from . import builtins  # noqa: F401  (first, so the generic tools list first)
from . import connectors  # noqa: F401  (importing registers the built-in types)
from .config import RUNTIME
from .registry import TOOLS
from .util import ToolError, log

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = """\
Tools over web apps the user is logged in to in Google Chrome. Each tool authenticates with the
user's own Chrome cookies; no API tokens. Call list_connectors to see the configured connectors
and pass a connector name as `instance` when several of one type exist.

If a session has expired, the tool opens Chrome on the login page and waits (a few minutes) for
the user to log in, then retries. If it still fails, ask the user to finish logging in and retry.
Tools are read-only unless the connector has allow_write; write tools ask before each call.
To show the user a page, use open_in_browser: it opens in their Chrome, already logged in.
Large results are written to a file under ~/.cache/chrome-session-mcp/ and the path is returned."""

_out_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=4)


def send(msg):
    line = json.dumps(msg, ensure_ascii=False)
    with _out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    send(msg)


# ---------------------------------------------------------------- tool list
def visible_tools():
    """Tools whose type is configured (write tools: with allow_write), in registration order."""
    out = []
    for t in TOOLS.values():
        if t.type is None or RUNTIME.of_type(t.type, t.write):
            out.append(t)
    return out


def schema(t):
    props = dict(t.props)
    required = list(t.required)
    if t.type is not None:
        names = [c.name for c in RUNTIME.of_type(t.type, t.write)]
        props = {"instance": {"type": "string", "enum": names,
                              "description": f"Which {t.type} connector to use"
                              + (" (optional: only one is configured)." if len(names) == 1 else
                                 "; a URL argument also picks it by host." if "url" in t.props else ".")},
                 **props}
        if len(names) > 1 and "url" not in t.props:
            required.insert(0, "instance")
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def describe(t):
    return {
        "name": t.name, "title": t.title, "description": t.description, "inputSchema": schema(t),
        "annotations": {"title": t.title, "readOnlyHint": not t.write,
                        "destructiveHint": bool(t.destructive), "openWorldHint": True},
    }


# ---------------------------------------------------------------- dispatch
def run_tool(name, args):
    t = TOOLS.get(name)
    if t is None or t not in visible_tools():
        raise LookupError(name)
    s = schema(t)
    missing = [k for k in s["required"] if args.get(k) in (None, "")]
    if missing:
        raise ToolError(f"missing required argument(s): {', '.join(missing)}")
    unknown = [k for k in args if k not in s["properties"]]
    if unknown:
        raise ToolError(f"unknown argument(s): {', '.join(unknown)}. Allowed: {', '.join(s['properties'])}")
    if t.type is None:
        return t.handler(**args)
    args = dict(args)
    conn = RUNTIME.resolve(t.type, args.pop("instance", None), t.write, args.get("url"))
    if t.write:
        conn.require_write()
    return t.handler(conn, **args)


def call_tool(mid, params):
    name = params.get("name")
    try:
        text = run_tool(name, params.get("arguments") or {})
        reply(mid, {"content": [{"type": "text", "text": text}], "isError": False})
    except LookupError:
        reply(mid, error={"code": -32602, "message": f"unknown tool: {name}"})
    except ToolError as e:
        reply(mid, {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True})
    except Exception as e:  # a bug in a tool must not take the server down
        log(f"tool {name} crashed:\n{traceback.format_exc()}")
        reply(mid, {"content": [{"type": "text", "text": f"error: {type(e).__name__}: {e}"}],
                    "isError": True})


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}
    if mid is None:  # a notification (initialized, cancelled, ...) needs no reply
        return
    if method == "initialize":
        want = params.get("protocolVersion")
        reply(mid, {
            "protocolVersion": want if want in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "chrome-session-mcp", "version": __version__},
            "instructions": INSTRUCTIONS,
        })
    elif method == "ping":
        reply(mid, {})
    elif method == "tools/list":
        reply(mid, {"tools": [describe(t) for t in visible_tools()]})
    elif method == "tools/call":
        # A worker thread, so a tool waiting for a browser login does not block pings.
        _pool.submit(call_tool, mid, params)
    elif method in ("resources/list", "prompts/list"):
        reply(mid, {method.split("/")[0]: []})
    else:
        reply(mid, error={"code": -32601, "message": f"method not found: {method}"})


def main():
    RUNTIME.load()
    log(f"v{__version__} ready: config {RUNTIME.path} "
        f"({len(RUNTIME.instances)} connector(s), {len(RUNTIME.errors)} error(s)), "
        f"{len(visible_tools())} tools, python {sys.version.split()[0]}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        for m in (msg if isinstance(msg, list) else [msg]):
            try:
                handle(m)
            except Exception:
                log(traceback.format_exc())
                if isinstance(m, dict) and m.get("id") is not None:
                    reply(m["id"], error={"code": -32603, "message": "internal error"})
    _pool.shutdown(wait=True)


if __name__ == "__main__":
    main()

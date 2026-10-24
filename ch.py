"""Minimal READ-ONLY ClickHouse client over the MCP gateway Claude Code already uses.

Credentials are read from ~/.claude.json at call time and never copied or stored by this app.
Only SELECT/WITH statements are accepted.
"""
import json
import os
import re
import urllib.error
import urllib.request

CONFIG_PATH = os.path.expanduser("~/.claude.json")


class CHError(Exception):
    pass


def _load():
    try:
        with open(CONFIG_PATH) as f:
            s = json.load(f)["mcpServers"]["clickhouse-mcp"]
        return s["url"], s["headers"]["x-api-key"]
    except (OSError, KeyError, ValueError) as e:
        raise CHError(f"ClickHouse gateway config not found in ~/.claude.json ({e.__class__.__name__})")


class Client:
    def __init__(self):
        self.url, self.key = _load()
        self.session = None
        self._init()

    def _post(self, payload, with_session=True):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "x-api-key": self.key}
        if with_session and self.session:
            h["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=h)
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                text, headers = r.read().decode().strip(), r.headers
        except urllib.error.HTTPError as e:
            raise CHError(f"gateway HTTP {e.code}: {e.read().decode()[:200]}")
        except urllib.error.URLError as e:
            raise CHError(f"gateway unreachable: {e.reason}")
        if text.startswith("event:"):
            text = next((ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")), text)
        body = json.loads(text)
        if "error" in body:
            raise CHError(str(body["error"])[:300])
        return body.get("result"), headers

    def _init(self):
        _, h = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "partner-crm", "version": "1.0"}}}, with_session=False)
        self.session = h.get("Mcp-Session-Id") or h.get("mcp-session-id")
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:  # noqa
            pass

    def query(self, sql):
        if not re.match(r"\s*(select|with)\b", sql, re.I) or re.search(r";\s*\S", sql):
            raise CHError("read-only: single SELECT/WITH only")
        for attempt in (0, 1):
            try:
                res, _ = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "query_events_sql", "arguments": {"sql": sql}}})
                break
            except CHError as e:
                if attempt == 0 and "ession" in str(e):
                    self._init()
                    continue
                raise
        if res.get("isError"):
            raise CHError(str(res)[:300])
        parsed = json.loads(res["content"][0]["text"])
        if isinstance(parsed, dict) and "result" in parsed and "columns" not in parsed:
            parsed = json.loads(parsed["result"])
        if isinstance(parsed, dict) and "error" in parsed and "columns" not in parsed:
            raise CHError(str(parsed["error"])[:300])
        return [dict(zip(parsed["columns"], r)) for r in parsed["rows"]]

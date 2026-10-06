"""Offline DOM replay of a Playwright trace (how bench failures are root-caused; ARCHITECTURE.md 4g-4i).

Lists the recorded calls with their results, rebuilds a frame snapshot as HTML, serves the recorded
resources, and runs JS (e.g. Radar's own FORM_STATE_JS) against it in Chromium. Page scripts are NOT
replayed: you see the DOM exactly as it was at that call.
LIMIT: Playwright snapshots contain no <script> elements at all, so JSON-LD product data is invisible in a
replay. For what the page's data said, read the RESULTS of Radar's own calls (the `after` event of each
call). bench 4's plum diagnosis missed a Product ld+json this way; bench 7 caught it (ARCHITECTURE.md 4l). Needs Playwright (sandbox; not the Mac shell).

    from tools.trace_replay import Trace, run_js
    t = Trace("data/sites/<store>/runs/<run>/<case>.a1_trace.zip")
    for call_id, method, params in t.actions(): ...
    snap = [s for s in t.all if s.get("callId") == "call@677" and s.get("isMainFrame")][-1]
    run_js(t, snap, "() => document.title")
"""
import json, sys, zipfile, html as H, re, threading, http.server, socketserver
from urllib.parse import urlparse

class Trace:
    def __init__(self, path):
        self.z = zipfile.ZipFile(path)
        self.events = [json.loads(l) for l in self.z.read("trace.trace").decode().split("\n") if l.strip()]
        self.net = [json.loads(l) for l in self.z.read("trace.network").decode().split("\n") if l.strip()]
        self.snaps = {}  # frameId -> list
        self.all = []
        for e in self.events:
            if e.get("type") == "frame-snapshot":
                s = e["snapshot"]; lst = self.snaps.setdefault(s["frameId"], [])
                s["_i"] = len(lst); lst.append(s); self.all.append(s)

    def actions(self):
        out = []
        for e in self.events:
            if e.get("type") == "before":
                out.append((e.get("callId"), e.get("method") or e.get("apiName"), json.dumps(e.get("params"))[:160]))
        return out

    def _nodes(self, s):
        if "_nodes" not in s:
            nodes = []
            def visit(n):
                if isinstance(n, str): nodes.append(n)
                elif isinstance(n, list) and n and isinstance(n[0], str):
                    for c in n[2:]: visit(c)
                    nodes.append(n)
            visit(s["html"]); s["_nodes"] = nodes
        return s["_nodes"]

    def render(self, s):
        lst = self.snaps[s["frameId"]]
        out = []
        def visit(n, si, ptag=None):
            if isinstance(n, str):
                out.append(n if ptag in ("STYLE", "style") else H.escape(n, quote=False)); return
            if isinstance(n, list) and n and isinstance(n[0], list):
                off, idx = n[0]; ri = si - off
                if 0 <= ri < len(lst):
                    nodes = self._nodes(lst[ri])
                    if 0 <= idx < len(nodes): visit(nodes[idx], ri, ptag)
                return
            name, attrs = n[0], (n[1] if len(n) > 1 and isinstance(n[1], dict) else {})
            if name.upper() in ("SCRIPT", "NOSCRIPT"): return
            if name == "IFRAME" or name == "FRAME": out.append("<iframe></iframe>"); return
            out.append("<" + name)
            for k, v in attrs.items():
                if k.startswith("__playwright"):
                    if k == "__playwright_value_": out.append(f' value="{H.escape(str(v))}"')
                    if k == "__playwright_checked_" and v == "true": out.append(" checked")
                    if k == "__playwright_selected_" and v == "true": out.append(" selected")
                    continue
                if k in ("value", "checked", "selected") and any(a.startswith("__playwright_" + k) for a in attrs): continue
                out.append(f' {k}="{H.escape(str(v))}"')
            out.append(">")
            for c in n[2:]: visit(c, si, name)
            if name.upper() not in ("BR", "IMG", "INPUT", "META", "LINK", "HR", "SOURCE", "WBR", "AREA", "BASE", "COL", "EMBED", "PARAM", "TRACK"):
                out.append(f"</{name}>")
        visit(s["html"], s["_i"])
        return (f"<!DOCTYPE {s.get('doctype') or 'html'}>" if s.get("doctype") else "") + "".join(out)

    def resource(self, url):
        best = None
        for e in self.net:
            sn = e.get("snapshot", {})
            if sn.get("request", {}).get("url") == url:
                c = sn.get("response", {}).get("content", {})
                if c.get("_sha1"): best = (c["_sha1"], c.get("mimeType", ""))
        if best:
            try: return self.z.read("resources/" + best[0]), best[1]
            except KeyError: return None
        return None


def run_js(trace, snap, js, arg=None, url=None):
    """Load snapshot html at its own URL in Chromium (requests answered from trace), evaluate js."""
    from playwright.sync_api import sync_playwright
    page_html = trace.render(snap)
    url = url or snap["frameUrl"]
    with sync_playwright() as p:
        b = p.chromium.launch(); ctx = b.new_context(viewport=snap.get("viewport") or {"width": 1366, "height": 850}, java_script_enabled=True)
        pg = ctx.new_page()
        def handle(route):
            u = route.request.url
            if u == url and route.request.resource_type == "document":
                return route.fulfill(status=200, body=page_html, content_type="text/html")
            if route.request.resource_type in ("script",):
                return route.fulfill(status=200, body="", content_type="text/javascript")
            r = trace.resource(u)
            if r: return route.fulfill(status=200, body=r[0], content_type=r[1] or None)
            return route.fulfill(status=404, body="")
        pg.route("**/*", handle)
        pg.goto(url, wait_until="load", timeout=60000)
        res = pg.evaluate(js, arg) if arg is not None else pg.evaluate(js)
        b.close()
        return res

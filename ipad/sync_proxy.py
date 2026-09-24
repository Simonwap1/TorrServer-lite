# -*- coding: utf-8 -*-
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json, urllib.parse, urllib.request, ssl

SYNC = "https://sync.jacred.stream"

def http_get(url, timeout=35):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "vid-sync-proxy"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()

def flatten(raw, year=""):
    out = []
    if not isinstance(raw, list):
        return out
    y = year if year.isdigit() else ""
    for b in raw:
        if not isinstance(b, dict):
            continue
        value = b.get("value") or {}
        if not isinstance(value, dict):
            continue
        for t in value.values():
            if not isinstance(t, dict):
                continue
            if y:
                rel = str(t.get("relased") or "").strip()
                title = str(t.get("title") or "")
                if rel.isdigit() and rel != y and y not in title:
                    continue
            mag = str(t.get("magnet") or "").strip()
            link = str(t.get("url") or "").strip()
            if not mag and not link:
                continue
            out.append({
                "title": str(t.get("title") or t.get("name") or ""),
                "size": int(t.get("size") or 0),
                "sizeName": str(t.get("sizeName") or ""),
                "sid": int(t.get("sid") or 0),
                "pir": int(t.get("pir") or 0),
                "magnet": mag,
                "url": link,
                "tracker": str(t.get("trackerName") or ""),
                "quality": t.get("quality") or 0,
            })
    out.sort(key=lambda x: (-x["sid"], -x["size"]))
    return out[:80]

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path not in ("/catalog/torrents", "/torrents", "/"):
            self.send_error(404)
            return
        qs = urllib.parse.parse_qs(u.query)
        q = (qs.get("q") or qs.get("search") or [""])[0].strip()
        alt = (qs.get("alt") or [""])[0].strip()
        year = (qs.get("year") or [""])[0].strip()
        items = []
        seen = set()
        keys = []
        for key in (q, alt):
            if len(key) < 2:
                continue
            # sync/fdb keys are lowercase-only
            for variant in (key, key.lower(), key.casefold()):
                if variant and variant not in keys:
                    keys.append(variant)
        for key in keys[:6]:
            url = SYNC + "/sync/fdb?" + urllib.parse.urlencode({"key": key, "limit": "40"})
            try:
                raw = json.loads(http_get(url).decode("utf-8") or "[]")
            except Exception:
                continue
            for t in flatten(raw, year):
                uniq = t["magnet"] or t["url"]
                if uniq in seen:
                    continue
                seen.add(uniq)
                items.append(t)
        if not items and year.isdigit():
            for key in keys[:6]:
                url = SYNC + "/sync/fdb?" + urllib.parse.urlencode({"key": key, "limit": "40"})
                try:
                    raw = json.loads(http_get(url).decode("utf-8") or "[]")
                except Exception:
                    continue
                for t in flatten(raw, ""):
                    uniq = t["magnet"] or t["url"]
                    if uniq in seen:
                        continue
                    seen.add(uniq)
                    items.append(t)
            items.sort(key=lambda x: (-x["sid"], -x["size"]))
            items = items[:80]
        body = json.dumps({"ok": True, "results": items, "source": "sync"}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

ThreadingHTTPServer(("0.0.0.0", 9080), H).serve_forever()

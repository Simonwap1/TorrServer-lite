# -*- coding: utf-8 -*-
"""Add a few public Prowlarr indexers (no login) if none configured."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CATALOG = os.path.join(ROOT, "catalog.json")

WANT = ("1337x", "RuTor", "Knaben", "LimeTorrents", "YTS", "Nyaa.si", "MegaPeer", "EZTV")


def load_cfg():
    with open(CATALOG, "r", encoding="utf-8") as f:
        return json.load(f)


def api(base, key, path, method="GET", body=None, timeout=90):
    url = base.rstrip("/") + path
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "X-Api-Key": key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "vid-prowlarr",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw.decode("utf-8") or "null")
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError):
            try:
                raw = e.read()
            except Exception:
                raw = b""
            try:
                err = json.loads(raw.decode("utf-8") or "null")
            except Exception:
                err = raw.decode("utf-8", "ignore")[:300]
            return e.code, err
        return 0, str(e)


def main():
    cfg = load_cfg()
    base = str(cfg.get("prowlarr_url") or "http://127.0.0.1:9696").rstrip("/")
    key = str(cfg.get("prowlarr_apikey") or "").strip()
    if not key:
        print("no_apikey")
        return 1
    st, existing = api(base, key, "/api/v1/indexer")
    if st >= 200 and isinstance(existing, list) and len(existing) > 0:
        print("already_have", len(existing))
        return 0
    st, schemas = api(base, key, "/api/v1/indexer/schema")
    if st < 200 or not isinstance(schemas, list):
        print("schema_fail", st, schemas)
        return 1
    by_name = {}
    for s in schemas:
        if isinstance(s, dict) and s.get("name"):
            by_name[str(s["name"])] = s
    added = 0
    for name in WANT:
        sch = by_name.get(name)
        if not sch:
            print("skip_missing", name)
            continue
        payload = dict(sch)
        payload["enable"] = True
        payload["priority"] = 25
        # appProfileId often required
        if not payload.get("appProfileId"):
            payload["appProfileId"] = 1
        st, res = api(base, key, "/api/v1/indexer", method="POST", body=payload)
        print("add", name, st, (res.get("id") if isinstance(res, dict) else str(res)[:120]))
        if st >= 200 and st < 300:
            added += 1
        # don't abort the whole seed on one slow indexer
        continue
    st, existing = api(base, key, "/api/v1/indexer")
    print("done added=%s total=%s" % (added, len(existing) if isinstance(existing, list) else existing))
    return 0 if added or (isinstance(existing, list) and existing) else 1


if __name__ == "__main__":
    raise SystemExit(main())

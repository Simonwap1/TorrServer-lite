#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Шлюз для iPad: lite UI + прокси TorrServer + HLS H.264 для Safari."""

from __future__ import annotations

import argparse
import http.client
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(ROOT, "index.html")
PLAYER = os.path.join(ROOT, "player.html")
HLS_ROOT = os.path.join(ROOT, "hls_cache")
LOCAL_TORRENTS = os.path.join(os.path.dirname(ROOT), "torrents")
POSTERS_FILE = os.path.join(ROOT, "posters.json")
CATALOG_FILE = os.path.join(ROOT, "catalog.json")
POSTERS_LOCK = threading.Lock()
CATALOG_LOCK = threading.Lock()

def load_catalog_cfg() -> dict:
    with CATALOG_LOCK:
        try:
            if not os.path.isfile(CATALOG_FILE):
                return {}
            with open(CATALOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def http_get_json(url: str, timeout: int = 25, headers: dict | None = None):
    hdrs = {
        "User-Agent": "TorrServer-Lite/1.0",
        "Accept": "application/json",
    }
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = getattr(resp, "status", 200) or 200
            return int(status), raw
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:
            raw = b""
        return e.code, raw
    except Exception as e:
        # retry once with unverified SSL (часто на Win без корневых сертификатов)
        try:
            import ssl

            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read()
                status = getattr(resp, "status", 200) or 200
                return int(status), raw
        except urllib.error.HTTPError as e2:
            try:
                raw = e2.read()
            except Exception:
                raw = b""
            return e2.code, raw
        except Exception as e2:
            return 0, ("%s | ssl-retry: %s" % (e, e2)).encode("utf-8", "ignore")



def tmdb_creds(cfg: dict):
    """Return (api_key_v3, access_token_v4)."""
    key = str(cfg.get("tmdb_api_key") or "").strip()
    token = str(
        cfg.get("tmdb_access_token")
        or cfg.get("tmdb_read_access_token")
        or cfg.get("tmdb_bearer")
        or ""
    ).strip()
    return key, token


def tmdb_api_bases(cfg: dict) -> list:
    bases = []
    custom = str(cfg.get("tmdb_api_base") or "").strip().rstrip("/")
    if custom:
        bases.append(custom)
    extra = cfg.get("tmdb_api_bases")
    if isinstance(extra, list):
        for b in extra:
            b = str(b or "").strip().rstrip("/")
            if b and b not in bases:
                bases.append(b)
    for b in (
        "https://api.themoviedb.org/3",
        "https://api.tmdb.org/3",
    ):
        if b not in bases:
            bases.append(b)
    return bases


def tmdb_get(cfg: dict, path: str, params: dict | None = None):
    """TMDB v3 API: Ключ API (api_key) или Ключ доступа (Bearer). Пробует несколько хостов."""
    key, token = tmdb_creds(cfg)
    if not key and not token:
        return 0, b"no tmdb credentials"
    path = path.lstrip("/")
    base_params = dict(params or {})
    errors = []

    # Сначала короткий Ключ API (v3), потом Bearer — на части сетей Bearer ломается
    modes = []
    if key:
        modes.append(("api_key", key, None))
    if token:
        modes.append(("bearer", None, token))
    if not modes:
        return 0, b"no tmdb credentials"

    for base in tmdb_api_bases(cfg):
        for mode_name, k, tok in modes:
            params = dict(base_params)
            headers = {}
            if tok:
                headers["Authorization"] = "Bearer " + tok
            if k:
                params["api_key"] = k
            url = base.rstrip("/") + "/" + path
            if params:
                url += "?" + urllib.parse.urlencode(params)
            status, raw = http_get_json(url, timeout=20, headers=headers if headers else None)
            if status >= 200 and status < 300:
                return status, raw
            detail = raw.decode("utf-8", "ignore")[:160] if raw else ""
            errors.append("%s/%s -> %s %s" % (base, mode_name, status or "net", detail))
    return 0, ("; ".join(errors) or "tmdb unreachable").encode("utf-8", "ignore")


def catalog_provider(cfg: dict) -> str:
    p = str(cfg.get("catalog_provider") or "").strip().lower()
    if p in ("kinopoisk", "kp", "kino"):
        return "kinopoisk"
    if p == "tmdb":
        return "tmdb"
    if str(cfg.get("kinopoisk_api_key") or "").strip():
        return "kinopoisk"
    if tmdb_creds(cfg)[0] or tmdb_creds(cfg)[1]:
        return "tmdb"
    return "tmdb"


def tmdb_poster_url(cfg: dict, path: str) -> str:
    if not path:
        return ""
    base = str(cfg.get("tmdb_image_base") or "https://image.tmdb.org/t/p/w300").rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def warm_jacred_parsers(base: str) -> None:
    """Kick public JacRed parsers in background (empty DB otherwise)."""
    base = (base or "").rstrip("/")
    if not base:
        return

    def _run() -> None:
        endpoints = (
            "/cron/rutor/parselatest?pages=2",
            "/cron/megapeer/parse?maxpage=2",
            "/cron/bitru/parse?page=1",
            "/cron/knaben/parse",
            "/cron/torrentby/parselatest?pages=2",
        )
        for ep in endpoints:
            try:
                http_get_json(base + ep, timeout=180)
            except Exception as e:
                sys.stderr.write("jacred warm %s: %s\n" % (ep, e))

    threading.Thread(target=_run, name="jacred-warm", daemon=True).start()


def normalize_jacred_items(raw) -> list:
    items = []
    if isinstance(raw, dict):
        raw = raw.get("Results") or raw.get("results") or raw.get("torrents") or []
    if not isinstance(raw, list):
        return items
    for t in raw:
        if not isinstance(t, dict):
            continue
        magnet = str(t.get("magnet") or t.get("MagnetUri") or t.get("magnetUrl") or "").strip()
        link = str(t.get("url") or t.get("Link") or "").strip()
        if not magnet and not link:
            continue
        title = str(t.get("title") or t.get("Title") or t.get("name") or "").strip()
        size = t.get("size")
        if size is None:
            size = t.get("Size")
        size_name = str(t.get("sizeName") or t.get("SizeName") or "").strip()
        sid = t.get("sid")
        if sid is None:
            sid = t.get("Seeders")
        pir = t.get("pir")
        if pir is None:
            pir = t.get("Peers")
        try:
            sid = int(sid or 0)
        except Exception:
            sid = 0
        try:
            pir = int(pir or 0)
        except Exception:
            pir = 0
        try:
            size_n = int(size or 0)
        except Exception:
            size_n = 0
        items.append(
            {
                "title": title,
                "size": size_n,
                "sizeName": size_name,
                "sid": sid,
                "pir": pir,
                "magnet": magnet,
                "url": link,
                "tracker": str(
                    t.get("tracker")
                    or t.get("Tracker")
                    or t.get("trackerName")
                    or ""
                ).strip(),
                "quality": t.get("quality") or 0,
            }
        )
    items.sort(key=lambda x: (-int(x.get("sid") or 0), -int(x.get("size") or 0)))
    return items[:80]


def fmt_size_name(n: int) -> str:
    try:
        n = int(n or 0)
    except Exception:
        return ""
    if n <= 0:
        return ""
    units = ("B", "KB", "MB", "GB", "TB")
    x = float(n)
    for u in units:
        if x < 1024.0 or u == units[-1]:
            if u in ("B", "KB"):
                return "%d %s" % (int(x), u)
            return "%.2f %s" % (x, u)
        x /= 1024.0
    return str(n)


def torrent_dedupe_key(item: dict) -> str:
    mag = str(item.get("magnet") or "")
    m = re.search(r"btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", mag, re.I)
    if m:
        return "ih:" + m.group(1).lower()
    title = re.sub(r"\s+", " ", str(item.get("title") or "").strip().lower())
    return "t:%s|%s" % (title, item.get("size") or 0)


def merge_torrent_items(*groups, limit: int = 100) -> list:
    seen = set()
    out = []
    for group in groups:
        for t in group or []:
            if not isinstance(t, dict):
                continue
            key = torrent_dedupe_key(t)
            if not key or key in seen:
                continue
            if not (t.get("magnet") or t.get("url")):
                continue
            seen.add(key)
            out.append(t)
    out.sort(key=lambda x: (-int(x.get("sid") or 0), -int(x.get("size") or 0)))
    return out[:limit]


def normalize_prowlarr_items(raw, source: str = "prowlarr") -> list:
    items = []
    if isinstance(raw, dict):
        raw = raw.get("Results") or raw.get("results") or []
    if not isinstance(raw, list):
        return items
    for t in raw:
        if not isinstance(t, dict):
            continue
        raw_mag = str(
            t.get("magnetUrl")
            or t.get("MagnetUri")
            or t.get("magnet")
            or ""
        ).strip()
        raw_dl = str(
            t.get("downloadUrl")
            or t.get("guid")
            or t.get("Link")
            or t.get("url")
            or ""
        ).strip()
        magnet = raw_mag if raw_mag.lower().startswith("magnet:") else ""
        link = raw_dl
        if not magnet and raw_mag.lower().startswith("http"):
            link = raw_mag
        if not magnet and not link:
            continue
        title = str(t.get("title") or t.get("Title") or "").strip()
        try:
            size_n = int(t.get("size") or t.get("Size") or 0)
        except Exception:
            size_n = 0
        try:
            sid = int(t.get("seeders") or t.get("Seeders") or 0)
        except Exception:
            sid = 0
        try:
            pir = int(t.get("leechers") or t.get("peers") or t.get("Peers") or 0)
        except Exception:
            pir = 0
        tracker = str(
            t.get("indexer")
            or t.get("Indexer")
            or t.get("tracker")
            or ""
        ).strip()
        items.append(
            {
                "title": title,
                "size": size_n,
                "sizeName": fmt_size_name(size_n),
                "sid": sid,
                "pir": pir,
                "magnet": magnet,
                "url": link,
                "tracker": tracker,
                "quality": 0,
                "source": source,
            }
        )
    items.sort(key=lambda x: (-int(x.get("sid") or 0), -int(x.get("size") or 0)))
    return items[:80]


def search_jacred(cfg: dict, q: str, alt: str = "", year: str = "") -> tuple:
    """Return (items, source_tag). source: local|sync|''."""
    base = str(cfg.get("jacred_url") or "").strip().rstrip("/")
    if not base:
        return [], ""
    params = {"sort": "sid"}
    if q:
        params["search"] = q
    if alt:
        params["altname"] = alt
    if year.isdigit():
        params["relased"] = year
    apikey = str(cfg.get("jacred_apikey") or "").strip()
    if apikey:
        params["apikey"] = apikey
    url = base + "/api/v1.0/torrents?" + urllib.parse.urlencode(params)
    status, raw = http_get_json(url, timeout=40)
    data = None
    if status >= 200 and status < 300:
        try:
            data = json.loads(raw.decode("utf-8") or "[]")
        except Exception:
            data = None
    else:
        params2 = {"Query": q or alt}
        if apikey:
            params2["apikey"] = apikey
        url2 = base + "/api/v2.0/indexers/all/results?" + urllib.parse.urlencode(params2)
        st2, raw2 = http_get_json(url2, timeout=40)
        if st2 >= 200 and st2 < 300:
            try:
                data = json.loads(raw2.decode("utf-8") or "[]")
            except Exception:
                data = None
    items = normalize_jacred_items(data or [])
    for t in items:
        t["source"] = "jacred"
    if not items and year.isdigit() and (q or alt):
        params_ny = dict(params)
        params_ny.pop("relased", None)
        url_ny = base + "/api/v1.0/torrents?" + urllib.parse.urlencode(params_ny)
        st2, raw2 = http_get_json(url_ny, timeout=40)
        if st2 >= 200 and st2 < 300:
            try:
                items = normalize_jacred_items(json.loads(raw2.decode("utf-8") or "[]"))
                for t in items:
                    t["source"] = "jacred"
            except Exception:
                pass
    source = "jacred" if items else ""
    if not items and (q or alt):
        try:
            items = search_jacred_sync_mirror(cfg, q, alt, year)
            for t in items:
                t["source"] = "sync"
            if items:
                source = "sync"
        except Exception as e:
            sys.stderr.write("jacred sync fallback: %s\n" % e)
    if not items:
        try:
            warm_jacred_parsers(base)
        except Exception:
            pass
    return items, source


def search_prowlarr(cfg: dict, q: str, alt: str = "", year: str = "") -> list:
    base = str(cfg.get("prowlarr_url") or "").strip().rstrip("/")
    key = str(cfg.get("prowlarr_apikey") or cfg.get("prowlarr_api_key") or "").strip()
    if not base or not key:
        return []
    queries = []
    for part in (q, alt):
        part = (part or "").strip()
        if len(part) < 2:
            continue
        qq = part
        if year.isdigit() and year not in qq:
            qq = "%s %s" % (qq, year)
        if qq not in queries:
            queries.append(qq)
    if not queries:
        return []
    headers = {"X-Api-Key": key}
    collected = []
    for query in queries[:2]:
        params = {"query": query, "type": "search"}
        url = base + "/api/v1/search?" + urllib.parse.urlencode(params)
        status, raw = http_get_json(url, timeout=55, headers=headers)
        if status < 200 or status >= 300:
            # Jackett-compatible endpoint some setups expose
            params2 = {"Query": query}
            url2 = base + "/api/v1/indexer/all/results?" + urllib.parse.urlencode(params2)
            status, raw = http_get_json(url2, timeout=55, headers=headers)
            if status < 200 or status >= 300:
                sys.stderr.write("prowlarr search HTTP %s\n" % (status or "fail"))
                continue
        try:
            data = json.loads(raw.decode("utf-8") or "[]")
        except Exception:
            continue
        collected.extend(normalize_prowlarr_items(data, source="prowlarr"))
    return merge_torrent_items(collected, limit=80)


def flatten_sync_fdb_buckets(raw, year: str = "") -> list:
    """Turn /sync/fdb?key=… buckets into a flat torrent list."""
    torrents = []
    if not isinstance(raw, list):
        return torrents
    y = year.strip() if year and str(year).isdigit() else ""
    for bucket in raw:
        if not isinstance(bucket, dict):
            continue
        value = bucket.get("value") or bucket.get("Value") or {}
        if not isinstance(value, dict):
            continue
        for t in value.values():
            if not isinstance(t, dict):
                continue
            if y:
                rel = str(t.get("relased") or t.get("released") or "").strip()
                if rel.isdigit() and rel != y:
                    title = str(t.get("title") or "")
                    if y not in title:
                        continue
            torrents.append(t)
    return torrents


def search_jacred_sync_mirror(cfg: dict, q: str, alt: str = "", year: str = "") -> list:
    """Fallback when local JacRed FDB is empty: query public sync mirror by key."""
    sync = str(cfg.get("jacred_sync_url") or "https://sync.jacred.stream").strip().rstrip("/")
    if not sync:
        return []
    keys = []
    for k in (q, alt):
        k = (k or "").strip()
        if len(k) < 2:
            continue
        # sync/fdb matching is lowercase-only
        for variant in (k, k.lower(), k.casefold()):
            if variant and variant not in keys:
                keys.append(variant)
    collected = []
    seen = set()
    for key in keys[:6]:
        url = sync + "/sync/fdb?" + urllib.parse.urlencode({"key": key, "limit": "40"})
        status, raw = http_get_json(url, timeout=35)
        if status < 200 or status >= 300:
            continue
        try:
            data = json.loads(raw.decode("utf-8") or "[]")
        except Exception:
            continue
        for t in flatten_sync_fdb_buckets(data, year=year):
            mag = str(t.get("magnet") or "")
            link = str(t.get("url") or "")
            uniq = mag or link
            if not uniq or uniq in seen:
                continue
            seen.add(uniq)
            collected.append(t)
    items = normalize_jacred_items(collected)
    if not items and year and year.isdigit():
        collected2 = []
        seen2 = set()
        for key in keys[:6]:
            url = sync + "/sync/fdb?" + urllib.parse.urlencode({"key": key, "limit": "40"})
            status, raw = http_get_json(url, timeout=35)
            if status < 200 or status >= 300:
                continue
            try:
                data = json.loads(raw.decode("utf-8") or "[]")
            except Exception:
                continue
            for t in flatten_sync_fdb_buckets(data, year=""):
                mag = str(t.get("magnet") or "")
                link = str(t.get("url") or "")
                uniq = mag or link
                if not uniq or uniq in seen2:
                    continue
                seen2.add(uniq)
                collected2.append(t)
        items = normalize_jacred_items(collected2)
    return items


HOP_BY_HOP = {
    "transfer-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "upgrade",
}

FFMPEG = ""
FFPROBE = ""


def resolve_ffmpeg_tools() -> tuple[str, str]:
    """Ищем ffmpeg/ffprobe: tools → C:\\ffmpeg → PATH."""
    tools_bin = os.path.join(os.path.dirname(ROOT), "tools", "ffmpeg", "bin")
    candidates_ff = [
        os.path.join(tools_bin, "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
        shutil.which("ffmpeg") or "",
    ]
    candidates_fp = [
        os.path.join(tools_bin, "ffprobe.exe"),
        r"C:\ffmpeg\bin\ffprobe.exe",
        shutil.which("ffprobe") or "",
    ]
    ff = next((p for p in candidates_ff if p and os.path.isfile(p)), "")
    fp = next((p for p in candidates_fp if p and os.path.isfile(p)), "")
    if ff and not fp:
        sib = os.path.join(os.path.dirname(ff), "ffprobe.exe")
        if os.path.isfile(sib):
            fp = sib
    return ff or r"C:\ffmpeg\bin\ffmpeg.exe", fp or r"C:\ffmpeg\bin\ffprobe.exe"


FFMPEG, FFPROBE = resolve_ffmpeg_tools()

# sid -> dict(proc, dir, started, last)
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
DURATION_CACHE = {}
DURATION_LOCK = threading.Lock()


def pick_encoder() -> list:
    if not os.path.isfile(FFMPEG):
        return ["libx264", "-preset", "ultrafast", "-tune", "zerolatency"]
    try:
        out = subprocess.check_output(
            [FFMPEG, "-hide_banner", "-encoders"],
            stderr=subprocess.STDOUT,
            timeout=8,
            universal_newlines=True,
            errors="ignore",
        )
    except Exception:
        out = ""
    # Для HLS/iPad надёжнее libx264 baseline; NVENC иногда даёт несовместимый bitstream
    if "libx264" in out:
        return ["libx264", "-preset", "veryfast", "-tune", "zerolatency"]
    if "h264_nvenc" in out:
        return ["h264_nvenc", "-preset", "ll", "-tune", "ll", "-rc", "vbr", "-cq", "28"]
    if "h264_amf" in out:
        return ["h264_amf", "-quality", "speed", "-rc", "vbr_latency"]
    return ["libx264", "-preset", "ultrafast", "-tune", "zerolatency"]


ENCODER = pick_encoder()


def make_sid(hash_: str, index: str, height: str, suffix: str = "", start: int = 0) -> str:
    safe = re.sub(r"[^a-zA-Z0-9]", "", hash_)[:24]
    base = "%s_%s_%s" % (safe, index, height)
    st = int(start or 0)
    if st >= 5:
        # корзина 15 сек — рядом позиции делят кэш
        base = "%s_s%d" % (base, (st // 15) * 15)
    if suffix:
        extra = re.sub(r"[^a-zA-Z0-9]", "", suffix)[:16]
        if extra:
            return base + "_" + extra
    return base


def json_escape(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", "") + '"'


def source_url(backend_host: str, backend_port: int, hash_: str, index: str) -> str:
    q = urllib.parse.urlencode({"link": hash_, "index": index, "play": ""})
    return "http://%s:%d/stream/file?%s" % (backend_host, backend_port, q)


def probe_duration(backend_host: str, backend_port: int, hash_: str, index: str) -> float:
    """Длительность файла (сек) через ffprobe — для полосы перемотки."""
    key = "%s:%s" % (hash_, index)
    with DURATION_LOCK:
        hit = DURATION_CACHE.get(key)
        if hit and (time.time() - hit[1]) < 6 * 3600:
            return float(hit[0])

    if not hash_ or not os.path.isfile(FFPROBE):
        return 0.0

    activate_torrent(backend_host, backend_port, hash_)
    warm_torrent(backend_host, backend_port, hash_, str(index))
    src = source_url(backend_host, backend_port, hash_, str(index))
    cmd = [
        FFPROBE,
        "-v",
        "error",
        "-analyzeduration",
        "15M",
        "-probesize",
        "15M",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        src,
    ]
    dur = 0.0
    try:
        out = subprocess.check_output(
            cmd,
            stderr=subprocess.DEVNULL,
            timeout=45,
            universal_newlines=True,
            errors="ignore",
        )
        dur = float(str(out).strip().splitlines()[0].strip())
    except Exception:
        dur = 0.0
    if dur < 1 or dur > 24 * 3600:
        dur = 0.0
    if dur > 0:
        with DURATION_LOCK:
            DURATION_CACHE[key] = (dur, time.time())
    return dur


def activate_torrent(backend_host: str, backend_port: int, hash_: str) -> None:
    try:
        body = json.dumps({"action": "get", "hash": hash_}).encode("utf-8")
        conn = http.client.HTTPConnection(backend_host, backend_port, timeout=10)
        conn.request(
            "POST",
            "/torrents",
            body=body,
            headers={"Content-Type": "application/json", "Connection": "close"},
        )
        resp = conn.getresponse()
        resp.read()
        conn.close()
    except Exception:
        pass


def preload_torrent(backend_host: str, backend_port: int, hash_: str, index: str) -> None:
    try:
        q = urllib.parse.urlencode({"link": hash_, "index": str(index), "preload": ""})
        conn = http.client.HTTPConnection(backend_host, backend_port, timeout=12)
        conn.request("GET", "/stream/file?" + q, headers={"Connection": "close"})
        resp = conn.getresponse()
        resp.read(512 * 1024)
        conn.close()
    except Exception:
        pass


def torrent_name_hint(backend_host: str, backend_port: int, hash_: str, index: str) -> str:
    try:
        body = json.dumps({"action": "get", "hash": hash_}).encode("utf-8")
        conn = http.client.HTTPConnection(backend_host, backend_port, timeout=12)
        conn.request(
            "POST",
            "/torrents",
            body=body,
            headers={"Content-Type": "application/json", "Connection": "close"},
        )
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        data = json.loads(raw.decode("utf-8") or "{}")
        parts = [str(data.get("title") or ""), str(data.get("name") or "")]
        for f in data.get("file_stats") or []:
            if str(f.get("id")) == str(index) or not parts:
                parts.append(str(f.get("path") or ""))
        if data.get("data"):
            try:
                extra = json.loads(data["data"])
                for f in extra.get("TorrServer", {}).get("Files", []):
                    parts.append(str(f.get("path") or ""))
            except Exception:
                pass
        return " ".join(parts).lower()
    except Exception:
        return ""


def should_copy_h264(hint: str, src: str = "") -> bool:
    """Ремукс (copy), если похоже на H.264. Без ffprobe — он мешает TorrServer."""
    h = (hint or "").lower()
    if any(x in h for x in ("x265", "hevc", "h265", "av1", ".avi")):
        return False
    if any(x in h for x in ("x264", "h264", "avc")):
        return True
    # типичные WEB/BD без hevc в имени чаще всего h264 — copy быстрее realtime
    if any(x in h for x in ("web-dl", "webdl", "webrip", "web-rip", "bdrip", "bluray", "blu-ray", "hdtv")):
        if not any(x in h for x in ("x265", "hevc", "h265")):
            return True
    if h.endswith(".mp4") or ".mp4" in h:
        return True
    return False


def warm_torrent(backend_host: str, backend_port: int, hash_: str, index: str) -> str:
    """Прогрев торрента перед ffmpeg — иначе Error -138 / timeout."""
    activate_torrent(backend_host, backend_port, hash_)
    hint = torrent_name_hint(backend_host, backend_port, hash_, str(index))
    for i in range(3):
        preload_torrent(backend_host, backend_port, hash_, str(index))
        if i < 2:
            time.sleep(0.7 + i * 0.4)
    return hint


def load_posters() -> dict:
    with POSTERS_LOCK:
        try:
            if not os.path.isfile(POSTERS_FILE):
                return {}
            with open(POSTERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def save_posters(data: dict) -> None:
    with POSTERS_LOCK:
        try:
            tmp = POSTERS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, POSTERS_FILE)
        except Exception as e:
            sys.stderr.write("posters save error: %s\n" % e)


def set_local_poster(hash_: str, poster: str) -> None:
    h = (hash_ or "").strip().lower()
    p = (poster or "").strip()
    if not h:
        return
    data = load_posters()
    if p:
        data[h] = p
    elif h in data:
        del data[h]
    save_posters(data)


def clear_local_poster(hash_: str) -> None:
    set_local_poster(hash_, "")


def merge_posters_into_list(raw: bytes, push_backend: bool = False, backend_host: str = "", backend_port: int = 0) -> bytes:
    """Подмешиваем сохранённые постеры в ответ list — TorrServer часто их теряет."""
    try:
        items = json.loads(raw.decode("utf-8") or "[]")
    except Exception:
        return raw
    if not isinstance(items, list):
        return raw
    posters = load_posters()
    if not posters:
        return raw
    changed = False
    for t in items:
        if not isinstance(t, dict):
            continue
        h = str(t.get("hash") or "").strip().lower()
        if not h:
            continue
        local = posters.get(h) or posters.get(h[:40])
        if not local:
            continue
        cur = str(t.get("poster") or "").strip()
        if cur != local:
            t["poster"] = local
            changed = True
            if push_backend and backend_host and backend_port:
                try:
                    apply_poster(backend_host, backend_port, h, local)
                except Exception as e:
                    sys.stderr.write("poster push: %s\n" % e)
    if not changed:
        return raw
    return json.dumps(items, ensure_ascii=False).encode("utf-8")


_poster_sync_stop = threading.Event()


def sync_posters_to_backend(backend_host: str, backend_port: int) -> int:
    """Пишем posters.json в TorrServer, чтобы MatriX (:8092) поле «Ссылка на постер» не было пустым."""
    posters = load_posters()
    if not posters:
        return 0
    status, raw = ts_api(backend_host, backend_port, {"action": "list"})
    items = []
    if status >= 200 and status < 300 and raw:
        try:
            parsed = json.loads(raw.decode("utf-8") or "[]")
            if isinstance(parsed, list):
                items = parsed
        except Exception:
            items = []
    by_hash = {}
    for t in items:
        if isinstance(t, dict) and t.get("hash"):
            by_hash[str(t["hash"]).strip().lower()] = t
    ok = 0
    for h, poster in posters.items():
        h = str(h or "").strip().lower()
        poster = str(poster or "").strip()
        if not h or not poster or len(h) < 32:
            continue
        t = by_hash.get(h)
        if not t:
            continue
        cur = str(t.get("poster") or "").strip()
        if cur == poster:
            continue
        title = str(t.get("title") or "").strip() or str(t.get("name") or "").strip()
        category = str(t.get("category") or "").strip()
        if apply_poster(backend_host, backend_port, h, poster, title=title, category=category):
            ok += 1
        else:
            sys.stderr.write("poster sync fail %s\n" % (h[:12],))
    return ok


def apply_poster(
    backend_host: str,
    backend_port: int,
    hash_: str,
    poster: str,
    title: str = "",
    category: str = "",
) -> bool:
    """Сохраняем постер локально и в TorrServer (MatriX читает poster из API list/get)."""
    h = (hash_ or "").strip()
    p = (poster or "").strip()
    if not h or not p:
        return False
    # MatriX UI требует расширение картинки в URL, иначе очищает поле ввода
    low = p.lower()
    if not any(ext in low for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")):
        # не отбрасываем — всё равно пишем; предупреждение в лог
        sys.stderr.write("poster url without image ext: %s\n" % p[:80])
    set_local_poster(h, p)
    payload = {"action": "set", "hash": h, "poster": p}
    if title:
        payload["title"] = title
    if category:
        payload["category"] = category
    st, _ = ts_api(backend_host, backend_port, payload)
    if st < 200 or st >= 300:
        time.sleep(0.35)
        st, _ = ts_api(backend_host, backend_port, payload)
    if st < 200 or st >= 300:
        return False
    # проверка: MatriX берёт value из list.poster
    st2, raw = ts_api(backend_host, backend_port, {"action": "get", "hash": h})
    if st2 >= 200 and st2 < 300 and raw:
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
            got = str((data or {}).get("poster") or "").strip()
            if got == p:
                return True
        except Exception:
            pass
    # повторная запись если get ещё пустой
    time.sleep(0.2)
    st3, _ = ts_api(backend_host, backend_port, payload)
    return st3 >= 200 and st3 < 300


def start_poster_sync_loop(backend_host: str, backend_port: int, interval: float = 5.0) -> None:
    """Фон: MatriX ходит на :8092 напрямую — постоянно восстанавливаем poster в БД."""

    def loop() -> None:
        while not _poster_sync_stop.wait(interval):
            try:
                sync_posters_to_backend(backend_host, backend_port)
            except Exception as e:
                sys.stderr.write("poster sync loop err: %s\n" % e)

    threading.Thread(target=loop, name="poster-sync", daemon=True).start()


def extract_multipart_field(body: bytes, name: str) -> str:
    if not body or not name:
        return ""
    marker = ('name="%s"' % name).encode("ascii", "ignore")
    i = body.find(marker)
    if i < 0:
        return ""
    i = body.find(b"\r\n\r\n", i)
    if i < 0:
        return ""
    i += 4
    j = body.find(b"\r\n", i)
    if j < 0:
        return ""
    return body[i:j].decode("utf-8", "ignore").strip()


def hash_from_ts_response(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        data = json.loads(raw.decode("utf-8") or "null")
    except Exception:
        return ""
    if isinstance(data, dict):
        h = str(data.get("hash") or "").strip()
        if h:
            return h
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("hash"):
                return str(item.get("hash") or "").strip()
    return ""


def kill_all_sessions() -> None:
    with SESSIONS_LOCK:
        for _sid, info in list(SESSIONS.items()):
            try:
                info["proc"].kill()
            except Exception:
                pass
            try:
                info["log"].close()
            except Exception:
                pass
            SESSIONS.pop(_sid, None)
    # дать TorrServer отпустить старый HTTP-стрим
    time.sleep(0.6)


def dir_size(path: str) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def cleanup_hls_cache(keep_sids: set | None = None, max_bytes: int = 0) -> dict:
    """Автоочистка отключена — кэш чистит пользователь вручную.
    Функция оставлена для совместимости / ручного вызова API.
    max_bytes<=0 — ничего не удаляем.
    """
    return hls_cache_stats()


def hls_cache_stats() -> dict:
    os.makedirs(HLS_ROOT, exist_ok=True)
    total = 0
    folders = 0
    try:
        for name in os.listdir(HLS_ROOT):
            p = os.path.join(HLS_ROOT, name)
            if os.path.isdir(p):
                folders += 1
                total += dir_size(p)
            elif os.path.isfile(p):
                try:
                    total += os.path.getsize(p)
                except OSError:
                    pass
    except OSError:
        pass
    return {
        "ok": True,
        "bytes": total,
        "folders": folders,
        "path": HLS_ROOT,
        "removed": 0,
        "freed": 0,
    }


def purge_hls_cache() -> dict:
    """Глушим ffmpeg-сессии и удаляем весь hls_cache."""
    kill_all_sessions()
    before = hls_cache_stats()
    removed = 0
    os.makedirs(HLS_ROOT, exist_ok=True)
    try:
        names = list(os.listdir(HLS_ROOT))
    except OSError:
        names = []
    for name in names:
        p = os.path.join(HLS_ROOT, name)
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
            elif os.path.isfile(p):
                os.remove(p)
                removed += 1
        except OSError:
            pass
    after = hls_cache_stats()
    freed = max(0, int(before.get("bytes") or 0) - int(after.get("bytes") or 0))
    return {
        "ok": True,
        "removed": removed,
        "freed": freed,
        "bytes": after.get("bytes") or 0,
        "folders": after.get("folders") or 0,
        "bytes_before": before.get("bytes") or 0,
        "path": HLS_ROOT,
    }


def hls_cleanup_loop() -> None:
    # автоочистка выключена
    return


def ensure_hls(
    backend_host: str,
    backend_port: int,
    hash_: str,
    index: str,
    height: int,
    restart: bool = False,
    sid: str | None = None,
    start: int = 0,
) -> dict:
    start = max(0, int(start or 0))
    if not sid:
        sid = make_sid(hash_, str(index), str(height), start=start)

    with SESSIONS_LOCK:
        cur = SESSIONS.get(sid)
        if cur and not restart and cur["proc"].poll() is None:
            cur["last"] = time.time()
            return cur

    kill_all_sessions()

    out_dir = os.path.join(HLS_ROOT, sid)
    bitrates = {360: "700k", 480: "1200k", 720: "2200k"}
    bv = bitrates.get(height, "1200k")
    src = source_url(backend_host, backend_port, hash_, str(index))
    playlist = os.path.join(out_dir, "index.m3u8")
    seg_pattern = os.path.join(out_dir, "seg%03d.ts")
    out_h = height if height in (360, 480, 720) else 480
    level = "4.0" if out_h >= 720 else "3.1"
    profile = "main" if out_h >= 720 else "baseline"

    last_info = None
    for attempt in range(1, 5):
        hint = warm_torrent(backend_host, backend_port, hash_, str(index))
        use_copy = should_copy_h264(hint, src)

        if os.path.isdir(out_dir):
            shutil.rmtree(out_dir, ignore_errors=True)
        os.makedirs(out_dir, exist_ok=True)

        cmd = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+genpts+discardcorrupt+igndts",
            "-analyzeduration",
            "20M",
            "-probesize",
            "20M",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_at_eof",
            "1",
            "-reconnect_delay_max",
            "60",
            "-rw_timeout",
            "60000000",
        ]
        if start >= 5:
            cmd.extend(["-ss", str(start)])
        cmd.extend(
            [
                "-i",
                src,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
            ]
        )
        if use_copy:
            cmd.extend(["-c:v", "copy"])
        else:
            cmd.extend(["-vf", "scale=-2:%d:flags=fast_bilinear" % out_h, "-c:v"])
            cmd.extend(ENCODER)
            cmd.extend(
                [
                    "-profile:v",
                    profile,
                    "-level",
                    level,
                    "-pix_fmt",
                    "yuv420p",
                    "-b:v",
                    bv,
                    "-maxrate",
                    bv,
                    "-bufsize",
                    "4000k",
                    "-g",
                    "50",
                    "-keyint_min",
                    "50",
                    "-sc_threshold",
                    "0",
                ]
            )
        cmd.extend(
            [
                "-af",
                "aresample=async=1:first_pts=0",
                "-c:a",
                "aac",
                "-ac",
                "2",
                "-ar",
                "44100",
                "-b:a",
                "128k",
                "-avoid_negative_ts",
                "make_zero",
                "-max_muxing_queue_size",
                "2048",
                "-f",
                "hls",
                "-hls_time",
                "3",
                "-hls_list_size",
                "0",
                "-hls_playlist_type",
                "event",
                "-start_number",
                "0",
                "-hls_segment_filename",
                seg_pattern,
                playlist,
            ]
        )

        err_path = os.path.join(out_dir, "ffmpeg.log")
        err_log = open(err_path, "wb", buffering=0)
        err_log.write(
            (
                "attempt=%s hint=%s copy=%s start=%s\n"
                % (attempt, (hint[:80] if hint else "?"), use_copy, start)
            ).encode("utf-8")
        )
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=err_log,
            stdin=subprocess.DEVNULL,
        )
        info = {
            "sid": sid,
            "proc": proc,
            "dir": out_dir,
            "playlist": playlist,
            "started": time.time(),
            "last": time.time(),
            "log": err_log,
            "hash": hash_,
            "index": str(index),
            "height": out_h,
            "start": start,
            "copy": use_copy,
            "codec": "h264-copy" if use_copy else "transcode",
            "attempt": attempt,
        }
        with SESSIONS_LOCK:
            SESSIONS[sid] = info
        last_info = info

        # ждём первые куски или быстрый крах (типичный -138)
        ok_early = False
        for _ in range(25):  # ~10 сек
            st = hls_segment_stats(info)
            if st["segments"] >= 1:
                ok_early = True
                break
            if proc.poll() is not None:
                break
            time.sleep(0.4)

        if ok_early or proc.poll() is None:
            # есть сегмент или ffmpeg ещё жив — отдаём клиенту дожидаться need
            return info

        # упал без сегментов — ещё попытка
        try:
            err_log.write(("retry after fail attempt=%s\n" % attempt).encode("utf-8"))
            err_log.close()
        except Exception:
            pass
        with SESSIONS_LOCK:
            SESSIONS.pop(sid, None)
        time.sleep(1.0 + attempt * 0.5)

    return last_info


def hls_segment_stats(info: dict) -> dict:
    segs = 0
    bytes_ = 0
    out_dir = info.get("dir") or ""
    if out_dir and os.path.isdir(out_dir):
        try:
            for name in os.listdir(out_dir):
                if not name.endswith(".ts"):
                    continue
                path = os.path.join(out_dir, name)
                try:
                    sz = os.path.getsize(path)
                except OSError:
                    continue
                if sz > 1000:
                    segs += 1
                    bytes_ += sz
        except OSError:
            pass
    alive = False
    proc = info.get("proc")
    if proc is not None:
        try:
            alive = proc.poll() is None
        except Exception:
            alive = False
    need = 4
    ready = segs >= need
    if ready:
        pct = 100
    else:
        pct = min(95, int(segs * (95.0 / need)) + (3 if segs else 0))
    return {
        "segments": segs,
        "bytes": bytes_,
        "ready": ready,
        "alive": alive,
        "pct": pct,
        "need": need,
    }


def wait_playlist(info: dict, timeout: float = 180.0, min_segments: int = 4) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = hls_segment_stats(info)
        if st["segments"] >= min_segments:
            return True
        if info["proc"].poll() is not None:
            return False
        time.sleep(0.35)
    return False


class Gateway(http.server.BaseHTTPRequestHandler):
    backend_host = "127.0.0.1"
    backend_port = 8092
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html", "/ipad", "/ipad/"):
            self._serve_file(INDEX, "text/html; charset=utf-8")
            return
        if path in ("/matrix", "/matrix/"):
            self._serve_matrix_index()
            return
        if path.startswith("/matrix/"):
            # статика MatriX через тот же 8080
            new_path = path[len("/matrix") :] or "/"
            if parsed.query:
                self.path = new_path + "?" + parsed.query
            else:
                self.path = new_path
            self._proxy()
            return
        if path in ("/player", "/player.html"):
            self._serve_file(PLAYER, "text/html; charset=utf-8")
            return
        if path == "/hls/ready":
            self._hls_ready(qs)
            return
        if path == "/hls/start":
            self._hls_start(qs)
            return
        if path == "/hls/progress":
            self._hls_progress(qs)
            return
        if path == "/hls/stop":
            self._hls_stop(qs)
            return
        if path == "/hls/duration":
            self._hls_duration(qs)
            return
        if path == "/hls/cache":
            self._hls_cache(qs)
            return
        if path == "/hls/cache/clear":
            self._hls_cache_clear()
            return
        if path == "/hls/index.m3u8":
            # совместимость: сразу готовим и отдаём playlist без 302
            self._hls_ready_playlist(qs)
            return
        if path.startswith("/hls/files/"):
            self._hls_file(path)
            return
        if path == "/local-torrents":
            self._list_local_torrents()
            return
        if path == "/catalog/config":
            self._catalog_config()
            return
        if path == "/catalog/search":
            self._catalog_search(qs)
            return
        if path == "/catalog/movie":
            self._catalog_movie(qs)
            return
        if path == "/catalog/torrents":
            self._catalog_torrents(qs)
            return
        if path == "/catalog/tmdb-ping":
            self._catalog_tmdb_ping()
            return
        if path == "/catalog/jacred-warm":
            self._catalog_jacred_warm()
            return
        if path.startswith("/transcode"):
            # Для VLC — MPEG-TS (надёжнее, чем fMP4)
            self._transcode_ts(qs)
            return
        self._proxy()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""

        if path == "/local-torrents/add":
            self._add_local_torrent(body)
            return

        if path == "/torrent/upload":
            self._torrent_upload(body)
            return

        if path in ("/torrents", "/remove"):
            try:
                req = json.loads(body.decode("utf-8") or "{}")
            except Exception:
                req = {}
            action = str(req.get("action") or "")
            hash_ = str(req.get("hash") or "")
            if path == "/remove" or action == "rem":
                ok, msg = force_remove_torrent(self.backend_host, self.backend_port, hash_)
                if ok and hash_:
                    clear_local_poster(hash_)
                out = json.dumps({"ok": ok, "message": msg}).encode("utf-8")
                self.send_response(200 if ok else 409)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(out)))
                self._cors()
                self.end_headers()
                self.wfile.write(out)
                return
            if action == "set":
                self._torrent_set(req)
                return
            if action == "list":
                self._torrent_list()
                return
            if action == "add":
                poster = str(req.get("poster") or "").strip()
                # проксируем add, потом запомним постер по hash из ответа
                self._torrent_add(body, req, poster)
                return
            if action == "wipe":
                stop_sessions_for_hash("")
                try:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/IM", "ffmpeg.exe", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=8,
                        )
                except Exception:
                    pass
                time.sleep(0.5)

        self._proxy_body(body)

    def do_PUT(self) -> None:
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()

    def do_HEAD(self) -> None:
        self.do_GET()

    def _json_response(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _catalog_config(self) -> None:
        cfg = load_catalog_cfg()
        prov = catalog_provider(cfg)
        sources = str(cfg.get("torrent_sources") or "jacred,prowlarr").strip().lower()
        self._json_response(
            {
                "ok": True,
                "provider": prov,
                "tmdb": bool(tmdb_creds(cfg)[0] or tmdb_creds(cfg)[1]),
                "kinopoisk": bool(str(cfg.get("kinopoisk_api_key") or "").strip()),
                "jacred": bool(str(cfg.get("jacred_url") or "").strip()),
                "jacred_url": str(cfg.get("jacred_url") or "").strip(),
                "jacred_auto_install": cfg.get("jacred_auto_install", True) is not False,
                "prowlarr": bool(
                    str(cfg.get("prowlarr_url") or "").strip()
                    and str(cfg.get("prowlarr_apikey") or cfg.get("prowlarr_api_key") or "").strip()
                ),
                "prowlarr_url": str(cfg.get("prowlarr_url") or "").strip(),
                "torrent_sources": sources,
            }
        )

    def _catalog_tmdb_ping(self) -> None:
        cfg = load_catalog_cfg()
        status, raw = tmdb_get(cfg, "configuration", {})
        self._json_response(
            {
                "ok": status >= 200 and status < 300,
                "status": status,
                "detail": raw.decode("utf-8", "ignore")[:400],
            },
            200 if status >= 200 and status < 300 else 502,
        )

    def _catalog_jacred_warm(self) -> None:
        cfg = load_catalog_cfg()
        base = str(cfg.get("jacred_url") or "").strip().rstrip("/")
        if not base:
            self._json_response({"ok": False, "error": "нет jacred_url"}, 400)
            return
        warm_jacred_parsers(base)
        self._json_response(
            {
                "ok": True,
                "message": "Парсеры JacRed запущены в фоне (rutor/megapeer/…). Подождите 5–15 минут и ищите снова.",
            }
        )

    def _catalog_search(self, qs: dict) -> None:
        cfg = load_catalog_cfg()
        q = (qs.get("q") or qs.get("query") or [""])[0].strip()
        if len(q) < 2:
            self._json_response({"ok": False, "error": "Введите название"}, 400)
            return
        prov = catalog_provider(cfg)
        if prov == "kinopoisk":
            self._catalog_search_kinopoisk(cfg, q)
        else:
            self._catalog_search_tmdb(cfg, q)

    def _catalog_search_tmdb(self, cfg: dict, q: str) -> None:
        key, token = tmdb_creds(cfg)
        if not key and not token:
            self._json_response(
                {
                    "ok": False,
                    "error": "В catalog.json нужен tmdb_api_key (Ключ API) или tmdb_access_token (Ключ доступа)",
                },
                400,
            )
            return
        lang = str(cfg.get("tmdb_language") or "ru-RU").strip() or "ru-RU"
        status, raw = tmdb_get(
            cfg,
            "search/movie",
            {"language": lang, "query": q, "include_adult": "false", "page": "1"},
        )
        if status < 200 or status >= 300:
            detail = raw.decode("utf-8", "ignore")[:300]
            msg = "TMDB недоступен с Server-pc"
            if status:
                msg = "TMDB HTTP %s" % status
            self._json_response(
                {
                    "ok": False,
                    "error": msg + " — проверьте интернет/DNS или tmdb_api_base в catalog.json",
                    "detail": detail,
                },
                502,
            )
            return
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            self._json_response({"ok": False, "error": "TMDB bad json"}, 502)
            return
        out = []
        for m in data.get("results") or []:
            if not isinstance(m, dict):
                continue
            mid = m.get("id")
            if not mid:
                continue
            title = str(m.get("title") or m.get("original_title") or "").strip()
            year = ""
            rd = str(m.get("release_date") or "")
            if len(rd) >= 4:
                year = rd[:4]
            out.append(
                {
                    "id": mid,
                    "title": title,
                    "original_title": str(m.get("original_title") or "").strip(),
                    "year": year,
                    "poster": tmdb_poster_url(cfg, str(m.get("poster_path") or "")),
                    "overview": str(m.get("overview") or "").strip(),
                }
            )
        self._json_response({"ok": True, "provider": "tmdb", "results": out})

    def _catalog_search_kinopoisk(self, cfg: dict, q: str) -> None:
        key = str(cfg.get("kinopoisk_api_key") or "").strip()
        if not key:
            self._json_response(
                {
                    "ok": False,
                    "error": "Укажите kinopoisk_api_key в catalog.json (kinopoiskapiunofficial.tech)",
                },
                400,
            )
            return
        url = (
            "https://kinopoiskapiunofficial.tech/api/v2.1/films/search-by-keyword?"
            + urllib.parse.urlencode({"keyword": q, "page": "1"})
        )
        status, raw = http_get_json(url, headers={"X-API-KEY": key})
        if status < 200 or status >= 300:
            self._json_response(
                {
                    "ok": False,
                    "error": "Kinopoisk HTTP %s" % (status or "fail"),
                    "detail": raw.decode("utf-8", "ignore")[:200],
                },
                502,
            )
            return
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            self._json_response({"ok": False, "error": "Kinopoisk bad json"}, 502)
            return
        out = []
        for m in data.get("films") or []:
            if not isinstance(m, dict):
                continue
            mid = m.get("filmId") or m.get("kinopoiskId")
            if not mid:
                continue
            title = str(m.get("nameRu") or m.get("nameEn") or m.get("nameOriginal") or "").strip()
            orig = str(m.get("nameEn") or m.get("nameOriginal") or "").strip()
            year = str(m.get("year") or "").strip()
            if year.lower() in ("null", "none"):
                year = ""
            poster = str(m.get("posterUrlPreview") or m.get("posterUrl") or "").strip()
            overview = str(m.get("description") or "").strip()
            out.append(
                {
                    "id": mid,
                    "title": title,
                    "original_title": orig,
                    "year": year,
                    "poster": poster,
                    "overview": overview,
                }
            )
        self._json_response({"ok": True, "provider": "kinopoisk", "results": out})

    def _catalog_movie(self, qs: dict) -> None:
        cfg = load_catalog_cfg()
        mid = (qs.get("id") or [""])[0].strip()
        if not mid:
            self._json_response({"ok": False, "error": "id required"}, 400)
            return
        prov = catalog_provider(cfg)
        if prov == "kinopoisk":
            self._catalog_movie_kinopoisk(cfg, mid)
        else:
            self._catalog_movie_tmdb(cfg, mid)

    def _catalog_movie_tmdb(self, cfg: dict, mid: str) -> None:
        key, token = tmdb_creds(cfg)
        if not key and not token:
            self._json_response({"ok": False, "error": "Нет tmdb_api_key / tmdb_access_token"}, 400)
            return
        lang = str(cfg.get("tmdb_language") or "ru-RU").strip() or "ru-RU"
        status, raw = tmdb_get(cfg, "movie/%s" % urllib.parse.quote(mid), {"language": lang})
        if status < 200 or status >= 300:
            self._json_response({"ok": False, "error": "TMDB HTTP %s" % (status or "fail")}, 502)
            return
        try:
            m = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            self._json_response({"ok": False, "error": "TMDB bad json"}, 502)
            return
        year = ""
        rd = str(m.get("release_date") or "")
        if len(rd) >= 4:
            year = rd[:4]
        self._json_response(
            {
                "ok": True,
                "provider": "tmdb",
                "movie": {
                    "id": m.get("id"),
                    "title": str(m.get("title") or "").strip(),
                    "original_title": str(m.get("original_title") or "").strip(),
                    "year": year,
                    "poster": tmdb_poster_url(cfg, str(m.get("poster_path") or "")),
                    "overview": str(m.get("overview") or "").strip(),
                },
            }
        )

    def _catalog_movie_kinopoisk(self, cfg: dict, mid: str) -> None:
        key = str(cfg.get("kinopoisk_api_key") or "").strip()
        if not key:
            self._json_response({"ok": False, "error": "Нет kinopoisk_api_key"}, 400)
            return
        url = "https://kinopoiskapiunofficial.tech/api/v2.2/films/%s" % urllib.parse.quote(mid)
        status, raw = http_get_json(url, headers={"X-API-KEY": key})
        if status < 200 or status >= 300:
            self._json_response({"ok": False, "error": "Kinopoisk HTTP %s" % (status or "fail")}, 502)
            return
        try:
            m = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            self._json_response({"ok": False, "error": "Kinopoisk bad json"}, 502)
            return
        year = str(m.get("year") or "").strip()
        if year.lower() in ("null", "none"):
            year = ""
        self._json_response(
            {
                "ok": True,
                "provider": "kinopoisk",
                "movie": {
                    "id": m.get("kinopoiskId") or m.get("filmId") or mid,
                    "title": str(m.get("nameRu") or m.get("nameOriginal") or "").strip(),
                    "original_title": str(m.get("nameOriginal") or m.get("nameEn") or "").strip(),
                    "year": year,
                    "poster": str(m.get("posterUrlPreview") or m.get("posterUrl") or "").strip(),
                    "overview": str(m.get("description") or "").strip(),
                },
            }
        )

    def _catalog_torrents(self, qs: dict) -> None:
        cfg = load_catalog_cfg()
        q = (qs.get("q") or qs.get("search") or [""])[0].strip()
        alt = (qs.get("alt") or qs.get("altname") or [""])[0].strip()
        year = (qs.get("year") or qs.get("relased") or [""])[0].strip()
        if len(q) < 2 and len(alt) < 2:
            self._json_response({"ok": False, "error": "Пустой запрос"}, 400)
            return

        sources_raw = str(
            (qs.get("sources") or [""])[0] or cfg.get("torrent_sources") or "jacred,prowlarr"
        ).strip().lower()
        want_jacred = "jacred" in sources_raw or sources_raw in ("all", "both", "*")
        want_prowlarr = "prowlarr" in sources_raw or sources_raw in ("all", "both", "*")
        if not want_jacred and not want_prowlarr:
            want_jacred = True
            want_prowlarr = True

        jacred_items = []
        prowlarr_items = []
        jacred_src = ""
        errors = []

        def _jacred() -> None:
            nonlocal jacred_items, jacred_src
            try:
                jacred_items, jacred_src = search_jacred(cfg, q, alt, year)
            except Exception as e:
                errors.append("jacred: %s" % e)

        def _prowlarr() -> None:
            nonlocal prowlarr_items
            try:
                prowlarr_items = search_prowlarr(cfg, q, alt, year)
            except Exception as e:
                errors.append("prowlarr: %s" % e)

        threads = []
        if want_jacred and str(cfg.get("jacred_url") or "").strip():
            threads.append(threading.Thread(target=_jacred, name="cat-jacred"))
        if want_prowlarr and str(cfg.get("prowlarr_url") or "").strip():
            threads.append(threading.Thread(target=_prowlarr, name="cat-prowlarr"))

        if not threads:
            self._json_response(
                {
                    "ok": False,
                    "error": "Нет источников: укажите jacred_url и/или prowlarr_url + prowlarr_apikey в catalog.json",
                },
                400,
            )
            return

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=70)

        items = merge_torrent_items(jacred_items, prowlarr_items, limit=100)
        used = []
        if jacred_items:
            used.append(jacred_src or "jacred")
        if prowlarr_items:
            used.append("prowlarr")
        hint = ""
        if not items:
            parts = []
            if want_jacred:
                parts.append("JacRed пуст/не отвечает")
            if want_prowlarr:
                if not str(cfg.get("prowlarr_apikey") or cfg.get("prowlarr_api_key") or "").strip():
                    parts.append("Prowlarr: укажите prowlarr_apikey")
                else:
                    parts.append("Prowlarr пуст — добавьте индексеры в http://127.0.0.1:9696/")
            hint = ". ".join(parts) if parts else "Раздачи не найдены"
            if errors:
                hint += " | " + "; ".join(errors[:2])
        self._json_response(
            {
                "ok": True,
                "results": items,
                "query": q,
                "alt": alt,
                "year": year,
                "source": "+".join(used) if used else "",
                "sources_used": used,
                "counts": {
                    "jacred": len(jacred_items),
                    "prowlarr": len(prowlarr_items),
                    "total": len(items),
                },
                "hint": hint,
            }
        )

    def _list_local_torrents(self) -> None:
        os.makedirs(LOCAL_TORRENTS, exist_ok=True)
        items = []
        try:
            for name in sorted(os.listdir(LOCAL_TORRENTS)):
                if not name.lower().endswith(".torrent"):
                    continue
                path = os.path.join(LOCAL_TORRENTS, name)
                if not os.path.isfile(path):
                    continue
                items.append({"name": name, "size": os.path.getsize(path)})
        except OSError as e:
            self.send_error(500, str(e))
            return
        body = json.dumps({"dir": LOCAL_TORRENTS, "files": items}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _add_local_torrent(self, body: bytes) -> None:
        try:
            req = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            self.send_error(400, "bad json")
            return
        name = os.path.basename(str(req.get("name") or ""))
        if not name or not name.lower().endswith(".torrent") or ".." in name:
            self.send_error(400, "bad name")
            return
        path = os.path.join(LOCAL_TORRENTS, name)
        if not os.path.isfile(path):
            self.send_error(404, "file not found")
            return
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as e:
            self.send_error(500, str(e))
            return

        # multipart upload в TorrServer /torrent/upload
        boundary = "----VidTorrent%s" % int(time.time())
        chunks = []
        chunks.append(("--%s\r\n" % boundary).encode())
        chunks.append(
            ('Content-Disposition: form-data; name="file"; filename="%s"\r\n' % name).encode("utf-8", "ignore")
        )
        chunks.append(b"Content-Type: application/x-bittorrent\r\n\r\n")
        chunks.append(raw)
        chunks.append(b"\r\n")
        chunks.append(("--%s\r\n" % boundary).encode())
        chunks.append(b'Content-Disposition: form-data; name="save"\r\n\r\ntrue\r\n')
        chunks.append(("--%s--\r\n" % boundary).encode())
        payload = b"".join(chunks)

        conn = http.client.HTTPConnection(self.backend_host, self.backend_port, timeout=60)
        try:
            conn.request(
                "POST",
                "/torrent/upload",
                body=payload,
                headers={
                    "Content-Type": "multipart/form-data; boundary=%s" % boundary,
                    "Content-Length": str(len(payload)),
                    "Connection": "close",
                },
            )
            resp = conn.getresponse()
            data = resp.read()
            status = resp.status
        except Exception as e:
            self.send_error(502, "upload: %s" % e)
            return
        finally:
            try:
                conn.close()
            except Exception:
                pass

        out = json.dumps(
            {"ok": status >= 200 and status < 300, "status": status, "name": name},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200 if status >= 200 and status < 300 else 502)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self._cors()
        self.end_headers()
        self.wfile.write(out)
        # на всякий случай логируем ответ TS
        try:
            sys.stderr.write("local torrent add %s -> %s %s\n" % (name, status, data[:120]))
        except Exception:
            pass

    def _serve_matrix_index(self) -> None:
        """Отдаёт UI MatriX (:8092) через :8080 с <base href=\"/matrix/\">."""
        try:
            conn = http.client.HTTPConnection(self.backend_host, self.backend_port, timeout=20)
            conn.request("GET", "/", headers={"Accept": "text/html", "Connection": "close"})
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            if resp.status < 200 or resp.status >= 300:
                self.send_error(502, "MatriX HTTP %s" % resp.status)
                return
        except Exception as e:
            self.send_error(502, "MatriX: %s" % e)
            return
        text = raw.decode("utf-8", "replace")
        base = '<base href="/matrix/">'
        if "<base " in text.lower():
            text = re.sub(r"<base\s[^>]*>", base, text, count=1, flags=re.I)
        elif "<head>" in text.lower():
            text = re.sub(r"(?i)<head>", "<head>" + base, text, count=1)
        else:
            text = base + text
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _head_file(self, path: str, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self._cors()
        self.end_headers()

    def _serve_file(self, path: str, ctype: str) -> None:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            self.send_error(500, str(e))
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,HEAD,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization,Accept,Range")

    def _parse_media_qs(self, qs: dict):
        hash_ = (qs.get("hash") or qs.get("link") or [""])[0].strip()
        index = (qs.get("index") or ["1"])[0].strip() or "1"
        height = (qs.get("h") or qs.get("q") or ["480"])[0].strip()
        try:
            height_i = int(height)
        except ValueError:
            height_i = 480
        if height_i not in (360, 480, 720):
            height_i = 480
        start_raw = (qs.get("t") or qs.get("start") or qs.get("ss") or ["0"])[0].strip()
        try:
            start_i = int(float(start_raw))
        except ValueError:
            start_i = 0
        if start_i < 0:
            start_i = 0
        if start_i > 12 * 3600:
            start_i = 12 * 3600
        return hash_, index, height_i, start_i

    def _playlist_body(self, sid: str) -> bytes:
        playlist = os.path.join(HLS_ROOT, sid, "index.m3u8")
        with open(playlist, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        # Только относительные segXXX.ts — так старый Safari надёжнее
        lines = []
        for line in text.splitlines():
            if line and not line.startswith("#") and ".ts" in line:
                lines.append(os.path.basename(line.strip()))
            else:
                # убираем теги, которые мешают старым WebKit
                if line.startswith("#EXT-X-INDEPENDENT-SEGMENTS"):
                    continue
                if line.startswith("#EXT-X-VERSION:"):
                    lines.append("#EXT-X-VERSION:3")
                    continue
                lines.append(line)
        return ("\n".join(lines) + "\n").encode("utf-8")

    def _json_ok(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _hls_start(self, qs: dict) -> None:
        """Стартует ffmpeg (или переиспользует живую сессию) и сразу отдаёт sid."""
        if not os.path.isfile(FFMPEG):
            self.send_error(500, "ffmpeg not found")
            return
        hash_, index, height, start = self._parse_media_qs(qs)
        if not hash_:
            self.send_error(400, "hash required")
            return
        force = (qs.get("restart") or ["1"])[0].strip().lower() not in ("0", "false", "no")
        sid = make_sid(hash_, str(index), str(height), start=start)
        # По умолчанию каждый «Смотреть» — новый поток с начала фильма
        restart = force
        info = ensure_hls(
            self.backend_host,
            self.backend_port,
            hash_,
            index,
            height,
            restart=restart,
            sid=sid,
            start=start,
        )
        self.log_message(
            "hls start sid=%s restart=%s start=%s enc=%s",
            info["sid"],
            restart,
            start,
            ENCODER[0],
        )
        url = "/hls/files/%s/index.m3u8" % info["sid"]
        st = hls_segment_stats(info)
        self._json_ok(
            {
                "ok": True,
                "sid": info["sid"],
                "url": url,
                "segments": st["segments"],
                "pct": st["pct"],
                "ready": st["ready"],
                "reused": not restart,
                "need": st["need"],
                "start": start,
            }
        )

    def _hls_progress(self, qs: dict) -> None:
        sid = (qs.get("sid") or [""])[0].strip()
        if not sid or ".." in sid or "/" in sid or "\\" in sid:
            self.send_error(400, "sid required")
            return
        with SESSIONS_LOCK:
            info = SESSIONS.get(sid)
        if not info:
            # сессия могла пропасть, но файлы ещё на диске
            out_dir = os.path.join(HLS_ROOT, sid)
            st = hls_segment_stats({"dir": out_dir})
            self._json_ok(
                {
                    "ok": True,
                    "sid": sid,
                    "alive": False,
                    "segments": st["segments"],
                    "bytes": st["bytes"],
                    "pct": st["pct"],
                    "ready": st["ready"],
                    "url": "/hls/files/%s/index.m3u8" % sid,
                }
            )
            return
        info["last"] = time.time()
        st = hls_segment_stats(info)
        dead = not st["alive"] and not st["ready"]
        self._json_ok(
            {
                "ok": not dead,
                "sid": sid,
                "alive": st["alive"],
                "segments": st["segments"],
                "bytes": st["bytes"],
                "pct": st["pct"],
                "ready": st["ready"],
                "url": "/hls/files/%s/index.m3u8" % sid,
                "error": "ffmpeg stopped" if dead else "",
            }
        )

    def _hls_duration(self, qs: dict) -> None:
        hash_, index, _height, _start = self._parse_media_qs(qs)
        if not hash_:
            self.send_error(400, "hash required")
            return
        dur = probe_duration(self.backend_host, self.backend_port, hash_, index)
        self._json_ok(
            {
                "ok": dur > 0,
                "duration": int(dur) if dur > 0 else 0,
                "hash": hash_,
                "index": index,
            }
        )

    def _hls_cache(self, qs: dict) -> None:
        self._json_ok(hls_cache_stats())

    def _hls_cache_clear(self) -> None:
        self.log_message("hls cache clear")
        self._json_ok(purge_hls_cache())

    def _hls_stop(self, qs: dict) -> None:
        """Остановка ffmpeg при закрытии плеера."""
        sid = (qs.get("sid") or [""])[0].strip()
        hash_ = (qs.get("hash") or [""])[0].strip()
        all_ = (qs.get("all") or ["0"])[0].strip().lower() in ("1", "true", "yes")
        stopped = 0
        if all_ or (not sid and not hash_):
            with SESSIONS_LOCK:
                n = len(SESSIONS)
            kill_all_sessions()
            stopped = n
        elif hash_:
            stopped = stop_sessions_for_hash(hash_)
        else:
            if ".." in sid or "/" in sid or "\\" in sid:
                self.send_error(400, "bad sid")
                return
            with SESSIONS_LOCK:
                info = SESSIONS.pop(sid, None)
            if info:
                try:
                    info["proc"].kill()
                except Exception:
                    pass
                try:
                    info["proc"].wait(timeout=2)
                except Exception:
                    pass
                try:
                    info["log"].close()
                except Exception:
                    pass
                stopped = 1
            else:
                stopped = 0
        self.log_message(
            "hls stop sid=%s hash=%s stopped=%s",
            sid or "-",
            (hash_[:12] if hash_ else "-"),
            stopped,
        )
        self._json_ok({"ok": True, "stopped": stopped})

    def _hls_ready(self, qs: dict) -> None:
        """JSON: старт с начала, ждём первый сегмент, отдаём URL плейлиста."""
        if not os.path.isfile(FFMPEG):
            self.send_error(500, "ffmpeg not found")
            return
        hash_, index, height, start = self._parse_media_qs(qs)
        if not hash_:
            self.send_error(400, "hash required")
            return

        info = ensure_hls(
            self.backend_host,
            self.backend_port,
            hash_,
            index,
            height,
            restart=True,
            start=start,
        )
        self.log_message("hls ready sid=%s enc=%s", info["sid"], ENCODER[0])

        if not wait_playlist(info, timeout=180, min_segments=4):
            err = ""
            try:
                with open(os.path.join(info["dir"], "ffmpeg.log"), "rb") as f:
                    err = f.read()[-500:].decode("utf-8", "ignore")
            except Exception:
                pass
            body = ('{"ok":false,"error":"not ready","log":%s}' % json_escape(err[:200])).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return

        url = "/hls/files/%s/index.m3u8" % info["sid"]
        body = ('{"ok":true,"sid":"%s","url":"%s"}' % (info["sid"], url)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _hls_ready_playlist(self, qs: dict) -> None:
        hash_, index, height, start = self._parse_media_qs(qs)
        if not hash_:
            self.send_error(400, "hash required")
            return
        info = ensure_hls(
            self.backend_host,
            self.backend_port,
            hash_,
            index,
            height,
            restart=True,
            start=start,
        )
        if not wait_playlist(info, timeout=180, min_segments=4):
            self.send_error(502, "HLS not ready")
            return
        body = self._playlist_body(info["sid"])
        # Перепишем сегменты на абсолютные /hls/files/sid/ — этот endpoint не в той же папке
        text = body.decode("utf-8")
        lines = []
        for line in text.splitlines():
            if line and not line.startswith("#") and line.endswith(".ts"):
                lines.append("/hls/files/%s/%s" % (info["sid"], line))
            else:
                lines.append(line)
        body = ("\n".join(lines) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/x-mpegURL")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _hls_file(self, path: str) -> None:
        # /hls/files/<sid>/<file>
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        parts = path.strip("/").split("/")
        if len(parts) < 4:
            self.send_error(404)
            return
        sid = parts[2]
        name = parts[3]
        if ".." in sid or ".." in name or "/" in name or "\\" in name:
            self.send_error(400)
            return

        # Плейлист: старт сессии (если нужно) + ожидание сегментов + относительные имена
        if name == "index.m3u8":
            hash_, index, height, start = self._parse_media_qs(qs)
            if hash_:
                with SESSIONS_LOCK:
                    alive = sid in SESSIONS and SESSIONS[sid]["proc"].poll() is None
                info = ensure_hls(
                    self.backend_host,
                    self.backend_port,
                    hash_,
                    index,
                    height,
                    restart=not alive,
                    sid=sid,
                    start=start,
                )
                if not wait_playlist(info, timeout=180, min_segments=4):
                    self.send_error(502, "HLS not ready")
                    return
            else:
                with SESSIONS_LOCK:
                    if sid in SESSIONS:
                        SESSIONS[sid]["last"] = time.time()
                if not os.path.isfile(os.path.join(HLS_ROOT, sid, "index.m3u8")):
                    self.send_error(404, "playlist missing")
                    return

            try:
                body = self._playlist_body(sid)
            except OSError as e:
                self.send_error(500, str(e))
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-mpegURL")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self._cors()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return

        with SESSIONS_LOCK:
            if sid in SESSIONS:
                SESSIONS[sid]["last"] = time.time()

        fpath = os.path.join(HLS_ROOT, sid, name)
        if not os.path.isfile(fpath):
            for _ in range(25):
                if os.path.isfile(fpath):
                    break
                time.sleep(0.2)
        if not os.path.isfile(fpath):
            self.send_error(404, "segment missing")
            return

        ctype = "video/mp2t" if name.endswith(".ts") else "application/octet-stream"
        try:
            size = os.path.getsize(fpath)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-cache")
            self._cors()
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(fpath, "rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
        except OSError as e:
            self.send_error(500, str(e))

    def _transcode_ts(self, qs: dict) -> None:
        """Поток MPEG-TS для VLC."""
        if not os.path.isfile(FFMPEG):
            self.send_error(500, "ffmpeg not found")
            return
        hash_, index, height, start = self._parse_media_qs(qs)
        if not hash_:
            self.send_error(400, "hash required")
            return

        bitrates = {360: "800k", 480: "1400k", 720: "2500k"}
        bv = bitrates.get(height, "1400k")
        src = source_url(self.backend_host, self.backend_port, hash_, index)
        level = "4.0" if height >= 720 else "3.1"
        profile = "main" if height >= 720 else "baseline"

        cmd = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "+genpts+discardcorrupt",
        ]
        if start >= 5:
            cmd.extend(["-ss", str(start)])
        cmd.extend(
            [
                "-i",
                src,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-vf",
                "scale=-2:%d:flags=fast_bilinear" % height,
                "-c:v",
            ]
        )
        cmd.extend(ENCODER)
        cmd.extend(
            [
                "-profile:v",
                profile,
                "-level",
                level,
                "-pix_fmt",
                "yuv420p",
                "-b:v",
                bv,
                "-maxrate",
                bv,
                "-bufsize",
                "4000k",
                "-g",
                "48",
                "-af",
                "aresample=async=1:first_pts=0",
                "-c:a",
                "aac",
                "-ac",
                "2",
                "-ar",
                "44100",
                "-b:a",
                "128k",
                "-avoid_negative_ts",
                "make_zero",
                "-f",
                "mpegts",
                "pipe:1",
            ]
        )

        self.log_message("ts h=%s enc=%s hash=%s", height, ENCODER[0], hash_[:12])
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )
        except Exception as e:
            self.send_error(500, "ffmpeg start: %s" % e)
            return

        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self._cors()
            self.end_headers()
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                try:
                    self.wfile.flush()
                except Exception:
                    break
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                pass

    def _torrent_list(self) -> None:
        status, data = ts_api(self.backend_host, self.backend_port, {"action": "list"})
        if status < 200 or status >= 300:
            self.send_error(502, "list failed")
            return
        body = merge_posters_into_list(
            data or b"[]",
            push_backend=True,
            backend_host=self.backend_host,
            backend_port=self.backend_port,
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _torrent_set(self, req: dict) -> None:
        hash_ = str(req.get("hash") or "").strip()
        poster = str(req.get("poster") or "").strip()
        title = str(req.get("title") or "").strip()
        category = str(req.get("category") or "").strip()
        if not hash_:
            self.send_error(400, "hash required")
            return
        ok = True
        if poster:
            ok = apply_poster(self.backend_host, self.backend_port, hash_, poster)
        if title or category:
            payload = {"action": "set", "hash": hash_}
            if title:
                payload["title"] = title
            if category:
                payload["category"] = category
            if poster:
                payload["poster"] = poster
            status, _data = ts_api(self.backend_host, self.backend_port, payload)
            if status < 200 or status >= 300:
                ok = False
            if poster:
                apply_poster(self.backend_host, self.backend_port, hash_, poster)
        out = json.dumps(
            {"ok": ok, "hash": hash_, "poster": poster},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200 if ok else 502)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self._cors()
        self.end_headers()
        self.wfile.write(out)

    def _torrent_add(self, body: bytes, req: dict, poster: str) -> None:
        # проксируем add, потом пишем постер и в posters.json, и в TorrServer
        conn = http.client.HTTPConnection(self.backend_host, self.backend_port, timeout=120)
        headers = {"Content-Type": "application/json", "Connection": "close", "Accept-Encoding": "identity"}
        try:
            conn.request("POST", "/torrents", body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            self.send_error(502, "add: %s" % e)
            return
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if poster and status >= 200 and status < 300:
            h = hash_from_ts_response(raw) or str(req.get("hash") or "").strip()
            if h:
                time.sleep(0.2)
                apply_poster(self.backend_host, self.backend_port, h, poster)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _torrent_upload(self, body: bytes) -> None:
        poster = extract_multipart_field(body, "poster")
        title = extract_multipart_field(body, "title")
        conn = http.client.HTTPConnection(self.backend_host, self.backend_port, timeout=300)
        headers = {
            "Content-Type": self.headers.get("Content-Type") or "application/octet-stream",
            "Content-Length": str(len(body)),
            "Connection": "close",
            "Accept-Encoding": "identity",
        }
        ctype = "application/json; charset=utf-8"
        try:
            conn.request("POST", "/torrent/upload", body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
            ctype = resp.getheader("Content-Type") or ctype
        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            self.send_error(502, "upload: %s" % e)
            return
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if status >= 200 and status < 300 and (poster or title):
            h = hash_from_ts_response(raw)
            if h:
                time.sleep(0.2)
                if poster:
                    apply_poster(self.backend_host, self.backend_port, h, poster)
                if title:
                    payload = {"action": "set", "hash": h, "title": title}
                    if poster:
                        payload["poster"] = poster
                    ts_api(self.backend_host, self.backend_port, payload)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _proxy(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else None
        self._proxy_body(body)

    def _proxy_body(self, body) -> None:
        conn = http.client.HTTPConnection(self.backend_host, self.backend_port, timeout=300)
        headers = {}
        for key in ("Content-Type", "Accept", "Authorization", "Range", "User-Agent", "Icy-MetaData"):
            val = self.headers.get(key)
            if val:
                headers[key] = val
        if body is not None and "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"
        headers["Accept-Encoding"] = "identity"
        headers["Connection"] = "close"

        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            self.send_error(502, "TorrServer: %s" % e)
            return

        try:
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                lk = k.lower()
                if lk in HOP_BY_HOP or lk == "content-encoding":
                    continue
                self.send_header(k, v)
            self._cors()
            self.send_header("Connection", "close")
            self.end_headers()

            if self.command == "HEAD":
                return

            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                try:
                    self.wfile.flush()
                except Exception:
                    break
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass


class ThreadedServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def lan_ips():
    ips = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def stop_sessions_for_hash(hash_: str) -> int:
    """Глушим ffmpeg/HLS, иначе TorrServer rem не выкидывает торрент из памяти."""
    stopped = 0
    h = (hash_ or "").lower()
    with SESSIONS_LOCK:
        to_stop = []
        for sid, info in list(SESSIONS.items()):
            info_hash = str(info.get("hash") or "").lower()
            if (not h) or info_hash == h or (h[:16] and h[:16] in sid.lower()):
                to_stop.append(sid)
        for sid in to_stop:
            info = SESSIONS.get(sid)
            if not info:
                continue
            try:
                info["proc"].kill()
            except Exception:
                pass
            try:
                info["proc"].wait(timeout=2)
            except Exception:
                pass
            try:
                info["log"].close()
            except Exception:
                pass
            SESSIONS.pop(sid, None)
            stopped += 1
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/IM", "ffmpeg.exe", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
            )
        else:
            subprocess.run(["pkill", "-f", "ffmpeg"], timeout=8)
    except Exception:
        pass
    return stopped


def ts_api(backend_host: str, backend_port: int, payload: dict, timeout: int = 30):
    body = json.dumps(payload).encode("utf-8")
    conn = http.client.HTTPConnection(backend_host, backend_port, timeout=timeout)
    try:
        conn.request(
            "POST",
            "/torrents",
            body=body,
            headers={"Content-Type": "application/json", "Connection": "close"},
        )
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, data
    finally:
        try:
            conn.close()
        except Exception:
            pass


def force_remove_torrent(backend_host: str, backend_port: int, hash_: str):
    if not hash_:
        return False, "hash empty"

    def still_there():
        _status, data = ts_api(backend_host, backend_port, {"action": "list"})
        try:
            lst = json.loads(data.decode("utf-8") or "[]")
        except Exception:
            lst = []
        return [t for t in (lst or []) if str(t.get("hash", "")).lower() == hash_.lower()]

    stop_sessions_for_hash(hash_)
    time.sleep(0.8)
    ts_api(backend_host, backend_port, {"action": "drop", "hash": hash_})
    time.sleep(0.3)
    ts_api(backend_host, backend_port, {"action": "rem", "hash": hash_})
    time.sleep(0.6)
    if not still_there():
        return True, "ok"

    # Повтор
    stop_sessions_for_hash(hash_)
    time.sleep(1.0)
    ts_api(backend_host, backend_port, {"action": "drop", "hash": hash_})
    ts_api(backend_host, backend_port, {"action": "rem", "hash": hash_})
    time.sleep(0.6)
    if not still_there():
        return True, "ok"

    # Жёстко: перезапуск TorrServer (иначе rem игнорируется при активном стриме)
    if not restart_torrserver():
        return False, "torrent busy, torrserver restart failed"
    time.sleep(2.5)
    # После рестарта торрент подтянется из DB без активных ридеров — rem должен сработать
    ts_api(backend_host, backend_port, {"action": "rem", "hash": hash_})
    time.sleep(0.8)
    if still_there():
        ts_api(backend_host, backend_port, {"action": "drop", "hash": hash_})
        ts_api(backend_host, backend_port, {"action": "rem", "hash": hash_})
        time.sleep(0.8)
    if still_there():
        return False, "torrent still in list after restart"
    return True, "ok"


def restart_torrserver() -> bool:
    """Перезапуск TorrServer-windows-amd64.exe на 8092 с -d C:\\vid."""
    exe = os.path.join(os.path.dirname(ROOT), "TorrServer-windows-amd64.exe")
    if not os.path.isfile(exe):
        exe = r"C:\vid\TorrServer-windows-amd64.exe"
    if not os.path.isfile(exe):
        return False
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/IM", "TorrServer-windows-amd64.exe", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        time.sleep(1.2)
        subprocess.Popen(
            [exe, "-p", "8092", "-d", r"C:\vid", "-k"],
            cwd=r"C:\vid",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # ждём порт
        for _ in range(30):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", 8092, timeout=2)
                conn.request("GET", "/echo")
                resp = conn.getresponse()
                resp.read()
                conn.close()
                if resp.status == 200:
                    return True
            except Exception:
                time.sleep(0.4)
        return False
    except Exception:
        return False


def main() -> int:
    p = argparse.ArgumentParser(description="TorrServer lite gateway for old iPad")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--backend", default="127.0.0.1:8092")
    args = p.parse_args()

    host, port_s = args.backend.rsplit(":", 1)
    Gateway.backend_host = host
    Gateway.backend_port = int(port_s)

    os.makedirs(HLS_ROOT, exist_ok=True)
    st = cleanup_hls_cache()
    print("hls_cache: %.1fMB (auto-clean OFF — чистите вручную)" % (st.get("bytes", 0) / (1024 * 1024.0)))

    if not os.path.isfile(INDEX):
        print("Нет файла:", INDEX, file=sys.stderr)
        return 1

    print("encoder:", " ".join(ENCODER))
    print("ffmpeg:", FFMPEG)
    n = sync_posters_to_backend(Gateway.backend_host, Gateway.backend_port)
    print("posters synced to TorrServer: %d" % n)
    start_poster_sync_loop(Gateway.backend_host, Gateway.backend_port, 5.0)
    server = ThreadedServer(("0.0.0.0", args.port), Gateway)
    print("TorrServer Lite gateway")
    print("  UI:      http://127.0.0.1:%d/" % args.port)
    for ip in lan_ips():
        print("  iPad:    http://%s:%d/" % (ip, args.port))
    print("  backend: http://%s:%d/" % (Gateway.backend_host, Gateway.backend_port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstop")
        kill_all_sessions()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

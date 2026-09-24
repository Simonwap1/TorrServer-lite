# -*- coding: utf-8 -*-
"""Download and start local JacRed (jacred-go) if not reachable."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
VID_ROOT = os.path.dirname(ROOT)
JACRED_DIR = os.path.join(VID_ROOT, "jacred")
JACRED_EXE = os.path.join(JACRED_DIR, "jacred-windows-amd64.exe")
JACRED_INIT = os.path.join(JACRED_DIR, "init.yaml")
CATALOG = os.path.join(ROOT, "catalog.json")

RELEASE = "1.3.4"
DOWNLOAD_URL = (
    "https://github.com/trinity-aml/jacred-go/releases/download/%s/jacred-windows-amd64.exe"
    % RELEASE
)

MIN_INIT = """listenip: \"127.0.0.1\"
listenport: 9117
apikey: \"\"
devkey: \"\"
web: true
log: false
logParsers: false
mergeduplicates: true
mergenumduplicates: true
maxreadfile: 200
fdbPathLevels: 2
openstats: true
opensync: true
opensync_v1: false
syncapi: \"https://sync.jacred.stream\"
timeSync: 60
syncsport: true
syncspidr: true
timeStatsUpdate: 90
memlimit: 0
gcpercent: 50
evercache:
  enable: true
  validHour: 1
  maxOpenWriteTask: 300
  dropCacheTake: 50
"""


def load_cfg() -> dict:
    try:
        with open(CATALOG, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "vid-jacred"}, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= int(getattr(r, "status", 200) or 200) < 500
    except Exception:
        return False


def jacred_base(cfg: dict) -> str:
    return str(cfg.get("jacred_url") or "http://127.0.0.1:9117").strip().rstrip("/")


def is_local_url(url: str) -> bool:
    u = (url or "").lower()
    return "127.0.0.1" in u or "localhost" in u


def download_exe() -> None:
    os.makedirs(JACRED_DIR, exist_ok=True)
    tmp = JACRED_EXE + ".tmp"
    sys.stderr.write("JacRed: downloading %s\n" % DOWNLOAD_URL)
    req = urllib.request.Request(DOWNLOAD_URL, headers={"User-Agent": "vid-jacred"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = resp.read()
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, JACRED_EXE)
    sys.stderr.write("JacRed: saved %s (%d bytes)\n" % (JACRED_EXE, len(data)))


def ensure_init() -> None:
    if os.path.isfile(JACRED_INIT):
        return
    with open(JACRED_INIT, "w", encoding="utf-8") as f:
        f.write(MIN_INIT)
    sys.stderr.write("JacRed: wrote %s\n" % JACRED_INIT)


def start_jacred() -> subprocess.Popen | None:
    ensure_init()
    if not os.path.isfile(JACRED_EXE):
        download_exe()
    creation = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [JACRED_EXE],
        cwd=JACRED_DIR,
        creationflags=creation,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def ensure_jacred(timeout: float = 45.0) -> bool:
    """Return True if JacRed is reachable (started or already up)."""
    cfg = load_cfg()
    if cfg.get("jacred_auto_install") is False:
        return http_ok(jacred_base(cfg) + "/")
    base = jacred_base(cfg)
    if http_ok(base + "/") or http_ok(base + "/api/v1.0/torrents?search=test"):
        return True
    if not is_local_url(base):
        sys.stderr.write("JacRed: remote URL down, skip auto-install: %s\n" % base)
        return False
    try:
        if not os.path.isfile(JACRED_EXE):
            download_exe()
        ensure_init()
        start_jacred()
    except Exception as e:
        sys.stderr.write("JacRed: start failed: %s\n" % e)
        return False
    t0 = time.time()
    while time.time() - t0 < timeout:
        if http_ok(base + "/") or http_ok(base + "/api/v1.0/torrents?search=a"):
            sys.stderr.write("JacRed: OK %s\n" % base)
            # empty FDB until parsers run — kick public ones once
            try:
                warm_once(base)
            except Exception as e:
                sys.stderr.write("JacRed warm: %s\n" % e)
            return True
        time.sleep(0.5)
    sys.stderr.write("JacRed: timeout waiting for %s\n" % base)
    return False


def warm_once(base: str) -> None:
    """Non-blocking: start cron parsers so /api/v1.0/torrents is not always []."""
    base = (base or "").rstrip("/")
    marker = os.path.join(JACRED_DIR, ".warmed")
    # re-warm at most once per 6 hours
    try:
        if os.path.isfile(marker) and (time.time() - os.path.getmtime(marker)) < 6 * 3600:
            return
    except OSError:
        pass
    try:
        open(marker, "wb").close()
    except OSError:
        pass

    def _run() -> None:
        for name in ("rutor", "megapeer", "bitru", "knaben", "torrentby"):
            try:
                urllib.request.urlopen(base + "/cron/%s/parse" % name, timeout=180)
            except Exception as e:
                sys.stderr.write("jacred warm %s: %s\n" % (name, e))

    threading.Thread(target=_run, name="jacred-warm", daemon=True).start()
    sys.stderr.write("JacRed: warm parsers started\n")


if __name__ == "__main__":
    ok = ensure_jacred()
    print("jacred_ok" if ok else "jacred_fail")
    raise SystemExit(0 if ok else 1)

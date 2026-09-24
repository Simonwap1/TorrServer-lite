# -*- coding: utf-8 -*-
"""Download/start local Prowlarr if configured and not reachable."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
VID_ROOT = os.path.dirname(ROOT)
PROWLARR_DIR = os.path.join(VID_ROOT, "prowlarr")
PROWLARR_DATA = os.path.join(VID_ROOT, "prowlarr-data")
PROWLARR_EXE = os.path.join(PROWLARR_DIR, "Prowlarr.exe")
CATALOG = os.path.join(ROOT, "catalog.json")

RELEASE = "v2.6.5.5623"
ZIP_NAME = "Prowlarr.master.2.6.5.5623.windows-core-x64.zip"
DOWNLOAD_URL = (
    "https://github.com/Prowlarr/Prowlarr/releases/download/%s/%s" % (RELEASE, ZIP_NAME)
)


def load_cfg() -> dict:
    try:
        with open(CATALOG, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cfg(cfg: dict) -> None:
    with open(CATALOG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def http_ok(url: str, headers: dict | None = None, timeout: float = 2.0) -> bool:
    try:
        req = urllib.request.Request(
            url, headers=headers or {"User-Agent": "vid-prowlarr"}, method="GET"
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= int(getattr(r, "status", 200) or 200) < 500
    except Exception:
        return False


def prowlarr_base(cfg: dict) -> str:
    return str(cfg.get("prowlarr_url") or "http://127.0.0.1:9696").strip().rstrip("/")


def is_local_url(url: str) -> bool:
    u = (url or "").lower()
    return "127.0.0.1" in u or "localhost" in u or "0.0.0.0" in u


def read_api_key_from_data() -> str:
    cfg_path = os.path.join(PROWLARR_DATA, "config.xml")
    if not os.path.isfile(cfg_path):
        # default Windows path if started without -data
        alt = os.path.expandvars(r"%LOCALAPPDATA%\Prowlarr\config.xml")
        cfg_path = alt if os.path.isfile(alt) else cfg_path
    try:
        text = open(cfg_path, "r", encoding="utf-8", errors="ignore").read()
    except OSError:
        return ""
    m = re.search(r"<ApiKey>([^<]+)</ApiKey>", text)
    return (m.group(1).strip() if m else "")


def download_zip() -> None:
    os.makedirs(PROWLARR_DIR, exist_ok=True)
    zip_path = os.path.join(PROWLARR_DIR, ZIP_NAME)
    if not os.path.isfile(zip_path):
        sys.stderr.write("Prowlarr: downloading %s\n" % DOWNLOAD_URL)
        urllib.request.urlretrieve(DOWNLOAD_URL, zip_path)
    # zip contains a single top folder "Prowlarr"
    extract_tmp = os.path.join(PROWLARR_DIR, "_extract")
    if os.path.isdir(extract_tmp):
        import shutil

        shutil.rmtree(extract_tmp, ignore_errors=True)
    os.makedirs(extract_tmp, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_tmp)
    # move inner Prowlarr/* into PROWLARR_DIR
    inner = os.path.join(extract_tmp, "Prowlarr")
    if not os.path.isdir(inner):
        # sometimes flat
        for name in os.listdir(extract_tmp):
            p = os.path.join(extract_tmp, name)
            if os.path.isdir(p) and os.path.isfile(os.path.join(p, "Prowlarr.exe")):
                inner = p
                break
    if not os.path.isdir(inner):
        raise RuntimeError("Prowlarr.exe not found in zip")
    import shutil

    for name in os.listdir(inner):
        src = os.path.join(inner, name)
        dst = os.path.join(PROWLARR_DIR, name)
        if os.path.isdir(dst):
            shutil.rmtree(dst, ignore_errors=True)
        elif os.path.isfile(dst):
            try:
                os.remove(dst)
            except OSError:
                pass
        shutil.move(src, dst)
    shutil.rmtree(extract_tmp, ignore_errors=True)
    if not os.path.isfile(PROWLARR_EXE):
        raise RuntimeError("Prowlarr.exe missing after extract")


def start_prowlarr() -> None:
    os.makedirs(PROWLARR_DATA, exist_ok=True)
    if not os.path.isfile(PROWLARR_EXE):
        download_zip()
    creation = 0x08000000 if os.name == "nt" else 0
    subprocess.Popen(
        [PROWLARR_EXE, "-nobrowser", "-data=%s" % PROWLARR_DATA],
        cwd=PROWLARR_DIR,
        creationflags=creation,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def ensure_prowlarr(timeout: float = 90.0) -> bool:
    cfg = load_cfg()
    if cfg.get("prowlarr_auto_install") is False:
        base = prowlarr_base(cfg)
        key = str(cfg.get("prowlarr_apikey") or cfg.get("prowlarr_api_key") or "").strip()
        if not key:
            return False
        return http_ok(base + "/api/v1/system/status", headers={"X-Api-Key": key})

    base = prowlarr_base(cfg)
    key = str(cfg.get("prowlarr_apikey") or cfg.get("prowlarr_api_key") or "").strip()
    if key and http_ok(base + "/api/v1/system/status", headers={"X-Api-Key": key}):
        return True

    # try discover key from existing data
    if not key:
        key = read_api_key_from_data()
        if key:
            cfg["prowlarr_apikey"] = key
            cfg["prowlarr_url"] = base
            try:
                save_cfg(cfg)
            except Exception:
                pass
            if http_ok(base + "/api/v1/system/status", headers={"X-Api-Key": key}):
                return True

    if not is_local_url(base):
        sys.stderr.write("Prowlarr: remote URL down, skip auto-install: %s\n" % base)
        return False

    try:
        if not os.path.isfile(PROWLARR_EXE):
            download_zip()
        start_prowlarr()
    except Exception as e:
        sys.stderr.write("Prowlarr: start failed: %s\n" % e)
        return False

    t0 = time.time()
    while time.time() - t0 < timeout:
        key = key or read_api_key_from_data()
        if key:
            cfg["prowlarr_apikey"] = key
            cfg.setdefault("prowlarr_url", base)
            try:
                save_cfg(cfg)
            except Exception:
                pass
            if http_ok(base + "/ping") or http_ok(
                base + "/api/v1/system/status", headers={"X-Api-Key": key}
            ):
                sys.stderr.write("Prowlarr: OK %s\n" % base)
                try:
                    seed = os.path.join(ROOT, "seed_prowlarr_indexers.py")
                    if os.path.isfile(seed):
                        subprocess.run(
                            [sys.executable, seed],
                            cwd=ROOT,
                            timeout=300,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=0x08000000 if os.name == "nt" else 0,
                        )
                except Exception:
                    pass
                return True
        elif http_ok(base + "/ping"):
            # up but key not ready yet
            pass
        time.sleep(1.0)
    sys.stderr.write("Prowlarr: timeout waiting for %s\n" % base)
    return False


if __name__ == "__main__":
    ok = ensure_prowlarr()
    print("prowlarr_ok" if ok else "prowlarr_fail")
    raise SystemExit(0 if ok else 1)

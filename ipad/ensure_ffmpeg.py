#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ставит ffmpeg/ffprobe в C:\\vid\\tools\\ffmpeg, если их ещё нет."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
VID_ROOT = os.path.dirname(ROOT)
TOOLS_FFMPEG = os.path.join(VID_ROOT, "tools", "ffmpeg")
BIN_DIR = os.path.join(TOOLS_FFMPEG, "bin")
FFMPEG_EXE = os.path.join(BIN_DIR, "ffmpeg.exe")
FFPROBE_EXE = os.path.join(BIN_DIR, "ffprobe.exe")

# Официальная essentials-сборка (Windows x64)
FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

CANDIDATES = [
    FFMPEG_EXE,
    r"C:\ffmpeg\bin\ffmpeg.exe",
    shutil.which("ffmpeg") or "",
]


def find_ffmpeg() -> str:
    for p in CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return ""


def find_ffprobe(ffmpeg_path: str = "") -> str:
    probes = [
        FFPROBE_EXE,
        r"C:\ffmpeg\bin\ffprobe.exe",
        shutil.which("ffprobe") or "",
    ]
    if ffmpeg_path:
        sibling = os.path.join(os.path.dirname(ffmpeg_path), "ffprobe.exe")
        probes.insert(0, sibling)
    for p in probes:
        if p and os.path.isfile(p):
            return p
    return ""


def _download(url: str, dest: str, timeout: int = 300) -> None:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "TorrServer-Lite/1.0 (ffmpeg-setup)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)


def _extract_bins(zip_path: str, dest_bin: str) -> bool:
    os.makedirs(dest_bin, exist_ok=True)
    want = ("ffmpeg.exe", "ffprobe.exe")
    found = {n: False for n in want}
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            base = os.path.basename(name).lower()
            if base not in want:
                continue
            if "/bin/" not in name.lower() and not name.lower().endswith("/" + base):
                # всё равно берём, если это exe в корне bin-подобной папки
                if not name.lower().endswith(base):
                    continue
            target = os.path.join(dest_bin, base)
            with zf.open(info, "r") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            found[base] = True
    return all(found.values())


def ensure_ffmpeg(force: bool = False) -> str:
    """Возвращает путь к ffmpeg.exe. Скачивает при необходимости."""
    cur = find_ffmpeg()
    if cur and not force:
        return cur

    sys.stderr.write("ffmpeg: скачиваю essentials в %s …\n" % TOOLS_FFMPEG)
    os.makedirs(TOOLS_FFMPEG, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="ffmpeg_dl_")
    zip_path = os.path.join(tmp, "ffmpeg.zip")
    try:
        _download(FFMPEG_ZIP_URL, zip_path)
        if not _extract_bins(zip_path, BIN_DIR):
            raise RuntimeError("в архиве нет ffmpeg.exe / ffprobe.exe")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if not os.path.isfile(FFMPEG_EXE):
        raise RuntimeError("не удалось установить ffmpeg")
    sys.stderr.write("ffmpeg: готово → %s\n" % FFMPEG_EXE)
    return FFMPEG_EXE


def main() -> int:
    try:
        path = ensure_ffmpeg(force="--force" in sys.argv)
    except Exception as e:
        sys.stderr.write("ffmpeg install failed: %s\n" % e)
        return 1
    print(path)
    probe = find_ffprobe(path)
    if probe:
        print(probe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

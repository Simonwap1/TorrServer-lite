"""Single-tray launcher for TorrServer + Lite UI (no console windows)."""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from ctypes import wintypes

ROOT = os.path.dirname(os.path.abspath(__file__))
TS_PORT = 8092
UI_PORT = 8080
JACRED_PORT = 9117
TS_EXE = os.path.join(ROOT, "TorrServer-windows-amd64.exe")
GATEWAY = os.path.join(ROOT, "ipad", "gateway.py")
ICON_FILE = os.path.join(ROOT, "films.ico")


def _guess_lan_ip() -> str:
    try:
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"


_LAN = _guess_lan_ip()
OPEN_URL = "http://127.0.0.1:%d/" % UI_PORT
TIP_TEXT = "Фильмы — http://%s:%d/" % (_LAN, UI_PORT)
MENU_OPEN = "Открыть"
MENU_CLEAR_HLS = "Очистить HLS кэш"
MENU_EXIT = "Выход"

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32
ole32 = ctypes.windll.ole32

# 64-bit safe pointer-sized types (wintypes.LPARAM can be too narrow)
LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
HWND = wintypes.HWND
UINT = wintypes.UINT

user32.DefWindowProcW.argtypes = [HWND, UINT, WPARAM, LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), HWND, UINT, UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostMessageW.argtypes = [HWND, UINT, WPARAM, LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.PostQuitMessage.argtypes = [ctypes.c_int]

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
WM_USER = 0x0400
WM_TRAY = WM_USER + 20
WM_NULL = 0x0000
WM_APP = 0x8000
WM_QUIT_APP = WM_APP + 1

IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_DEFAULTSIZE = 0x00000040
LR_SHARED = 0x00008000
LR_LOADFROMFILE = 0x00000010
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_SHOWTIP = 0x00000080
MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_RETURNCMD = 0x0100
TPM_LEFTALIGN = 0x0000
TPM_RIGHTBUTTON = 0x0002
WS_OVERLAPPED = 0x00000000
IDC_ARROW = 32512
ERROR_ALREADY_EXISTS = 183
CREATE_NO_WINDOW = 0x08000000

ID_OPEN = 1001
ID_QUIT = 1002
ID_CLEAR_HLS = 1003

# Vista+ NOTIFYICONDATA size (with guidItem + hBalloonIcon omitted we use V2 tip size)
class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)

_procs = []  # type: list
_hwnd = None
_wndproc_ref = None
_nid = None


def http_ok(url, needle=None, timeout=1.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read().decode("utf-8", "ignore")
            if needle and needle not in body:
                return False
            return True
    except Exception:
        return False


def http_json(url, timeout=8):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore") or "{}")
    except Exception:
        return None


def fmt_bytes(n):
    n = float(n or 0)
    if n < 1024:
        return "%d B" % int(n)
    if n < 1048576:
        return "%.1f KB" % (n / 1024.0)
    if n < 1073741824:
        return "%.1f MB" % (n / 1048576.0)
    return "%.2f GB" % (n / 1073741824.0)


def hls_cache_info():
    data = http_json("http://127.0.0.1:%d/hls/cache" % UI_PORT, timeout=5)
    if isinstance(data, dict) and data.get("ok") is not False:
        return int(data.get("bytes") or 0), int(data.get("folders") or 0)
    # fallback: считаем с диска, если шлюз ещё не поднялся
    root = os.path.join(ROOT, "ipad", "hls_cache")
    total = 0
    folders = 0
    if os.path.isdir(root):
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath == root:
                folders = len(dirnames)
            for name in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
    return total, folders


def clear_hls_cache():
    data = http_json("http://127.0.0.1:%d/hls/cache/clear" % UI_PORT, timeout=60)
    if isinstance(data, dict) and data.get("ok"):
        return True, int(data.get("freed") or 0), int(data.get("bytes") or 0)
    # прямой сброс папки
    root = os.path.join(ROOT, "ipad", "hls_cache")
    before, _ = hls_cache_info()
    if os.path.isdir(root):
        for name in list(os.listdir(root)):
            p = os.path.join(root, name)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.remove(p)
            except OSError:
                pass
    after, _ = hls_cache_info()
    return True, max(0, before - after), after


def ensure_deps():
    """ffmpeg (+ ffprobe) для HLS на iPad."""
    script = os.path.join(ROOT, "ipad", "ensure_ffmpeg.py")
    if not os.path.isfile(script):
        return True
    # уже есть — тихо
    try:
        sys.path.insert(0, os.path.join(ROOT, "ipad"))
        import ensure_ffmpeg as _ef  # type: ignore

        if _ef.find_ffmpeg():
            return True
        user32.MessageBoxW(
            None,
            "Первый запуск: скачиваю ffmpeg (~80 МБ).\nНужен интернет, подождите…",
            "Фильмы",
            0x40,
        )
    except Exception:
        pass
    try:
        r = subprocess.run(
            [sys.executable, script],
            cwd=ROOT,
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=600,
        )
        return r.returncode == 0
    except Exception:
        return False


def wait_ready(timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if http_ok("http://127.0.0.1:%d/echo" % TS_PORT, "MatriX") and http_ok(
            "http://127.0.0.1:%d/" % UI_PORT
        ):
            return True
        time.sleep(0.5)
    return False


def start_children():
    if not os.path.isfile(TS_EXE):
        user32.MessageBoxW(None, "Нет файла TorrServer-windows-amd64.exe:\n" + ROOT, "Фильмы", 0x10)
        return False
    if not os.path.isfile(GATEWAY):
        user32.MessageBoxW(None, "Нет файла gateway.py", "Фильмы", 0x10)
        return False

    if not ensure_deps():
        user32.MessageBoxW(
            None,
            "Не удалось установить ffmpeg.\nНужен интернет при первом запуске\nили положите ffmpeg в C:\\ffmpeg\\bin\\",
            "Фильмы",
            0x30,
        )

    # JacRed / Prowlarr больше не поднимаем — каталог убран из UI

    if not http_ok("http://127.0.0.1:%d/echo" % TS_PORT, "MatriX"):
        _procs.append(
            subprocess.Popen(
                [TS_EXE, "-p", str(TS_PORT), "-d", ROOT, "-k"],
                cwd=ROOT,
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )

    py = sys.executable
    if py.lower().endswith("python.exe"):
        pyw = py[:-10] + "pythonw.exe"
        if os.path.isfile(pyw):
            py = pyw

    # always refresh gateway on start (reload code from disk)
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "$c=Get-NetTCPConnection -LocalPort %d -State Listen -EA SilentlyContinue;"
                    "if($c){ $c | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -EA SilentlyContinue } }"
                )
                % UI_PORT,
            ],
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
        time.sleep(0.5)
    except Exception:
        pass

    _procs.append(
        subprocess.Popen(
            [py, GATEWAY, "--port", str(UI_PORT), "--backend", "127.0.0.1:%d" % TS_PORT],
            cwd=ROOT,
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    )
    return True


def stop_children():
    for p in list(_procs):
        try:
            if p.poll() is None:
                p.terminate()
        except Exception:
            pass
    time.sleep(0.4)
    for p in list(_procs):
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass
    _procs[:] = []
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "TorrServer-windows-amd64.exe"],
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "jacred-windows-amd64.exe"],
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "Prowlarr.exe"],
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
    # kill gateway python holding 8080 — only our child list is safer; also try by window title skip
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "$c=Get-NetTCPConnection -LocalPort %d -State Listen -EA SilentlyContinue;"
                    "if($c){ $c | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -EA SilentlyContinue } }"
                )
                % UI_PORT,
            ],
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
    except Exception:
        pass


def open_ui():
    webbrowser.open(OPEN_URL)


def show_menu(hwnd):
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    menu = user32.CreatePopupMenu()
    user32.AppendMenuW(menu, MF_STRING, ID_OPEN, MENU_OPEN)
    size_b, folders = hls_cache_info()
    clear_label = "%s — %s" % (MENU_CLEAR_HLS, fmt_bytes(size_b))
    if folders:
        clear_label += " · %d пап." % folders
    user32.AppendMenuW(menu, MF_STRING, ID_CLEAR_HLS, clear_label)
    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
    user32.AppendMenuW(menu, MF_STRING, ID_QUIT, MENU_EXIT)
    user32.SetForegroundWindow(hwnd)
    cmd = user32.TrackPopupMenu(
        menu,
        TPM_RETURNCMD | TPM_LEFTALIGN | TPM_RIGHTBUTTON,
        pt.x,
        pt.y,
        0,
        hwnd,
        None,
    )
    user32.DestroyMenu(menu)
    user32.PostMessageW(hwnd, WM_NULL, 0, 0)
    if cmd == ID_OPEN:
        open_ui()
    elif cmd == ID_CLEAR_HLS:
        ok, freed, left = clear_hls_cache()
        if ok:
            user32.MessageBoxW(
                None,
                "HLS кэш очищен.\nОсвобождено: %s\nСейчас: %s" % (fmt_bytes(freed), fmt_bytes(left)),
                "Фильмы",
                0x40,
            )
        else:
            user32.MessageBoxW(None, "Не удалось очистить HLS кэш", "Фильмы", 0x30)
    elif cmd == ID_QUIT:
        user32.PostMessageW(hwnd, WM_QUIT_APP, 0, 0)


def remove_tray(hwnd):
    global _nid
    if _nid is not None:
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(_nid))
        _nid = None


def wnd_proc(hwnd, msg, wparam, lparam):
    if msg == WM_TRAY:
        low = lparam & 0xFFFF
        if low in (WM_RBUTTONUP, WM_LBUTTONDBLCLK):
            show_menu(hwnd)
        return 0
    if msg == WM_COMMAND:
        if wparam & 0xFFFF == ID_OPEN:
            open_ui()
        elif wparam & 0xFFFF == ID_CLEAR_HLS:
            clear_hls_cache()
        elif wparam & 0xFFFF == ID_QUIT:
            user32.PostMessageW(hwnd, WM_QUIT_APP, 0, 0)
        return 0
    if msg == WM_QUIT_APP:
        remove_tray(hwnd)
        stop_children()
        user32.PostQuitMessage(0)
        return 0
    if msg == WM_DESTROY:
        remove_tray(hwnd)
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def run_tray():
    global _hwnd, _wndproc_ref, _nid
    try:
        ole32.CoInitialize(None)
    except Exception:
        pass

    hInstance = kernel32.GetModuleHandleW(None)
    class_name = "FilmsLiteTrayClass"
    _wndproc_ref = WNDPROC(wnd_proc)

    class WNDCLASS(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    wc = WNDCLASS()
    wc.lpfnWndProc = _wndproc_ref
    wc.hInstance = hInstance
    wc.lpszClassName = class_name
    wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))

    if not user32.RegisterClassW(ctypes.byref(wc)):
        err = kernel32.GetLastError()
        if err not in (0, 1410):  # already registered
            user32.MessageBoxW(None, "RegisterClass error: %s" % err, "Фильмы", 0x10)
            stop_children()
            return 1

    hwnd = user32.CreateWindowExW(
        0,
        class_name,
        "Фильмы",
        WS_OVERLAPPED,
        0,
        0,
        0,
        0,
        None,
        None,
        hInstance,
        None,
    )
    _hwnd = hwnd
    if not hwnd:
        user32.MessageBoxW(None, "Не удалось создать окно трея: %s" % kernel32.GetLastError(), "Фильмы", 0x10)
        stop_children()
        return 1

    hIcon = None
    if os.path.isfile(ICON_FILE):
        hIcon = user32.LoadImageW(
            None,
            ICON_FILE,
            IMAGE_ICON,
            0,
            0,
            LR_LOADFROMFILE | LR_DEFAULTSIZE,
        )
    if not hIcon:
        hIcon = user32.LoadImageW(
            None,
            ctypes.c_void_p(IDI_APPLICATION),
            IMAGE_ICON,
            0,
            0,
            LR_DEFAULTSIZE | LR_SHARED,
        )
    if not hIcon:
        hIcon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))

    nid = NOTIFYICONDATA()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_TRAY
    nid.hIcon = hIcon
    nid.szTip = TIP_TEXT[:127]
    ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
    if not ok:
        nid.cbSize = 952
        ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
    if not ok:
        user32.MessageBoxW(
            None,
            "Не удалось показать иконку в трее. UI может работать.",
            "Фильмы",
            0x30,
        )
    else:
        _nid = nid

    def _boot():
        if wait_ready(90):
            open_ui()
        else:
            user32.MessageBoxW(
                None,
                "TorrServer / UI не запустились.\nПроверьте Python, ffmpeg, брандмауэр.",
                "Фильмы",
                0x30,
            )

    threading.Thread(target=_boot, daemon=True).start()

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    return 0


def main():
    kernel32.SetLastError(0)
    kernel32.CreateMutexW(None, False, "Local\\FilmsLiteTrayMutex")
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        open_ui()
        return 0
    if not start_children():
        return 1
    return run_tray()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        try:
            user32.MessageBoxW(None, "Ошибка Фильмы:\n%s" % e, "Фильмы", 0x10)
        except Exception:
            pass
        raise

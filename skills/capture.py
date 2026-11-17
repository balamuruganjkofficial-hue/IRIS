"""Live frames for Iris' display surface: windows and the whole desktop.

The interface never calls Windows itself. This module owns one small worker
thread that grabs either a single window (for apps that are mirrored inside
Iris) or the screen, writes a JPEG, and publishes it on the ``surface_frame``
bus topic. ``interfaces/ui.py`` swaps the picture in place, so the page or the
app looks like it is running inside Iris' own window.

Best-effort input forwarding is included as well: clicks, scrolling and keys
are posted to the mirrored window's message queue. Apps that refuse synthetic
input simply ignore it; nothing here needs admin rights and nothing can freeze
the interface, because capturing happens off the Tk thread.
"""

import atexit
import ctypes
import logging
import os
import threading
import time
from pathlib import Path

from core import bus
from core.config import CFG, ROOT

log = logging.getLogger("iris")

FRAMES = ROOT / "data" / "surface_frames"
_LOCK = threading.Lock()
_JOB = None
_THREAD = None
_COUNTER = [0]
_LAST_KEPT = []
_MAX_KEPT = 24

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEWHEEL = 0x020A
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
MK_LBUTTON = 0x0001
SRCCOPY = 0x00CC0020

_VK = {
    "Return": 0x0D, "KP_Enter": 0x0D, "BackSpace": 0x08, "Tab": 0x09,
    "Escape": 0x1B, "space": 0x20, "Left": 0x25, "Up": 0x26,
    "Right": 0x27, "Down": 0x28, "Home": 0x24, "End": 0x23,
    "Prior": 0x21, "Next": 0x22, "Delete": 0x2E, "Insert": 0x2D,
}


# ------------------------------------------------------------------ settings
def _surface_cfg():
    return CFG.get("surface") or {}


def _fps():
    try:
        return max(2.0, min(15.0, float(_surface_cfg().get("live_fps", 6))))
    except (TypeError, ValueError):
        return 6.0


def _quality():
    try:
        return max(35, min(92, int((CFG.get("browser") or {})
                                   .get("capture_quality", 70))))
    except (TypeError, ValueError):
        return 70


# ------------------------------------------------------------------ capturing
def _black(img):
    try:
        return img.convert("L").getextrema()[1] < 6
    except Exception:  # noqa: BLE001
        return False


def _grab_window(hwnd):
    """PrintWindow first (works when the window is covered), BitBlt after."""
    import win32gui
    import win32ui

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = int(right - left), int(bottom - top)
    if width < 8 or height < 8:
        return None
    try:
        hwnd_dc = win32gui.GetWindowDC(hwnd)
    except Exception:  # noqa: BLE001
        return None
    src = mem = bmp = None
    try:
        src = win32ui.CreateDCFromHandle(hwnd_dc)
        mem = src.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(src, width, height)
        mem.SelectObject(bmp)
        user32 = ctypes.windll.user32
        try:
            ok = bool(user32.PrintWindow(hwnd, mem.GetSafeHdc(), 2))
        except Exception:  # noqa: BLE001
            ok = False
        info = bmp.GetInfo()
        bits = bmp.GetBitmapBits(True)
        img = _from_bits(bits, info["bmWidth"], info["bmHeight"])
        if img is None or not ok or _black(img):
            try:
                mem.BitBlt((0, 0), (width, height), src, (0, 0), SRCCOPY)
                bits = bmp.GetBitmapBits(True)
                alt = _from_bits(bits, info["bmWidth"], info["bmHeight"])
                if alt is not None and not _black(alt):
                    img = alt
            except Exception:  # noqa: BLE001
                pass
        return img
    except Exception as e:  # noqa: BLE001
        log.debug("window capture failed: %s", e)
        return None
    finally:
        try:
            if bmp is not None:
                win32gui.DeleteObject(bmp.GetHandle())
        except Exception:  # noqa: BLE001
            pass
        for dc in (mem, src):
            try:
                if dc is not None:
                    dc.DeleteDC()
            except Exception:  # noqa: BLE001
                pass
        try:
            win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:  # noqa: BLE001
            pass


def _from_bits(bits, width, height):
    from PIL import Image
    try:
        return Image.frombuffer("RGB", (int(width), int(height)), bits,
                                "raw", "BGRX", 0, 1).copy()
    except Exception:  # noqa: BLE001
        return None


def _grab_screen():
    from PIL import ImageGrab
    try:
        img = ImageGrab.grab(all_screens=True)
    except TypeError:                       # older Pillow
        img = ImageGrab.grab()
    return img.convert("RGB")


def _save(img):
    """Write one JPEG and publish it. Old frames are pruned on the way."""
    from PIL import Image
    width = int((CFG.get("browser") or {}).get("capture_width", 1280) or 1280)
    if img.width > width:
        img = img.resize((width, max(1, int(img.height * width / img.width))),
                         Image.BILINEAR)
    FRAMES.mkdir(parents=True, exist_ok=True)
    _COUNTER[0] = (_COUNTER[0] + 1) % 100000
    path = FRAMES / "frame_{0:05d}.jpg".format(_COUNTER[0])
    img.save(path, "JPEG", quality=_quality(), optimize=False)
    _LAST_KEPT.append(path)
    while len(_LAST_KEPT) > _MAX_KEPT:
        old = _LAST_KEPT.pop(0)
        try:
            old.unlink(missing_ok=True)
        except OSError:
            pass
    return path


def _publish(path, img, job, lost=False):
    bus.publish("surface_frame", {
        "state": "lost" if lost else "frame",
        "kind": job.get("kind", "window"),
        "hwnd": job.get("hwnd", 0),
        "path": "" if path is None else str(path),
        "w": int(img.width) if img is not None else 0,
        "h": int(img.height) if img is not None else 0,
        "at": time.time(),
    })


def _loop():
    global _JOB
    while True:
        with _LOCK:
            job = _JOB
        if job is None:
            time.sleep(0.12)
            continue
        started = time.perf_counter()
        try:
            if job.get("kind") == "screen":
                img = _grab_screen()
            else:
                hwnd = int(job.get("hwnd", 0))
                import win32gui
                if not hwnd or not win32gui.IsWindow(hwnd):
                    _publish(None, None, job, lost=True)
                    with _LOCK:
                        if _JOB is job:
                            _JOB = None
                    continue
                if win32gui.IsIconic(hwnd):
                    _publish(None, None, job, lost=True)
                    time.sleep(0.5)
                    continue
                img = _grab_window(hwnd)
            if img is None:
                _publish(None, None, job, lost=True)
                time.sleep(0.5)
                continue
            path = _save(img)
            _publish(path, img, job)
        except Exception:  # noqa: BLE001 - a mirror never kills Iris
            log.exception("surface frame failed")
            time.sleep(0.4)
        delay = max(0.02, 1.0 / _fps() - (time.perf_counter() - started))
        time.sleep(delay)


def _ensure_thread():
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _THREAD = threading.Thread(target=_loop, daemon=True, name="surface-frames")
        _THREAD.start()


def start_window(hwnd, title=""):
    global _JOB
    try:
        hwnd = int(hwnd)
    except (TypeError, ValueError):
        return False
    if not hwnd:
        return False
    with _LOCK:
        _JOB = {"kind": "window", "hwnd": hwnd, "title": str(title or "")}
    _ensure_thread()
    return True


def start_screen():
    global _JOB
    with _LOCK:
        _JOB = {"kind": "screen"}
    _ensure_thread()
    return True


def stop():
    global _JOB, _LAST_KEPT
    with _LOCK:
        _JOB = None
    return True


def running():
    with _LOCK:
        return dict(_JOB) if _JOB else None


# ------------------------------------------------------------------- input
def _alive(hwnd):
    try:
        import win32gui
        return bool(hwnd) and bool(win32gui.IsWindow(int(hwnd)))
    except Exception:  # noqa: BLE001
        return False


def _post(hwnd, message, wparam, lparam):
    """Post once, never raise: a stale handle is not an Iris error."""
    try:
        import win32api
        win32api.PostMessage(int(hwnd), message, int(wparam), int(lparam))
        return True
    except Exception:  # noqa: BLE001
        return False


def _client_offset(hwnd):
    """Difference between the window rectangle and its client area."""
    import win32gui
    try:
        left, top, _, _ = win32gui.GetWindowRect(hwnd)
        cx, cy = win32gui.ClientToScreen(hwnd, (0, 0))
        return int(cx - left), int(cy - top)
    except Exception:  # noqa: BLE001
        return 0, 0


def click(hwnd, x, y):
    hwnd = int(hwnd)
    if not _alive(hwnd):
        return False
    dx, dy = _client_offset(hwnd)
    x, y = int(x) - dx, int(y) - dy
    if x < 0 or y < 0:
        return False
    lparam = (y << 16) | (x & 0xFFFF)
    try:
        import win32api
        win32api.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001
        pass
    _post(hwnd, WM_MOUSEMOVE, 0, lparam)
    _post(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
    time.sleep(0.03)
    _post(hwnd, WM_LBUTTONUP, 0, lparam)
    return True


def scroll(hwnd, x, y, dy):
    hwnd = int(hwnd)
    if not _alive(hwnd):
        return False
    delta = int(-float(dy) * 1.8)
    delta = max(-1200, min(1200, delta))
    wparam = ((delta & 0xFFFF) << 16)
    dx, dy_off = _client_offset(hwnd)
    try:
        import win32gui
        sx, sy = win32gui.ClientToScreen(hwnd, (int(x) - dx, int(y) - dy_off))
    except Exception:  # noqa: BLE001
        sx, sy = int(x), int(y)
    lparam = ((int(sy) & 0xFFFF) << 16) | (int(sx) & 0xFFFF)
    _post(hwnd, WM_MOUSEWHEEL, wparam, lparam)
    return True


def key(hwnd, keysym):
    hwnd = int(hwnd)
    if not _alive(hwnd):
        return False
    vk = _VK.get(str(keysym))
    if vk is None:
        text = str(keysym)
        if len(text) == 1:
            return _post(hwnd, WM_CHAR, ord(text), 0)
        return False
    _post(hwnd, WM_KEYDOWN, vk, 0)
    _post(hwnd, WM_KEYUP, vk, 0)
    return True


def type_text(hwnd, text):
    if not _alive(hwnd):
        return False
    for char in str(text or ""):
        if not _post(hwnd, WM_CHAR, ord(char), 0):
            return False
        time.sleep(0.006)
    return True


@atexit.register
def _shutdown():
    stop()

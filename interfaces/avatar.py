"""Iris' floating avatar: a small human presence that lives outside her window.

The main interface is a page you visit; this is the part that sits on the
desktop with you. It is a frameless, always-on-top Toplevel drawn with plain
tkinter: a stylised woman with hair, eyes that blink and look around, brows that
react, a mouth that moves with her speech and shoulders that breathe. A short
caption shows the last thing she said. Drag it anywhere, click it to bring the
main window back, double-click to start listening, right-click for a tiny menu.

All drawing happens on the Tk thread. The assistant never touches this file
directly; :class:`interfaces.ui.IrisUI` forwards state, level and speech events.
The small pure functions near the top keep the motion testable without a display.
"""

import json
import logging
import math
import time
import tkinter as tk

from core.config import CFG, DATA

log = logging.getLogger("iris")

_UI = CFG.get("ui") or {}
BG = _UI.get("bg", "#060407")
BG2 = _UI.get("bg2", "#12060A")
SURFACE = _UI.get("surface", "#1A070C")
ACCENT = _UI.get("accent", "#FF1F3D")
ACCENT_SOFT = _UI.get("accent_soft", "#FF5C73")
TEXT = _UI.get("text", "#F7ECEF")
MUTED = _UI.get("muted", "#A07A82")
TRANSPARENT = "#010203"
POS_FILE = DATA / "avatar_pos.json"

SKIN = "#F2C7AA"
SKIN_LIGHT = "#FFE0CC"
SKIN_SHADE = "#D79A7D"
HAIR = "#2B0A16"
HAIR_HI = "#7A1C31"
LIPS = "#E4607C"
LIP_DARK = "#5A1020"
EYE_WHITE = "#FFF5F1"
IRIS = "#B33A52"
PUPIL = "#2A0710"
BLUSH = "#FF8FA8"

_SPEED = {"idle": 1.4, "listening": 2.8, "thinking": 3.6, "speaking": 5.0}
_COLOR = {"idle": MUTED, "listening": ACCENT, "thinking": ACCENT_SOFT,
          "speaking": ACCENT}
_EYE_OPEN = {"idle": 1.0, "listening": 1.14, "thinking": 0.92,
             "speaking": 0.86}


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    t = max(0.0, min(1.0, t))
    ca, cb = _rgb(a), _rgb(b)
    return "#%02x%02x%02x" % tuple(
        round(ca[i] + (cb[i] - ca[i]) * t) for i in range(3))


# --------------------------------------------------------------- pure motion
def _blink_pulse(phase, duration):
    if phase < 0:
        return 0.0
    if phase < duration:
        return phase / duration
    if phase < duration * 2:
        return 1.0 - (phase - duration) / duration
    return 0.0


def blink_amount(t, period=4.8, duration=0.13):
    """0 = eyes open, 1 = closed. One blink per cycle plus a quick double."""
    first = _blink_pulse(t % period, duration)
    second = _blink_pulse((t - period * 0.55) % (period * 2.1), duration * 0.85)
    return max(0.0, min(1.0, max(first, second)))


def mouth_amount(state, t, level=0.0):
    """How open the mouth is, 0..1. Only speaking moves it."""
    if state != "speaking":
        return 0.0
    env = abs(math.sin(t * 8.5)) * (0.55 + 0.45 * abs(math.sin(t * 2.3)))
    if math.sin(t * 1.1) < -0.72:          # small pauses between phrases
        env *= 0.2
    return max(0.0, min(1.0, env))


def head_offset(t, state):
    """Tiny head drift in pixels, so she never looks frozen."""
    if state == "listening":
        return (math.sin(t * 1.3) * 1.7, -0.8 + math.sin(t * 2.1) * 0.6)
    if state == "thinking":
        return (math.sin(t * 0.55) * 2.2, -1.4 + math.sin(t * 0.9) * 0.5)
    if state == "speaking":
        return (math.sin(t * 2.7) * 1.1, math.sin(t * 4.1) * 0.9)
    return (math.sin(t * 0.65) * 1.0, math.sin(t * 1.15) * 0.7)


def gaze_offset(state, t):
    """Pupil offset as a fraction of the eye, -1..1 on each axis."""
    if state == "listening":
        return (0.0, 0.0)
    if state == "thinking":
        return (0.35, -0.45)
    if state == "speaking":
        return (0.15 * math.sin(t * 1.9), 0.08)
    return (0.22 * math.sin(t * 0.37), 0.10 * math.sin(t * 0.61))


def brow_lift(state):
    """Positive raises the brows, negative furrows them."""
    return {"listening": 1.0, "thinking": -0.55, "speaking": 0.25}.get(state, 0.0)


def eye_open(state):
    return _EYE_OPEN.get(state, 1.0)


class Avatar:
    """A small always-on-top human avatar, drawn with plain tkinter."""

    def __init__(self, root, on_open=None, on_listen=None, on_stop=None,
                 on_hide=None, size=None):
        self.root = root
        self.on_open = on_open or (lambda: None)
        self.on_listen = on_listen or (lambda: None)
        self.on_stop = on_stop or (lambda: None)
        self.on_hide = on_hide or (lambda: None)
        try:
            self.size = max(72, min(220, int(size or _UI.get("avatar_size", 118))))
        except (TypeError, ValueError):
            self.size = 118
        self.w = int(self.size + 88)
        self.h = int(self.size * 1.55) + 44
        self.state = "idle"
        self.level = 0.0
        self._lvl = 0.0
        self._line = ""
        self._line_until = 0.0
        self._drag = None
        self._moved = False
        self._closed = False
        self._t0 = time.perf_counter()
        self._last = self._t0
        self._head_off = (0.0, 0.0)
        self._body_off = 0.0

        self.top = tk.Toplevel(root)
        self.top.title("Iris")
        self.top.overrideredirect(True)
        self.top.configure(bg=TRANSPARENT)
        self.top.attributes("-topmost", True)
        try:
            self.top.attributes("-transparentcolor", TRANSPARENT)
        except tk.TclError:
            pass
        self.canvas = tk.Canvas(self.top, width=self.w, height=self.h,
                                bg=TRANSPARENT, highlightthickness=0)
        self.canvas.pack()
        self._place()
        self._draw()
        self._bind()
        self.top.after(40, self._tick)

    # ------------------------------------------------------------ placement
    def _place(self):
        pos = None
        try:
            with open(POS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            pos = (int(data["x"]), int(data["y"]))
        except Exception:  # noqa: BLE001 - first run, or a corrupt file
            pos = None
        sw = self.top.winfo_screenwidth()
        sh = self.top.winfo_screenheight()
        if pos is None:
            pos = (max(0, sw - self.w - 42),
                   max(0, sh - self.h - 120))
        pos = (max(0, min(pos[0], sw - self.w)),
               max(0, min(pos[1], sh - self.h)))
        self.top.geometry("+%d+%d" % pos)

    def _save_pos(self):
        try:
            POS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(POS_FILE, "w", encoding="utf-8") as f:
                json.dump({"x": self.top.winfo_x(), "y": self.top.winfo_y()}, f)
        except OSError:
            pass

    # --------------------------------------------------------------- drawing
    def _draw(self):
        c = self.canvas
        s = self.size
        cx = self.w / 2.0
        cy = s * 0.44 + 8
        rx = s * 0.255
        ry = s * 0.295
        self._geom = (cx, cy, rx, ry)

        c.delete("all")
        self._halo = []
        for i in range(4):
            frac = i / 3.0
            rad = s * (0.62 - 0.11 * i)
            self._halo.append(c.create_oval(
                cx - rad, cy - rad * 0.92 + ry * 0.35,
                cx + rad, cy + rad * 0.92 + ry * 0.35,
                outline=mix(BG2, ACCENT, 0.05 + 0.10 * frac),
                width=max(1, int(2 - frac))))

        # hair behind the head, then the body, then the face on top
        c.create_oval(cx - rx * 1.20, cy - ry * 1.24,
                      cx + rx * 1.20, cy + ry * 1.08,
                      fill=HAIR, outline=mix(HAIR, ACCENT, 0.30),
                      width=1, tags="head")
        for sign in (-1, 1):
            c.create_polygon(
                cx + sign * rx * 1.02, cy - ry * 0.20,
                cx + sign * rx * 1.14, cy + ry * 0.55,
                cx + sign * rx * 0.96, cy + ry * 1.30,
                cx + sign * rx * 0.52, cy + ry * 1.04,
                cx + sign * rx * 0.72, cy + ry * 0.30,
                fill=HAIR, outline="", smooth=True, tags="head")

        body_top = cy + ry + s * 0.12
        c.create_oval(cx - s * 0.46, body_top,
                      cx + s * 0.46, body_top + s * 0.64,
                      fill=mix(SURFACE, BG, 0.25),
                      outline=mix(SURFACE, ACCENT, 0.55),
                      width=max(1, int(s / 60.0)), tags="body")
        c.create_arc(cx - s * 0.26, body_top - s * 0.05,
                     cx + s * 0.26, body_top + s * 0.22,
                     start=200, extent=140, style="arc",
                     outline=mix(SURFACE, ACCENT_SOFT, 0.55),
                     width=max(1, int(s / 80.0)), tags="body")

        c.create_oval(cx - rx * 0.30, cy + ry * 0.55,
                      cx + rx * 0.30, body_top + 8,
                      fill=SKIN_SHADE, outline="", tags="head")
        c.create_oval(cx - rx, cy - ry, cx + rx, cy + ry,
                      fill=SKIN, outline=mix(SKIN_SHADE, HAIR, 0.25),
                      width=max(1, int(s / 70.0)), tags="head")
        for sign in (-1, 1):
            c.create_oval(cx + sign * rx * 1.02 - rx * 0.12,
                          cy + ry * 0.02 - ry * 0.16,
                          cx + sign * rx * 1.02 + rx * 0.12,
                          cy + ry * 0.02 + ry * 0.16,
                          fill=SKIN, outline=SKIN_SHADE, tags="head")

        # fringe and a soft highlight in the hair
        c.create_polygon(
            cx - rx * 1.02, cy - ry * 0.10,
            cx - rx * 0.92, cy - ry * 0.84,
            cx - rx * 0.25, cy - ry * 1.08,
            cx + rx * 0.45, cy - ry * 1.05,
            cx + rx * 0.96, cy - ry * 0.70,
            cx + rx * 1.02, cy - ry * 0.08,
            cx + rx * 0.55, cy - ry * 0.38,
            cx + rx * 0.10, cy - ry * 0.20,
            cx - rx * 0.30, cy - ry * 0.45,
            cx - rx * 0.65, cy - ry * 0.20,
            fill=HAIR, outline="", smooth=True, tags="head")
        c.create_arc(cx - rx * 0.78, cy - ry * 1.02,
                     cx + rx * 0.78, cy - ry * 0.08,
                     start=35, extent=110, style="arc",
                     outline=HAIR_HI, width=max(1, int(s / 60.0)), tags="head")

        # eyes
        eye_y = cy - ry * 0.02
        eye_dx = rx * 0.42
        eye_w = rx * 0.34
        eye_h = ry * 0.20
        self._eyes = []
        for sign in (-1, 1):
            ex = cx + sign * eye_dx
            white = c.create_oval(ex - eye_w / 2, eye_y - eye_h / 2,
                                  ex + eye_w / 2, eye_y + eye_h / 2,
                                  fill=EYE_WHITE,
                                  outline=mix(SKIN_SHADE, HAIR, 0.30),
                                  tags="head")
            iris = c.create_oval(ex - eye_w * 0.30, eye_y - eye_h * 0.42,
                                 ex + eye_w * 0.30, eye_y + eye_h * 0.42,
                                 fill=IRIS, outline="", tags="head")
            pupil = c.create_oval(ex - eye_w * 0.14, eye_y - eye_h * 0.26,
                                  ex + eye_w * 0.14, eye_y + eye_h * 0.26,
                                  fill=PUPIL, outline="", tags="head")
            glint = c.create_oval(ex - eye_w * 0.10, eye_y - eye_h * 0.32,
                                  ex + eye_w * 0.02, eye_y - eye_h * 0.16,
                                  fill=EYE_WHITE, outline="", tags="head")
            lash = c.create_line(ex - eye_w / 2, eye_y - eye_h * 0.30,
                                 ex, eye_y - eye_h * 0.58,
                                 ex + eye_w / 2, eye_y - eye_h * 0.30,
                                 smooth=True, fill=HAIR,
                                 width=max(1, int(s / 70.0)),
                                 capstyle="round", tags="head")
            self._eyes.append({"x": ex, "y": eye_y, "w": eye_w, "h": eye_h,
                               "white": white, "iris": iris, "pupil": pupil,
                               "glint": glint, "lash": lash})

        # brows
        self._brows = []
        for sign in (-1, 1):
            bx = cx + sign * eye_dx
            by = eye_y - ry * 0.22
            item = c.create_line(bx - rx * 0.19, by + ry * 0.02,
                                 bx, by - ry * 0.03,
                                 bx + rx * 0.19, by + ry * 0.02,
                                 smooth=True, fill=mix(HAIR, TEXT, 0.12),
                                 width=max(1, int(s / 52.0)),
                                 capstyle="round", tags="head")
            self._brows.append({"x": bx, "y": by, "item": item})

        # nose
        c.create_line(cx - rx * 0.03, cy + ry * 0.14,
                      cx + rx * 0.09, cy + ry * 0.30,
                      cx - rx * 0.02, cy + ry * 0.32,
                      smooth=True, fill=SKIN_SHADE,
                      width=max(1, int(s / 80.0)), tags="head")

        # mouth: a soft smile line and an open shape for speech
        mouth_y = cy + ry * 0.54
        self._mouth_line = c.create_line(
            cx - rx * 0.19, mouth_y - ry * 0.01,
            cx, mouth_y + ry * 0.05,
            cx + rx * 0.19, mouth_y - ry * 0.01,
            smooth=True, fill=LIPS, width=max(1, int(s / 56.0)),
            capstyle="round", tags="head")
        self._mouth_open = c.create_oval(
            cx - rx * 0.15, mouth_y - 1, cx + rx * 0.15, mouth_y + 1,
            fill=LIP_DARK, outline=LIPS, width=max(1, int(s / 90.0)),
            state="hidden", tags="head")
        self._tongue = c.create_oval(
            cx - rx * 0.08, mouth_y, cx + rx * 0.08, mouth_y + 2,
            fill=mix(LIPS, ACCENT, 0.25), outline="",
            state="hidden", tags="head")
        self._mouth = (cx, mouth_y)

        # blush
        self._blush = []
        for sign in (-1, 1):
            self._blush.append(c.create_oval(
                cx + sign * rx * 0.62 - rx * 0.19, cy + ry * 0.20,
                cx + sign * rx * 0.62 + rx * 0.19, cy + ry * 0.40,
                fill=mix(SKIN, BLUSH, 0.28), outline="", tags="head"))

        # thought dots, only while thinking
        self._thought = []
        for i in range(3):
            x = cx + rx * 1.10 + i * (s * 0.055)
            y = cy - ry * 1.28 - i * (s * 0.035)
            rad = max(1.5, s * 0.022)
            self._thought.append(c.create_oval(
                x - rad, y - rad, x + rad, y + rad,
                fill=ACCENT_SOFT, outline="", state="hidden"))

        self._caption = c.create_text(self.w / 2.0, self.h - 13, text="",
                                      fill=MUTED, width=self.w - 14,
                                      justify="center", font=("Segoe UI", 8))
        self._dot = c.create_oval(self.w - 14, 5, self.w - 6, 13,
                                  fill=ACCENT, outline="")

    def _bind(self):
        c = self.canvas
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._move)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Double-Button-1>", lambda _e: self.on_listen())
        c.bind("<Button-3>", self._menu)

    def _press(self, event):
        self._drag = (event.x_root - self.top.winfo_x(),
                      event.y_root - self.top.winfo_y())
        self._moved = False

    def _move(self, event):
        if not self._drag:
            return
        dx, dy = self._drag
        x, y = event.x_root - dx, event.y_root - dy
        if abs(x - self.top.winfo_x()) > 2 or abs(y - self.top.winfo_y()) > 2:
            self._moved = True
        self.top.geometry("+%d+%d" % (x, y))

    def _release(self, _event):
        if self._moved:
            self._save_pos()
        else:
            self.on_open()
        self._drag = None

    def _menu(self, event):
        menu = tk.Menu(self.top, tearoff=0, bg=BG2, fg=TEXT,
                       activebackground=ACCENT, activeforeground=TEXT)
        menu.add_command(label="Show Iris", command=self.on_open)
        menu.add_command(label="Listen", command=self.on_listen)
        menu.add_command(label="Stop speaking", command=self.on_stop)
        menu.add_separator()
        menu.add_command(label="Hide avatar", command=self.hide)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ------------------------------------------------------------- animation
    def _tick(self):
        if self._closed:
            return
        try:
            self._animate()
        except tk.TclError:
            return
        except Exception:  # noqa: BLE001 - the avatar must never kill the UI
            log.debug("avatar tick failed", exc_info=True)
        self.top.after(40, self._tick)

    def _animate(self):
        now = time.perf_counter()
        dt = min(0.12, now - self._last)
        self._last = now
        t = now - self._t0
        c = self.canvas
        s = self.size
        cx, cy, rx, ry = self._geom
        state = self.state
        color = _COLOR.get(state, ACCENT)
        pulse = 0.5 - 0.5 * math.cos(t * _SPEED.get(state, 1.5))
        target = self.level if state == "listening" else 0.0
        self._lvl += (target - self._lvl) * min(1.0, dt * 14)

        # halo pulse, centred a little lower than the head
        for i, item in enumerate(self._halo):
            frac = i / max(1, len(self._halo) - 1)
            rad = s * (0.62 - 0.11 * i) * (1 + 0.035 * pulse + 0.03 * self._lvl)
            c.coords(item, cx - rad, cy - rad * 0.92 + ry * 0.35,
                     cx + rad, cy + rad * 0.92 + ry * 0.35)
            c.itemconfigure(item, outline=mix(BG2, color,
                                              0.05 + 0.13 * frac + 0.10 * pulse))

        # breathing body and a drifting head
        body_dy = math.sin(t * 1.25) * 0.9 + self._lvl * 0.5
        c.move("body", 0, body_dy - self._body_off)
        self._body_off = body_dy
        hx, hy = head_offset(t, state)
        c.move("head", hx - self._head_off[0], hy - self._head_off[1])
        self._head_off = (hx, hy)

        # eyes: blink, gaze and a slight squint while speaking
        blink = blink_amount(t)
        gx, gy = gaze_offset(state, t)
        scale = max(0.04, eye_open(state) * (1.0 - blink))
        for eye in self._eyes:
            ex = eye["x"] + hx
            ey = eye["y"] + hy
            ew = eye["w"]
            eh = max(1.0, eye["h"] * scale)
            c.coords(eye["white"], ex - ew / 2, ey - eh / 2,
                     ex + ew / 2, ey + eh / 2)
            iw = ew * 0.60
            ih = max(0.8, eh * 0.84)
            ix = ex + gx * ew * 0.22
            iy = ey + gy * eh * 0.22
            c.coords(eye["iris"], ix - iw / 2, iy - ih / 2,
                     ix + iw / 2, iy + ih / 2)
            pw = max(0.8, iw * 0.46)
            ph = max(0.8, ih * 0.62)
            c.coords(eye["pupil"], ix - pw / 2, iy - ph / 2,
                     ix + pw / 2, iy + ph / 2)
            gw = max(0.6, iw * 0.30)
            gh = max(0.6, ih * 0.30)
            c.coords(eye["glint"], ix - iw * 0.26, iy - ih * 0.30,
                     ix - iw * 0.26 + gw, iy - ih * 0.30 + gh)
            c.coords(eye["lash"], ex - ew / 2, ey - eh * 0.28,
                     ex, ey - eh * 0.56,
                     ex + ew / 2, ey - eh * 0.28)

        # brows react to the state
        lift = brow_lift(state)
        for brow in self._brows:
            bx = brow["x"] + hx
            by = brow["y"] + hy - lift * ry * 0.09
            c.coords(brow["item"], bx - rx * 0.19, by + ry * 0.02,
                     bx, by - ry * 0.03 - lift * ry * 0.02,
                     bx + rx * 0.19, by + ry * 0.02)

        # mouth: smile line at rest, open oval while speaking
        env = mouth_amount(state, t, self._lvl)
        mx = self._mouth[0] + hx
        my = self._mouth[1] + hy
        if env > 0.12:
            c.itemconfigure(self._mouth_line, state="hidden")
            c.itemconfigure(self._mouth_open, state="normal")
            c.itemconfigure(self._tongue, state="normal")
            mw = rx * 0.30 * (0.75 + 0.25 * env)
            mh = max(1.5, ry * 0.17 * env)
            c.coords(self._mouth_open, mx - mw / 2, my - mh / 2,
                     mx + mw / 2, my + mh / 2)
            tw = mw * 0.55
            th = max(0.8, mh * 0.35)
            c.coords(self._tongue, mx - tw / 2, my + mh * 0.10,
                     mx + tw / 2, my + mh * 0.10 + th)
        else:
            c.itemconfigure(self._mouth_open, state="hidden")
            c.itemconfigure(self._tongue, state="hidden")
            c.itemconfigure(self._mouth_line, state="normal")
            smile = ry * (0.05 if state in ("idle", "listening") else 0.03)
            c.coords(self._mouth_line, mx - rx * 0.19, my - ry * 0.01,
                     mx, my + smile,
                     mx + rx * 0.19, my - ry * 0.01)

        # a little warmth in the cheeks while she talks or listens
        blush = 0.28 + (0.14 * env if state == "speaking" else
                        0.08 * self._lvl if state == "listening" else 0.0)
        for item in self._blush:
            c.itemconfigure(item, fill=mix(SKIN, BLUSH, blush))

        # thinking dots
        for i, item in enumerate(self._thought):
            if state == "thinking":
                rad = max(1.5, s * 0.022) * (0.75 + 0.35 * abs(
                    math.sin(t * 3.4 - i * 0.8)))
                x = cx + rx * 1.10 + i * (s * 0.055)
                y = cy - ry * 1.28 - i * (s * 0.035)
                c.coords(item, x - rad, y - rad, x + rad, y + rad)
                c.itemconfigure(item, state="normal",
                                fill=mix(ACCENT_SOFT, BG2, 0.15 * i))
            else:
                c.itemconfigure(item, state="hidden")

        caption = self._line if now < self._line_until else ""
        c.itemconfigure(self._caption, text=caption,
                        fill=TEXT if caption else MUTED)
        c.itemconfigure(self._dot, fill=color)

    # ------------------------------------------------------------------ api
    def set_state(self, state):
        self.state = str(state or "idle")

    def set_level(self, level):
        try:
            self.level = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            pass

    def say(self, text, seconds=5.0):
        line = " ".join(str(text or "").split())
        if not line:
            return
        self._line = line[:110] + ("..." if len(line) > 110 else "")
        self._line_until = time.perf_counter() + max(1.0, seconds)

    def hide(self):
        try:
            self.top.withdraw()
        except tk.TclError:
            pass
        self.on_hide()

    def show(self):
        try:
            self.top.deiconify()
            self.top.lift()
        except tk.TclError:
            pass

    def visible(self):
        try:
            return bool(self.top.winfo_viewable())
        except tk.TclError:
            return False

    def close(self):
        self._closed = True
        try:
            self.top.destroy()
        except tk.TclError:
            pass

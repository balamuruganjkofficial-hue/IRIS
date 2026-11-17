"""Iris' interface: a frameless, animated dark sharp-red window built with
tkinter (no extra dependencies). The assistant runs on its own thread; this file
only paints, animates and forwards input through a thread-safe queue.

Everything Iris opens - web pages, images, documents, folders, dashboards and
even whole applications - is drawn inside the ``surface`` panel on the right,
so no separate window ever steals focus.
"""

import ctypes
import logging
import math
import os
import queue
import random
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

if __package__ in (None, ""):          # allow `python interfaces/ui.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import bus
from core.config import CFG

_UI = CFG.get("ui") or {}

BG1 = _UI.get("bg", "#060407")
BG2 = _UI.get("bg2", "#12060A")
SURFACE = _UI.get("surface", "#1A070C")
SURFACE_HI = _UI.get("surface_hi", "#2A0A12")
ACCENT = _UI.get("accent", "#FF1F3D")
ACCENT_SOFT = _UI.get("accent_soft", "#FF5C73")
TEXT = _UI.get("text", "#F7ECEF")
MUTED = _UI.get("muted", "#A07A82")
USER_BG = _UI.get("user_bubble", "#4A0818")
IRIS_BG = _UI.get("iris_bubble", "#1A070C")
FPS = max(15, min(60, int(_UI.get("fps", 30))))
WIN_W = int(_UI.get("width", 1020))
WIN_H = int(_UI.get("height", 700))
CYBER = bool(_UI.get("cyber", True))
WAKE = str(CFG["voice"].get("wake_word", "iris")).replace("_", " ")
SURFACE_CFG = CFG.get("surface") or {}

_SPEED = {"idle": 1.4, "listening": 2.6, "thinking": 3.4, "speaking": 4.6}
_PEAK = {"idle": 0.15, "listening": 0.27, "thinking": 0.23, "speaking": 0.33}


# ------------------------------------------------------------------ helpers
def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    t = max(0.0, min(1.0, t))
    ca, cb = _rgb(a), _rgb(b)
    return "#%02x%02x%02x" % tuple(round(ca[i] + (cb[i] - ca[i]) * t) for i in range(3))


def _round_points(x1, y1, x2, y2, r=14):
    return [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1,
            x2, y1 + r, x2, y1 + r, x2, y2 - r, x2, y2 - r, x2, y2,
            x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2, x1, y2,
            x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1]


def _round_rect(canvas, x1, y1, x2, y2, r=14, **kw):
    return canvas.create_polygon(_round_points(x1, y1, x2, y2, r),
                                 smooth=True, **kw)


def _wrap(font, text, max_px):
    lines, cur = [], ""
    for word in (text or "").split():
        probe = word if not cur else cur + " " + word
        if font.measure(probe) <= max_px or not cur:
            cur = probe
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def _size_text(size):
    try:
        size = float(size or 0)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return ("{0:.0f} {1}".format(size, unit) if unit == "B"
                    else "{0:.1f} {1}".format(size, unit))
        size /= 1024.0
    return ""


class RoundButton(tk.Canvas):
    """A flat, rounded button drawn on a canvas with a hover state."""

    def __init__(self, master, text="", command=None, width=92, height=38,
                 radius=19, fill=SURFACE, hover=SURFACE_HI, fg=TEXT,
                 font=("Segoe UI", 10), outline=""):
        bg = master.cget("bg")
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self._fill, self._hover = fill, hover
        self._shape = _round_rect(self, 1, 1, width - 2, height - 2, radius,
                                  fill=fill, outline=outline or fill)
        self._label = self.create_text(width / 2, height / 2, text=text, fill=fg, font=font)
        self.bind("<Enter>", lambda e: self._paint(self._hover))
        self.bind("<Leave>", lambda e: self._paint(self._fill))
        self.bind("<ButtonPress-1>", lambda e: self._paint(self._hover))
        self.bind("<ButtonRelease-1>", self._click)

    def _paint(self, color):
        self.itemconfigure(self._shape, fill=color, outline=color)

    def _click(self, _event):
        self._paint(self._hover)
        if self.command:
            self.command()


class MicButton(tk.Canvas):
    """A circle with a hand-drawn microphone; no emoji fonts involved."""

    def __init__(self, master, command=None, size=42):
        super().__init__(master, width=size, height=size, bg=master.cget("bg"),
                         highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self.s = size
        self._hot = False
        self._shape = self.create_oval(1, 1, size - 1, size - 1,
                                       fill=SURFACE, outline=SURFACE_HI, width=1)
        self._mic = []
        self.bind("<Enter>", lambda e: self._paint(True))
        self.bind("<Leave>", lambda e: self._paint(False))
        self.bind("<ButtonRelease-1>", self._click)
        self._draw_mic()

    def _draw_mic(self):
        c, s = self, self.s
        x, y = s / 2, s / 2
        for item in self._mic:
            c.delete(item)
        self._mic = [
            c.create_oval(x - 6, y - 11, x + 6, y + 1, outline=ACCENT, width=2),
            c.create_arc(x - 10, y - 6, x + 10, y + 9, start=200, extent=140,
                         style="arc", outline=ACCENT, width=2),
            c.create_line(x, y + 8, x, y + 13, fill=ACCENT, width=2),
            c.create_line(x - 6, y + 13, x + 6, y + 13, fill=ACCENT, width=2),
        ]

    def _paint(self, hot):
        self._hot = hot
        self.itemconfigure(self._shape, fill=SURFACE_HI if hot else SURFACE,
                           outline=ACCENT if hot else SURFACE_HI)

    def _click(self, _event):
        if self.command:
            self.command()


class Bubble(tk.Canvas):
    """A rounded chat bubble whose height follows its wrapped text."""

    def __init__(self, master, text, mine, font, max_width, bg=BG2):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0)
        self.mine = mine
        self.font = font
        self.max_width = max(160, max_width)
        self.text = ""
        self.set_text(text)

    def set_text(self, text):
        self.text = text or ""
        self.delete("all")
        pad_x, pad_y = 14, 9
        lines = _wrap(self.font, self.text, self.max_width - 2 * pad_x)
        line_h = self.font.metrics("linespace")
        width = min(self.max_width,
                    max(self.font.measure(line) for line in lines) + 2 * pad_x)
        height = len(lines) * line_h + 2 * pad_y + (len(lines) - 1) * 2
        self.configure(width=width, height=height)
        fill = USER_BG if self.mine else IRIS_BG
        _round_rect(self, 0, 0, width, height, r=13, fill=fill, outline=fill)
        self.create_text(pad_x, pad_y, anchor="nw", text="\n".join(lines),
                         font=self.font, fill=TEXT, justify="left")


# ------------------------------------------------------------------ window
class IrisUI:
    def __init__(self, send=None, stop=None, on_close=None, open_url=None,
                 browser_cmd=None, open_path=None, window_cmd=None):
        self.send = send or (lambda text: None)
        self.stop_cb = stop or (lambda: None)
        self.on_close = on_close or self.post_close
        self.open_url = open_url or (lambda url: None)
        self.browser_cmd = browser_cmd or (lambda action, **kw: None)
        self.open_path = open_path or (lambda target: None)
        self.window_cmd = window_cmd or (lambda action, **kw: None)

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

        self.root = tk.Tk()
        self.root.title("Iris")
        self.root.configure(bg=BG1)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", bool(_UI.get("always_on_top", False)))

        self.s = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        w, h = int(WIN_W * self.s), int(WIN_H * self.s)
        self.root.geometry("%dx%d+%d+%d" % (
            w, h,
            max(0, (self.root.winfo_screenwidth() - w) // 2),
            max(0, (self.root.winfo_screenheight() - h) // 2 - 20 * self.s)))
        self.root.minsize(int(600 * self.s), int(480 * self.s))

        self._q = queue.Queue()
        self._uiq = queue.Queue()
        self._closed = False
        self._dialog = None
        self._drag = None
        self._resize = None

        self.state = "idle"
        self.level = 0.0
        self._lvl = 0.0
        self._barv = [0.0] * 28
        self._tool = ""
        self._toast = ""
        self._toast_until = 0.0
        self._latency = 0.0
        self._t0 = time.perf_counter()
        self._last = self._t0
        self._geom = (0, 0)
        self._resize_due = 0.0
        self._log = []
        self._bubbles = []
        self._stream_bubble = None
        self._stream_idx = None
        self._relayout_job = None

        # cyber / intelligence panels
        self._auth = None
        self._auth_until = 0.0
        self._view = None
        self._results = None
        self._surface = None
        self._surface_live = None
        self._surface_frame_size = None
        self._surface_src_size = None
        self._surface_img_label = None
        self._surface_ratio = float(SURFACE_CFG.get(
            "ratio", _UI.get("surface_ratio", 0.70)) or 0.70)
        self._slide = None
        self._intel_on = False
        self._img_ref = None
        self._orig_img = None
        self._panel_w = 380
        self._panel_h = 460
        self._panel_key = None
        self._intel_render_job = None
        self._rain = []
        self._scan = 0.0

        # fonts
        self.f_body = tkfont.Font(family="Segoe UI", size=10)
        self.f_ui = tkfont.Font(family="Segoe UI", size=10)
        self.f_small = tkfont.Font(family="Segoe UI", size=9)
        self.f_bold = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.f_name = tkfont.Font(family="Segoe UI", size=64, weight="bold")
        self.f_hud = tkfont.Font(family="Consolas", size=7)
        self.f_hud_big = tkfont.Font(family="Consolas", size=9, weight="bold")

        self._build()
        self._bind_bus()

    # ---------------------------------------------------------- construction
    def _build(self):
        s = self.s
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=1)

        # title bar ---------------------------------------------------------
        self.topbar = tk.Frame(self.root, bg=BG1, height=int(46 * s))
        self.topbar.grid(row=0, column=0, sticky="ew")
        self.topbar.pack_propagate(False)
        self.topbar.grid_propagate(False)
        self.topbar.grid_columnconfigure(1, weight=1)

        self.dot = tk.Canvas(self.topbar, width=int(10 * s), height=int(10 * s),
                             bg=BG1, highlightthickness=0)
        self.dot.grid(row=0, column=0, padx=(22 * s, 8 * s))
        self._dot_item = self.dot.create_oval(1, 1, 9 * s, 9 * s, fill=MUTED, outline="")

        wordmark = tk.Label(self.topbar, text="I R I S", bg=BG1, fg=MUTED,
                            font=("Segoe UI Semibold", 9))
        wordmark.grid(row=0, column=1, sticky="w")

        buttons = tk.Frame(self.topbar, bg=BG1)
        buttons.grid(row=0, column=2, sticky="e", padx=(0, 12 * s))
        for text, cmd, hover in (("–", self._minimise, SURFACE_HI),
                                 ("▢", self._maximise, SURFACE_HI),
                                 ("✕", self._close, "#7A1330")):
            b = tk.Label(buttons, text=text, bg=BG1, fg=MUTED,
                         font=("Segoe UI", 10), padx=int(9 * s), pady=int(4 * s),
                         cursor="hand2")
            b.pack(side="left", padx=1)
            b.bind("<Enter>", lambda e, w=b, c=hover: w.configure(bg=c, fg=TEXT))
            b.bind("<Leave>", lambda e, w=b: w.configure(bg=BG1, fg=MUTED))
            b.bind("<ButtonRelease-1>", lambda e, c=cmd: c())

        for widget in (self.topbar, wordmark):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<Double-Button-1>", lambda e: self._maximise())

        # stage -------------------------------------------------------------
        self.stage = tk.Canvas(self.root, bg=BG1, highlightthickness=0, bd=0)
        self.stage.grid(row=1, column=0, sticky="nsew")
        self._build_stage()
        self._build_intel()
        self.stage.bind("<Configure>", self._on_stage_configure)

        # panel -------------------------------------------------------------
        self.panel = tk.Frame(self.root, bg=BG2)
        self.panel.grid(row=2, column=0, sticky="ew")

        self.tc = tk.Canvas(self.panel, bg=BG2, highlightthickness=0, bd=0,
                            height=int(138 * s))
        self.tc.pack(fill="x")
        self.tf = tk.Frame(self.tc, bg=BG2)
        self._tc_window = self.tc.create_window((0, 0), window=self.tf, anchor="nw")
        self.tf.bind("<Configure>", self._on_transcript_configure)
        self.tc.bind("<Configure>", self._on_transcript_resize)
        self.tc.bind("<MouseWheel>", self._on_wheel)
        self.tf.bind("<MouseWheel>", self._on_wheel)

        inputrow = tk.Frame(self.panel, bg=BG2)
        inputrow.pack(fill="x", padx=int(18 * s), pady=(2 * s, int(14 * s)))
        inputrow.grid_columnconfigure(1, weight=1)

        self.mic = MicButton(inputrow, command=lambda: self.send(""),
                             size=int(42 * s))
        self.mic.grid(row=0, column=0, padx=(0, int(10 * s)))

        self.entry_wrap = tk.Frame(inputrow, bg=SURFACE, highlightthickness=1,
                                   highlightbackground=SURFACE_HI)
        self.entry_wrap.grid(row=0, column=1, sticky="ew")
        self.entry = tk.Entry(self.entry_wrap, bg=SURFACE, fg=MUTED, relief="flat",
                              bd=0, insertbackground=ACCENT,
                              font=("Segoe UI", 11))
        self.entry.pack(fill="x", padx=int(13 * s), pady=int(10 * s))
        self._placeholder_on = True
        self.entry.insert(0, "Type a message…")
        self.entry.bind("<FocusIn>", self._focus_in)
        self.entry.bind("<FocusOut>", self._focus_out)
        self.entry.bind("<Return>", lambda e: self._send())
        self.entry.bind("<Escape>", lambda e: self.stop_cb())
        self.entry_wrap.bind("<Button-1>", lambda e: self.entry.focus_set())

        self.send_btn = RoundButton(inputrow, text="Send", command=self._send,
                                    width=int(88 * s), height=int(38 * s),
                                    radius=int(19 * s), fill=ACCENT,
                                    hover=ACCENT_SOFT, fg="#1A0410",
                                    font=("Segoe UI Semibold", 10))
        self.send_btn.grid(row=0, column=2, padx=(int(10 * s), 0))

        # resize grip -------------------------------------------------------
        self.grip = tk.Canvas(self.panel, width=int(16 * s), height=int(16 * s),
                              bg=BG2, highlightthickness=0, cursor="bottom_right_corner")
        self.grip.place(relx=1.0, rely=1.0, anchor="se")
        for off in (5, 9, 13):
            self.grip.create_line(off * s, 16 * s, 16 * s, off * s,
                                  fill=mix(MUTED, BG2, 0.45), width=1)
        self.grip.bind("<ButtonPress-1>", self._resize_start)
        self.grip.bind("<B1-Motion>", self._resize_move)

        self.root.bind("<Escape>", lambda e: self.stop_cb())
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build_stage(self):
        c = self.stage
        self._glow = [c.create_oval(0, 0, 0, 0, outline="", fill=BG2) for _ in range(12)]
        self._ripples = [c.create_oval(0, 0, 0, 0, outline=ACCENT, width=1,
                                       state="hidden") for _ in range(3)]
        self._arcs = [
            c.create_arc(0, 0, 0, 0, start=0, extent=150, style="arc",
                         outline=ACCENT, width=2),
            c.create_arc(0, 0, 0, 0, start=180, extent=55, style="arc",
                         outline=mix(ACCENT, BG2, 0.55), width=1),
        ]
        self._particles = [c.create_oval(0, 0, 0, 0, outline="", fill=ACCENT)
                           for _ in range(24)]
        self._ticks = [c.create_line(0, 0, 0, 0, fill=mix(ACCENT, BG2, 0.62))
                       for _ in range(48)]
        self._rain = [c.create_text(0, 0, text="", font=self.f_hud,
                                    fill=mix(ACCENT_SOFT, BG2, 0.72),
                                    anchor="n") for _ in range(16)]
        self._rain_state = [{"x": random.random(), "y": random.random(),
                             "v": 0.05 + random.random() * 0.09}
                            for _ in self._rain]
        self._corner = [c.create_line(0, 0, 0, 0, fill=mix(ACCENT, BG2, 0.35),
                                      width=2) for _ in range(8)]
        self._scan = c.create_line(0, 0, 0, 0, fill=mix(ACCENT, BG2, 0.72), width=1)
        self._scan2 = c.create_line(0, 0, 0, 0, fill=mix(ACCENT_SOFT, BG1, 0.86),
                                    width=1)
        self._tele_tl = c.create_text(0, 0, text="", font=self.f_hud,
                                      fill=mix(ACCENT_SOFT, BG2, 0.45), anchor="nw")
        self._tele_tr = c.create_text(0, 0, text="", font=self.f_hud,
                                      fill=mix(ACCENT_SOFT, BG2, 0.45), anchor="ne")
        self._tele_bl = c.create_text(0, 0, text="", font=self.f_hud,
                                      fill=mix(ACCENT_SOFT, BG2, 0.50), anchor="sw")
        self._letters_shadow = [c.create_text(0, 0, text=ch, font=self.f_name,
                                              fill="#2A0A14") for ch in "IRIS"]
        self._letters = [c.create_text(0, 0, text=ch, font=self.f_name,
                                       fill=TEXT) for ch in "IRIS"]
        self._status = c.create_text(0, 0, text="", font=self.f_ui, fill=MUTED)
        self._bars = [c.create_line(0, 0, 0, 0, fill=ACCENT, width=2,
                                    capstyle="round") for _ in range(28)]
        self._hint = c.create_text(0, 0, text="", font=self.f_small,
                                   fill=mix(MUTED, BG2, 0.25))
        self._latency = c.create_text(0, 0, text="", font=self.f_small,
                                      fill=mix(MUTED, BG2, 0.15), anchor="ne")
        # voice-permission HUD
        self._auth_box = _round_rect(c, 0, 0, 0, 0, r=10, fill=SURFACE,
                                     outline=mix(ACCENT, BG2, 0.35),
                                     state="hidden")
        self._auth_title = c.create_text(0, 0, text="", font=self.f_hud_big,
                                         fill=ACCENT_SOFT, state="hidden")
        self._auth_q = c.create_text(0, 0, text="", font=self.f_ui, fill=TEXT,
                                     width=300, justify="center", state="hidden")
        self._auth_hint = c.create_text(0, 0, text="", font=self.f_hud,
                                        fill=MUTED, state="hidden")
        self._auth_items = (self._auth_box, self._auth_title, self._auth_q,
                            self._auth_hint)
        if not CYBER:
            for item in (self._ticks + self._rain + self._corner +
                         [self._scan, self._scan2, self._tele_tl,
                          self._tele_tr, self._tele_bl]):
                c.itemconfigure(item, state="hidden")
        self._letter_x = [0.0] * 4
        self._r = 100.0
        self._cx = self._cy = 0.0
        self._bar_y = 100.0
        self._bar_span = 200.0
        self._palette()


    # --------------------------------------------------------------- surface
    def _build_intel(self):
        s = self.s
        self.intel = tk.Frame(self.stage, bg=BG2, highlightthickness=1,
                              highlightbackground=mix(ACCENT, BG2, 0.45))
        head = tk.Frame(self.intel, bg=BG2)
        head.pack(fill="x", padx=int(10 * s), pady=(int(7 * s), 0))
        self.intel_tag = tk.Label(head, text="SURFACE", bg=BG2, fg=ACCENT_SOFT,
                                  font=("Consolas", 9, "bold"), anchor="w")
        self.intel_tag.pack(side="left")
        self.intel_mode = tk.Label(head, text="", bg=BG2, fg=mix(ACCENT_SOFT, BG2, 0.35),
                                   font=("Consolas", 7), anchor="w")
        self.intel_mode.pack(side="left", padx=(int(8 * s), 0))
        close = tk.Label(head, text="×", bg=BG2, fg=MUTED,
                         font=("Segoe UI", 12), cursor="hand2")
        close.pack(side="right")
        close.bind("<Enter>", lambda e: close.configure(fg=ACCENT))
        close.bind("<Leave>", lambda e: close.configure(fg=MUTED))
        close.bind("<ButtonRelease-1>", lambda e: self._close_surface())
        self.intel_sub = tk.Label(self.intel, text="", bg=BG2, fg=MUTED,
                                  font=("Segoe UI", 8), anchor="w", justify="left")
        self.intel_sub.pack(fill="x", padx=int(11 * s))

        # browser transport -------------------------------------------------
        self.intel_bar = tk.Frame(self.intel, bg=BG2)
        for text, action in (("‹", "back"), ("›", "forward"),
                             ("⟳", "reload"), ("⌂", "home")):
            b = tk.Label(self.intel_bar, text=text, bg=SURFACE, fg=MUTED,
                         font=("Segoe UI", 10), padx=int(6 * s),
                         cursor="hand2")
            b.pack(side="left", padx=(0, int(3 * s)))
            b.bind("<Enter>", lambda e, w=b: w.configure(bg=SURFACE_HI, fg=TEXT))
            b.bind("<Leave>", lambda e, w=b: w.configure(bg=SURFACE, fg=MUTED))
            b.bind("<ButtonRelease-1>",
                   lambda e, a=action: self.browser_cmd(a))
        self.addr = tk.Entry(self.intel_bar, bg=SURFACE, fg=TEXT, relief="flat",
                             bd=0, insertbackground=ACCENT, font=("Consolas", 8))
        self.addr.pack(side="left", fill="x", expand=True, padx=(int(3 * s), 0),
                       ipady=int(4 * s))
        self.addr.bind("<Return>", self._go_addr)
        self.addr.bind("<FocusIn>",
                       lambda e: self.addr.configure(bg=SURFACE_HI))
        self.addr.bind("<FocusOut>",
                       lambda e: self.addr.configure(bg=SURFACE))

        self.intel_body = tk.Frame(self.intel, bg=BG2)
        self.intel_body.pack(fill="both", expand=True, padx=int(7 * s),
                             pady=int(7 * s))
        self._intel_item = self.stage.create_window(
            0, 0, window=self.intel, anchor="nw", width=1, height=1,
            state="hidden")

    def _show_bar(self, show):
        if show:
            if not self.intel_bar.winfo_manager():
                self.intel_bar.pack(fill="x", padx=int(9 * self.s),
                                    pady=(int(5 * self.s), 0),
                                    before=self.intel_body)
        elif self.intel_bar.winfo_manager():
            self.intel_bar.pack_forget()

    def _layout_intel(self, w, h):
        if w < 40 or h < 40:
            return
        s = self.s
        if self._surface is not None:
            width = int(min(max(w * self._surface_ratio, 340 * s),
                             max(340 * s, w - 250 * s)))
            self._panel_w = width
            self._panel_h = int(max(260 * s, h - 28 * s))
        else:
            self._panel_w = int(min(max(w * 0.43, 290 * s), 620 * s))
            self._panel_h = int(min(max(h * 0.76, 240 * s), 580 * s))
        if self._intel_on:
            self._place_intel(w, h)
            self.stage.itemconfigure(self._intel_item, width=self._panel_w,
                                     height=self._panel_h, state="normal")
        else:
            self.stage.itemconfigure(self._intel_item, state="hidden")
        key = (self._panel_w, self._panel_h)
        if key != self._panel_key:
            self._panel_key = key
            if self._intel_on:
                if self._intel_render_job is not None:
                    try:
                        self.root.after_cancel(self._intel_render_job)
                    except tk.TclError:
                        pass
                self._intel_render_job = self.root.after(140, self._render_surface)

    def _place_intel(self, w=None, h=None, slide=False):
        s = self.s
        w = w if w is not None else self.stage.winfo_width()
        x = max(int(10 * s), w - self._panel_w - int(14 * s))
        y = int(14 * s)
        if slide:
            self._slide = {"t0": time.perf_counter(), "x": x, "y": y}
        else:
            self._slide = None
            self.stage.coords(self._intel_item, x, y)

    # ---------------------------------------------------- publishing helpers
    def _show_view(self, payload):
        payload = payload or {}
        if str(payload.get("state", "")) == "closed":
            self._close_surface()
            return
        view = dict(payload)
        view.setdefault("kind", "browser")
        view.setdefault("id", "browser")
        self._show_surface(view)

    def _show_results(self, payload):
        data = dict(payload or {})
        data["kind"] = "results"
        data["id"] = "results"
        self._show_surface(data)

    def _show_surface(self, payload):
        payload = payload or {}
        if str(payload.get("state", "")) in ("closed", "close"):
            self._close_surface()
            return
        self._surface = payload
        self._surface_live = None
        self._view = None
        self._results = None
        self._intel_on = True
        if str(payload.get("kind")) not in ("app", "screen"):
            try:
                from skills import capture
                capture.stop()
            except Exception:  # noqa: BLE001
                pass
        self._panel_key = None
        w, h = self.stage.winfo_width(), self.stage.winfo_height()
        self._layout_stage(w, h)
        self._place_intel(w, h, slide=True)
        self._render_surface()
        self._palette()

    def _close_surface(self):
        self._surface = None
        self._surface_live = None
        self._view = None
        self._results = None
        self._hide_intel()
        try:
            from skills import capture
            capture.stop()
        except Exception:  # noqa: BLE001
            pass

    def _hide_intel(self):
        self._intel_on = False
        self._surface_img_label = None
        self.stage.itemconfigure(self._intel_item, state="hidden")
        self._layout_stage(self.stage.winfo_width(), self.stage.winfo_height())

    def _clear_intel_body(self):
        self._surface_img_label = None
        for child in self.intel_body.winfo_children():
            child.destroy()

    def _render_surface(self):
        if self._intel_render_job is not None:
            try:
                self.root.after_cancel(self._intel_render_job)
            except tk.TclError:
                pass
            self._intel_render_job = None
        if not self._intel_on:
            return
        if self._surface is None:
            self._hide_intel()
            return
        surf = self._surface
        kind = str(surf.get("kind") or "info")
        self._clear_intel_body()
        tags = {"browser": "SURFACE // BROWSER", "image": "SURFACE // IMAGE",
                "text": "SURFACE // DOCUMENT", "folder": "SURFACE // FILES",
                "dashboard": "SURFACE // TELEMETRY", "results": "SURFACE // INTEL",
                "app": "SURFACE // APPLICATION", "screen": "SURFACE // DESKTOP",
                "info": "SURFACE // STATUS"}
        self.intel_tag.configure(text=tags.get(kind, "SURFACE"))
        live = kind in ("browser", "app", "screen")
        self.intel_mode.configure(
            text="LIVE" if live else kind.upper()[:9])
        sub = surf.get("title") or ""
        extra = surf.get("url") or surf.get("subtitle") or ""
        if extra and extra != sub:
            sub = (sub + "  ·  " + extra) if sub else extra
        self.intel_sub.configure(text=sub[:160])
        self.intel_sub.configure(
            wraplength=max(120, self._panel_w - int(26 * self.s)))
        self._show_bar(kind == "browser")
        if kind == "browser":
            url = str(surf.get("url") or "")
            if url and self.addr.get() != url:
                self.addr.delete(0, "end")
                self.addr.insert(0, url)
        if kind in ("browser", "image", "app", "screen"):
            self._render_image_surface(surf)
        elif kind == "text":
            self._render_text_surface(surf)
        elif kind == "folder":
            self._render_folder_surface(surf)
        elif kind == "dashboard":
            self._render_dashboard(surf)
        elif kind == "results":
            self._render_results()
        else:
            self._render_info(surf)

    # ------------------------------------------------------------- renderers
    def _render_image_surface(self, surf):
        s = self.s
        kind = str(surf.get("kind") or "")
        path = surf.get("path") or ""
        max_w = max(80, self._panel_w - int(26 * s))
        max_h = max(60, self._panel_h - int(104 * s))
        if not path:
            text = surf.get("text") or "Connecting to the live feed…"
            label = tk.Label(self.intel_body, text=text, bg=BG2, fg=MUTED,
                             font=("Consolas", 9), justify="center",
                             cursor="hand2",
                             wraplength=max(120, self._panel_w - int(40 * s)))
            label.pack(expand=True, fill="both")
            if kind in ("app", "screen"):
                self._surface_img_label = label
                hwnd = int(surf.get("hwnd") or 0)
                if hwnd:
                    label.bind("<ButtonRelease-1>",
                               lambda e, h=hwnd: self._window_click(e, h))
                    label.bind("<MouseWheel>",
                               lambda e, h=hwnd: self._window_wheel(e, h))
                    label.bind("<KeyPress>",
                               lambda e, h=hwnd: self._window_key(e, h))
                self._request_frame_refresh(surf)
            return
        try:
            from PIL import Image, ImageTk
            img = Image.open(path)
            img.load()
            self._orig_img = img.copy()
            self._surface_src_size = (img.width, img.height)
            img.thumbnail((max_w, max_h), Image.LANCZOS)
            self._img_ref = ImageTk.PhotoImage(img)
        except Exception:  # noqa: BLE001 - no Pillow, missing file, bad JPEG
            self._img_ref = None
            tk.Label(self.intel_body,
                     text="LIVE FEED UNAVAILABLE\n\nAsk me to try again.",
                     bg=BG2, fg=MUTED, font=("Consolas", 9),
                     justify="center").pack(expand=True)
            return
        holder = tk.Frame(self.intel_body, bg=BG1, cursor="hand2")
        holder.pack(expand=True)
        label = tk.Label(holder, image=self._img_ref, bg=BG1, bd=0,
                         cursor="hand2", takefocus=1)
        label.pack()
        self._surface_img_label = label
        self._surface_frame_size = (self._img_ref.width(), self._img_ref.height())
        if kind == "browser":
            label.bind("<ButtonRelease-1>", self._browser_click)
            label.bind("<MouseWheel>", self._browser_wheel)
            self.intel_sub.configure(text=(surf.get("title") or
                                          surf.get("url") or "")[:160])
        elif kind in ("app", "screen"):
            hwnd = int(surf.get("hwnd") or 0)
            if hwnd:
                label.bind("<ButtonRelease-1>",
                           lambda e, h=hwnd: self._window_click(e, h))
                label.bind("<MouseWheel>",
                           lambda e, h=hwnd: self._window_wheel(e, h))
                label.bind("<KeyPress>",
                           lambda e, h=hwnd: self._window_key(e, h))
        if kind in ("app", "screen"):
            self._request_frame_refresh(surf)

    def _request_frame_refresh(self, surf):
        """Ask for a fresh live frame until one arrives."""
        if surf.get("live", True) is False:
            return
        if str(surf.get("kind")) == "screen":
            try:
                from skills import capture
                capture.start_screen()
            except Exception:  # noqa: BLE001
                pass
        elif surf.get("hwnd"):
            try:
                from skills import capture
                capture.start_window(surf["hwnd"], surf.get("title", ""))
            except Exception:  # noqa: BLE001
                pass

    def _browser_click(self, event):
        surf = self._surface or {}
        vw, vh = int(surf.get("vw") or 0), int(surf.get("vh") or 0)
        label = self._surface_img_label
        if not vw or not vh or label is None:
            return
        x = event.x * vw / max(1, label.winfo_width())
        y = event.y * vh / max(1, label.winfo_height())
        self.browser_cmd("click_at", x=x, y=y)

    def _browser_wheel(self, event):
        surf = self._surface or {}
        vw = int(surf.get("vw") or 0)
        label = self._surface_img_label
        if not vw or label is None:
            return
        x = event.x * vw / max(1, label.winfo_width())
        self.browser_cmd("scroll_at", x=x, y=0, dy=event.delta)

    def _window_click(self, event, hwnd):
        label = self._surface_img_label
        if label is None:
            return
        label.focus_set()
        src = self._surface_live or {}
        w = int(src.get("w") or 0) or (self._surface_src_size or (0, 0))[0]
        h = int(src.get("h") or 0) or (self._surface_src_size or (0, 0))[1]
        if not w or not h:
            return
        x = event.x * w / max(1, label.winfo_width())
        y = event.y * h / max(1, label.winfo_height())
        self.window_cmd("click", hwnd=hwnd, x=x, y=y)

    def _window_wheel(self, event, hwnd):
        label = self._surface_img_label
        if label is None:
            return
        src = self._surface_live or {}
        w = int(src.get("w") or 0) or (self._surface_src_size or (0, 0))[0]
        h = int(src.get("h") or 0) or (self._surface_src_size or (0, 0))[1]
        if not w or not h:
            return
        x = event.x * w / max(1, label.winfo_width())
        y = event.y * h / max(1, label.winfo_height())
        self.window_cmd("scroll", hwnd=hwnd, x=x, y=y, dy=event.delta)

    def _window_key(self, event, hwnd):
        self.window_cmd("key", hwnd=hwnd, key=event.keysym)

    def _go_addr(self, _event=None):
        text = self.addr.get().strip()
        if not text:
            return
        if "." in text and " " not in text:
            if not text.startswith(("http://", "https://")):
                text = "https://" + text
            self.browser_cmd("open", url=text)
        else:
            self.browser_cmd("search", query=text)
        try:
            self.intel.focus_set()
        except tk.TclError:
            pass

    def _render_text_surface(self, surf):
        s = self.s
        box = tk.Frame(self.intel_body, bg=BG1)
        box.pack(fill="both", expand=True)
        text = tk.Text(box, bg=BG1, fg=TEXT, insertbackground=ACCENT, bd=0,
                       wrap="word", font=("Consolas", 9), padx=int(10 * s),
                       pady=int(8 * s), relief="flat",
                       selectbackground=ACCENT, selectforeground=BG1,
                       highlightthickness=0, spacing1=1, spacing3=1)
        bar = tk.Scrollbar(box, command=text.yview, bg=BG2, troughcolor=BG1,
                           activebackground=ACCENT, width=int(9 * s), bd=0,
                           relief="flat", highlightthickness=0)
        text.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        text.insert("1.0", str(surf.get("text") or ""))
        text.configure(state="disabled")
        text.bind("<MouseWheel>",
                  lambda e: text.yview_scroll(int(-e.delta / 40), "units"))

    def _render_folder_surface(self, surf):
        s = self.s
        outer = tk.Canvas(self.intel_body, bg=BG2, highlightthickness=0, bd=0)
        inner = tk.Frame(outer, bg=BG2)
        window = outer.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: outer.configure(scrollregion=outer.bbox("all")))
        outer.bind("<Configure>",
                   lambda e: outer.itemconfigure(window, width=e.width))
        outer.pack(fill="both", expand=True)
        wheel = lambda e: outer.yview_scroll(int(-e.delta / 40), "units")
        outer.bind("<MouseWheel>", wheel)
        items = surf.get("items") or []
        if not items:
            tk.Label(inner, text="EMPTY FOLDER", bg=BG2, fg=MUTED,
                     font=("Consolas", 9)).pack(pady=int(20 * s))
        for item in items:
            is_dir = item.get("kind") == "dir"
            row = tk.Frame(inner, bg=BG1, cursor="hand2")
            row.pack(fill="x", padx=1, pady=(0, int(3 * s)))
            glyph = "▸" if is_dir else "·"
            icon = tk.Label(row, text=glyph, bg=BG1,
                            fg=ACCENT if is_dir else MUTED,
                            font=("Consolas", 10), width=2, cursor="hand2")
            icon.pack(side="left", padx=(int(4 * s), 0))
            name = tk.Label(row, text=item.get("name", ""), bg=BG1, fg=TEXT,
                            font=("Segoe UI", 9), anchor="w", cursor="hand2")
            name.pack(side="left", fill="x", expand=True)
            meta = "DIR" if is_dir else _size_text(item.get("size", 0))
            extra = tk.Label(row, text=meta, bg=BG1, fg=MUTED,
                             font=("Consolas", 7), cursor="hand2")
            extra.pack(side="right", padx=(0, int(6 * s)))
            path = item.get("path", "")

            def open_row(_event=None, p=path):
                self.open_path(p)

            for widget in (row, icon, name, extra):
                widget.bind("<ButtonRelease-1>", open_row)
                widget.bind("<MouseWheel>", wheel)
                widget.bind("<Enter>",
                            lambda e, w=row: self._tint_row(w, True))
                widget.bind("<Leave>",
                            lambda e, w=row: self._tint_row(w, False))

    def _tint_row(self, row, hot):
        color = SURFACE if hot else BG1
        try:
            row.configure(bg=color)
            for child in row.winfo_children():
                child.configure(bg=color)
        except tk.TclError:
            pass

    def _render_dashboard(self, surf):
        s = self.s
        data = surf.get("data") or {}
        wrap = tk.Frame(self.intel_body, bg=BG2)
        wrap.pack(fill="both", expand=True)
        tk.Label(wrap, text="{}  ·  {}".format(data.get("date", ""),
                                              data.get("at", "")),
                 bg=BG2, fg=MUTED, font=("Consolas", 7), anchor="w").pack(
            fill="x", padx=int(2 * s))
        bar_w = max(80, self._panel_w - int(46 * s))
        metrics = [("CPU", data.get("cpu", 0)),
                   ("MEMORY", data.get("ram", 0)),
                   ("DISK", data.get("disk", 0))]
        if data.get("battery") is not None:
            metrics.append(("BATTERY", data.get("battery", 0)))
        for label, value in metrics:
            row = tk.Frame(wrap, bg=BG2)
            row.pack(fill="x", padx=int(2 * s), pady=(int(5 * s), 0))
            tk.Label(row, text=label, bg=BG2, fg=MUTED,
                     font=("Consolas", 8), anchor="w").pack(side="left")
            tk.Label(row, text="{0}%".format(value), bg=BG2, fg=ACCENT_SOFT,
                     font=("Consolas", 8, "bold")).pack(side="right")
            bar = tk.Canvas(wrap, bg=SURFACE_HI, height=int(5 * s),
                            highlightthickness=0, bd=0)
            bar.pack(fill="x", padx=int(2 * s), pady=(int(2 * s), 0))
            filled = max(2, int(bar_w * max(0, min(100, value)) / 100.0))
            bar.create_rectangle(0, 0, filled, int(5 * s), fill=ACCENT,
                                 outline="")
        foot = tk.Frame(wrap, bg=BG2)
        foot.pack(fill="x", padx=int(2 * s), pady=(int(7 * s), 0))
        up = data.get("uptime", 0)
        uptime = "{0}d {1:02d}h".format(up // 86400, (up % 86400) // 3600)
        info = ("MEM {0} / {1} GB     NET ↓{2} ↑{3} MB     UP {4}".format(
            data.get("ram_used", 0), data.get("ram_total", 0),
            data.get("net_down", 0), data.get("net_up", 0), uptime))
        tk.Label(foot, text=info, bg=BG2, fg=MUTED, font=("Consolas", 7),
                 anchor="w", justify="left",
                 wraplength=max(120, self._panel_w - int(30 * s))).pack(fill="x")
        tk.Label(wrap, text="TOP PROCESSES", bg=BG2, fg=ACCENT_SOFT,
                 font=("Consolas", 7, "bold"), anchor="w").pack(
            fill="x", padx=int(2 * s), pady=(int(9 * s), int(2 * s)))
        for proc in data.get("processes") or []:
            row = tk.Frame(wrap, bg=BG2)
            row.pack(fill="x", padx=int(2 * s))
            tk.Label(row, text=proc.get("name", "?"), bg=BG2, fg=TEXT,
                     font=("Consolas", 8), anchor="w").pack(side="left")
            tk.Label(row, text="{0} MB".format(proc.get("mb", 0)), bg=BG2,
                     fg=MUTED, font=("Consolas", 8)).pack(side="right")

    def _render_info(self, surf):
        s = self.s
        wrap = tk.Frame(self.intel_body, bg=BG2)
        wrap.pack(fill="both", expand=True)
        tk.Label(wrap, text=str(surf.get("title") or ""), bg=BG2, fg=TEXT,
                 font=("Segoe UI Semibold", 11), anchor="w",
                 justify="left",
                 wraplength=max(120, self._panel_w - int(34 * s))).pack(
            fill="x", padx=int(4 * s), pady=(int(4 * s), int(6 * s)))
        tk.Label(wrap, text=str(surf.get("text") or "Nothing to show yet."),
                 bg=BG2, fg=MUTED, font=("Segoe UI", 9), anchor="nw",
                 justify="left",
                 wraplength=max(120, self._panel_w - int(34 * s))).pack(
            fill="both", expand=True, padx=int(4 * s))

    def _on_surface_frame(self, payload):
        payload = payload or {}
        surf = self._surface or {}
        if not self._intel_on or surf.get("kind") not in ("app", "screen"):
            return
        label = self._surface_img_label
        if str(payload.get("state")) == "lost":
            return
        path = payload.get("path") or ""
        if not path or label is None:
            return
        max_w = max(80, self._panel_w - int(26 * self.s))
        max_h = max(60, self._panel_h - int(104 * self.s))
        try:
            from PIL import Image, ImageTk
            img = Image.open(path)
            img.load()
            img.thumbnail((max_w, max_h), Image.LANCZOS)
            self._img_ref = ImageTk.PhotoImage(img)
            label.configure(image=self._img_ref)
            self._surface_frame_size = (self._img_ref.width(),
                                        self._img_ref.height())
            self._surface_live = payload
        except Exception:  # noqa: BLE001 - drop one bad frame, keep streaming
            return

    def _render_results(self):
        self._clear_intel_body()
        data = self._surface or {}
        items = data.get("items") or []
        kind = str(data.get("kind") or "web").upper()
        query = str(data.get("query") or "")
        self.intel_tag.configure(text="SURFACE // INTEL")
        self.intel_mode.configure(text=kind[:9])
        self.intel_sub.configure(
            text=("“{0}”  ·  {1} results".format(query, len(items)))[:160])
        self.intel_sub.configure(wraplength=max(120, self._panel_w - int(24 * self.s)))
        if not items:
            tk.Label(self.intel_body, text="NO SIGNAL\n\nNothing found.",
                     bg=BG2, fg=MUTED, font=("Consolas", 9),
                     justify="center").pack(expand=True)
            return
        outer = tk.Canvas(self.intel_body, bg=BG2, highlightthickness=0, bd=0)
        inner = tk.Frame(outer, bg=BG2)
        window = outer.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: outer.configure(scrollregion=outer.bbox("all")))
        outer.bind("<Configure>",
                   lambda e: outer.itemconfigure(window, width=e.width))
        outer.pack(fill="both", expand=True)
        wheel = lambda e: outer.yview_scroll(int(-e.delta / 40), "units")
        outer.bind("<MouseWheel>", wheel)
        for n, item in enumerate(items, 1):
            self._result_card(inner, item, n, wheel)

    def _result_card(self, parent, item, n, wheel):
        s = self.s
        card = tk.Frame(parent, bg=SURFACE, highlightthickness=1,
                        highlightbackground=mix(ACCENT, BG2, 0.30),
                        cursor="hand2")
        card.pack(fill="x", padx=2, pady=(0, int(6 * s)))
        title = tk.Label(card, text="{0:02d}  {1}".format(n, item.get("title", "")),
                         bg=SURFACE, fg=TEXT, font=("Segoe UI Semibold", 9),
                         anchor="w", justify="left",
                         wraplength=max(140, self._panel_w - int(34 * s)),
                         cursor="hand2")
        title.pack(fill="x", padx=int(8 * s), pady=(int(6 * s), 0))
        meta = item.get("source") or item.get("date") or ""
        if meta:
            tk.Label(card, text=meta[:60], bg=SURFACE, fg=ACCENT_SOFT,
                     font=("Consolas", 7), anchor="w", cursor="hand2").pack(
                fill="x", padx=int(8 * s))
        body = (item.get("body") or "").strip()
        if body:
            tk.Label(card, text=body, bg=SURFACE, fg=MUTED,
                     font=("Segoe UI", 8), anchor="w", justify="left",
                     wraplength=max(140, self._panel_w - int(34 * s)),
                     cursor="hand2").pack(fill="x", padx=int(8 * s),
                                          pady=(int(2 * s), 0))
        tk.Label(card, text="OPEN IN IRIS  ›", bg=SURFACE, fg=ACCENT,
                 font=("Consolas", 7, "bold"), anchor="w",
                 cursor="hand2").pack(fill="x", padx=int(8 * s),
                                      pady=(int(2 * s), int(5 * s)))
        url = item.get("url") or ""

        def open_it(_event=None):
            self._click_result(url)

        for widget in card.winfo_children() + [card]:
            widget.bind("<ButtonRelease-1>", open_it)
            widget.bind("<MouseWheel>", wheel)

    def _click_result(self, url):
        if not url:
            return
        self.intel_sub.configure(text="Opening in my own browser…")
        try:
            self.open_url(url)
        except Exception:  # noqa: BLE001
            logging.getLogger("iris").exception("could not open result")

    # ------------------------------------------------------- voice permission
    def _on_auth(self, payload):
        payload = payload or {}
        state = str(payload.get("state", "asking"))
        if state == "closed":
            self._auth = None
            self._auth_until = 0.0
        else:
            self._auth = payload
            self._auth_until = 0.0 if state in ("asking", "fallback") else \
                time.perf_counter() + 2.8
        self._layout_auth()

    def _layout_auth(self):
        w = self.stage.winfo_width()
        h = self.stage.winfo_height()
        if not self._auth or w < 80 or h < 120:
            for item in self._auth_items:
                self.stage.itemconfigure(item, state="hidden")
            return
        s = self.s
        width = int(min(max(300 * s, w * 0.36), 540 * s, w - 34 * s))
        cx = self._cx if self._cx > width / 2 + 10 * s else w / 2
        if self._intel_on:
            width = min(width, max(240 * s, (self._cx - 18 * s) * 2))
        height = int(94 * s)
        y2 = h - int(30 * s)
        y1 = y2 - height
        x1, x2 = cx - width / 2, cx + width / 2
        self.stage.coords(self._auth_box, *_round_points(x1, y1, x2, y2, 10 * s))
        self.stage.coords(self._auth_title, cx, y1 + 16 * s)
        self.stage.coords(self._auth_q, cx, y1 + 42 * s)
        self.stage.coords(self._auth_hint, cx, y2 - 14 * s)
        self.stage.itemconfigure(self._auth_q, width=max(120, width - 30 * s))
        for item in self._auth_items:
            self.stage.itemconfigure(item, state="normal")

    # ----------------------------------------------------------- bus wiring
    def _bind_bus(self):
        for topic in ("state", "say", "reply", "heard", "tool", "level",
                      "latency", "toast", "auth", "view", "results", "surface",
                      "surface_frame"):
            bus.subscribe(topic, lambda payload, t=topic: self._q.put((t, payload)))

    # ---------------------------------------------------------- input events
    def _focus_in(self, _event):
        if self._placeholder_on:
            self._placeholder_on = False
            self.entry.delete(0, "end")
            self.entry.configure(fg=TEXT)
        self.entry_wrap.configure(highlightbackground=ACCENT)

    def _focus_out(self, _event):
        self.entry_wrap.configure(highlightbackground=SURFACE_HI)
        if not self.entry.get().strip():
            self._placeholder_on = True
            self.entry.delete(0, "end")
            self.entry.insert(0, "Type a message…")
            self.entry.configure(fg=MUTED)

    def _send(self):
        if self._placeholder_on:
            return
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self.send(text)

    def _on_wheel(self, event):
        if self.tc.bbox("all") and self.tc.bbox("all")[3] > self.tc.winfo_height():
            self.tc.yview_scroll(int(-event.delta / 40), "units")

    def _on_transcript_configure(self, _event):
        self.tc.configure(scrollregion=self.tc.bbox("all"))

    def _on_transcript_resize(self, event):
        self.tc.itemconfigure(self._tc_window, width=event.width)
        self._relayout_bubbles()

    # -------------------------------------------------------- window actions
    def _drag_start(self, event):
        self._drag = (event.x_root - self.root.winfo_x(),
                      event.y_root - self.root.winfo_y())

    def _drag_move(self, event):
        if not self._drag:
            return
        x = event.x_root - self._drag[0]
        y = event.y_root - self._drag[1]
        self.root.geometry("+%d+%d" % (x, y))

    def _resize_start(self, event):
        self._resize = (event.x_root, event.y_root,
                        self.root.winfo_width(), self.root.winfo_height())

    def _resize_move(self, event):
        if not self._resize:
            return
        x0, y0, w0, h0 = self._resize
        w = max(self.root.minsize()[0], w0 + (event.x_root - x0))
        h = max(self.root.minsize()[1], h0 + (event.y_root - y0))
        self.root.geometry("%dx%d" % (w, h))

    def _minimise(self):
        self.root.overrideredirect(False)
        self.root.iconify()

        def _restore(_event=None):
            if self.root.wm_state() == "normal":
                self.root.after(10, lambda: self.root.overrideredirect(True))
        self.root.bind("<Map>", _restore, add="+")

    def _maximise(self):
        if self.root.wm_state() == "zoomed":
            self.root.wm_state("normal")
            return
        try:
            self.root.wm_state("zoomed")
        except tk.TclError:
            self.root.geometry("%dx%d+0+0" % (self.root.winfo_screenwidth(),
                                              self.root.winfo_screenheight()))

    def _close(self):
        self.on_close()

    # --------------------------------------------------------------- dialogs
    def ask(self, question, timeout=20):
        """Called from the assistant thread. Blocks until the user clicks."""
        event, box = threading.Event(), {}
        self._uiq.put(("ask", question, event, box, timeout))
        event.wait(timeout + 2)
        return bool(box.get("answer"))

    def post_close(self):
        self._uiq.put(("close",))

    def _show_ask(self, question, event, box, timeout):
        if self._closed or self._dialog is not None:
            box["answer"] = False
            event.set()
            return
        s = self.s
        w, h = int(400 * s), int(186 * s)
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.configure(bg=BG2)
        top.attributes("-topmost", True)
        top.geometry("%dx%d+%d+%d" % (
            w, h,
            self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2,
            self.root.winfo_rooty() + (self.root.winfo_height() - h) // 2))
        c = tk.Canvas(top, bg=BG2, highlightthickness=0)
        c.pack(fill="both", expand=True)
        _round_rect(c, 1, 1, w - 2, h - 2, r=int(18 * s), fill=SURFACE,
                    outline=ACCENT)
        c.create_text(w / 2, 34 * s, text="IRIS NEEDS YOUR OKAY", fill=ACCENT_SOFT,
                      font=("Segoe UI Semibold", 9))
        c.create_text(w / 2, 76 * s, text=question, fill=TEXT,
                      font=("Segoe UI", 10), width=w - 56 * s, justify="center")
        self._dialog = top
        done = {"finished": False}

        def finish(answer):
            if done["finished"]:
                return
            done["finished"] = True
            box["answer"] = answer
            try:
                top.grab_release()
                top.destroy()
            except tk.TclError:
                pass
            self._dialog = None
            event.set()

        yes = RoundButton(top, text="Yes", command=lambda: finish(True),
                          width=int(92 * s), height=int(36 * s), radius=int(18 * s),
                          fill=ACCENT, hover=ACCENT_SOFT, fg="#1A0410",
                          font=("Segoe UI Semibold", 10))
        yes.place(x=int(58 * s), y=int(126 * s))
        no = RoundButton(top, text="No", command=lambda: finish(False),
                         width=int(92 * s), height=int(36 * s), radius=int(18 * s),
                         fill=SURFACE_HI, hover=mix(SURFACE_HI, ACCENT, 0.25),
                         fg=TEXT, font=("Segoe UI", 10))
        no.place(x=w - int(150 * s), y=int(126 * s))
        top.bind("<Return>", lambda e: finish(True))
        top.bind("<Escape>", lambda e: finish(False))
        top.after(int(timeout * 1000), lambda: finish(False))
        top.focus_force()
        try:
            top.grab_set()
        except tk.TclError:
            pass

    # ------------------------------------------------------------- transcript
    def _bubble_width(self):
        return max(180, min(self.tc.winfo_width() - 120, int(560 * self.s)))

    def _append_widget(self, who, text):
        row = tk.Frame(self.tf, bg=BG2)
        row.pack(fill="x", padx=int(18 * self.s), pady=(2 * self.s, 2 * self.s),
                 anchor="e" if who == "you" else "w")
        bubble = Bubble(row, text, who == "you", self.f_body, self._bubble_width())
        bubble.pack(anchor="e" if who == "you" else "w")
        self._bubbles.append(bubble)
        return bubble

    def _add_bubble(self, who, text):
        if not text:
            return
        self._log.append((who, text))
        self._trim_log()
        self._append_widget(who, text)
        self._scroll_bottom()

    def _trim_log(self):
        if len(self._log) > 80:
            drop = len(self._log) - 60
            del self._log[:drop]
            self._stream_idx = None

    def _relayout_bubbles(self):
        if not self._log or self._relayout_job is not None:
            return
        # debounce so dragging the window does not rebuild 60 canvases per event
        self._relayout_job = self.root.after(120, self._do_relayout)

    def _do_relayout(self):
        self._relayout_job = None
        if not self._log:
            return
        for child in self.tf.winfo_children():
            child.destroy()
        log = list(self._log)
        self._log, self._bubbles = [], []
        for who, text in log:
            self._log.append((who, text))
            self._append_widget(who, text)
        if self._stream_idx is not None and self._stream_idx < len(self._bubbles):
            self._stream_bubble = self._bubbles[self._stream_idx]
        else:
            self._stream_bubble = None
        self._scroll_bottom()

    def _scroll_bottom(self):
        self.tc.update_idletasks()
        self.tc.configure(scrollregion=self.tc.bbox("all"))
        self.tc.yview_moveto(1.0)

    # ------------------------------------------------------------- animation
    def _on_stage_configure(self, _event):
        self._resize_due = time.perf_counter() + 0.05

    def _draw_background(self, w, h):
        c = self.stage
        c.delete("bg")
        steps = 110
        band = max(2, int(h / steps) + 1)
        for i in range(steps):
            y = h * i / (steps - 1)
            c.create_line(0, y, w, y, fill=mix(BG1, BG2, i / (steps - 1)),
                          width=band, tags="bg")
        step = max(26, int(42 * self.s))
        if CYBER:
            grid = mix(BG2, ACCENT, 0.10)
            for x in range(step, w, step):
                c.create_line(x, 0, x, h, fill=grid, width=1, tags="bg")
            for y in range(step, h, step):
                c.create_line(0, y, w, y, fill=grid, width=1, tags="bg")
        c.tag_lower("bg")

    def _layout_stage(self, w, h):
        if w < 40 or h < 40:
            return
        s = self.s
        self._layout_intel(w, h)
        left = w - (self._panel_w + int(28 * s)) if self._intel_on else w
        self._cx = w / 2 if not self._intel_on else max(150 * s, left / 2)
        self._cy = h * 0.40
        # keep the outer halo inside the canvas and the IRIS block inside the
        # space that is left when the intel panel is open
        self._r = max(42 * s, min(min(w, h) * 0.23, 152 * s,
                                  (self._cy - 10 * s) / 1.94,
                                  max(90 * s, self._cx - 12 * s) / 1.94))
        headroom = (h - self._cy - 106 * s) / (0.9 * s)
        max_letters = max(150 * s, 2 * (self._cx - 18 * s))
        size = int(max(22, min(min(w, h) * 0.135, headroom, 120 * s)))
        while size > 18:
            self.f_name.configure(size=size)
            widths = [self.f_name.measure(ch) for ch in "IRIS"]
            track = size * 0.16
            if sum(widths) + track * 3 <= max_letters:
                break
            size = int(size * 0.92)
        half = size * s * 0.90
        widths = [self.f_name.measure(ch) for ch in "IRIS"]
        track = size * 0.16
        total = sum(widths) + track * 3
        x = self._cx - total / 2
        for i, ch in enumerate("IRIS"):
            cx = x + widths[i] / 2
            self._letter_x[i] = cx
            self.stage.itemconfigure(self._letters_shadow[i], font=self.f_name)
            self.stage.itemconfigure(self._letters[i], font=self.f_name)
            self.stage.coords(self._letters_shadow[i], cx, self._cy + 2 * s)
            self.stage.coords(self._letters[i], cx, self._cy)
            x += widths[i] + track
        status_y = self._cy + max(self._r * 0.78, half + 20 * s)
        self.stage.coords(self._status, self._cx, status_y)
        self._bar_y = status_y + 26 * s
        self.stage.coords(self._hint, self._cx, h - 18 * s)
        self.stage.coords(self._latency, w - 20 * s, 42 * s)
        self.stage.itemconfigure(self._hint, text="Esc to stop   ·   Enter to send"
                                 "   ·   say \"%s\" to wake" % WAKE)
        self._bar_span = min(w * 0.46, 340 * s)
        if self._intel_on:
            self._bar_span = min(self._bar_span, max(150 * s, 2 * (self._cx - 28 * s)))
        # HUD furniture
        pad = int(14 * s)
        self.stage.coords(self._tele_tl, pad, pad)
        self.stage.coords(self._tele_tr, w - pad, pad)
        self.stage.coords(self._tele_bl, pad, h - pad)
        arm, m = int(20 * s), int(8 * s)
        corners = [
            (m, m, m + arm, m), (m, m, m, m + arm),
            (w - m, m, w - m - arm, m), (w - m, m, w - m, m + arm),
            (m, h - m, m + arm, h - m), (m, h - m, m, h - m - arm),
            (w - m, h - m, w - m - arm, h - m), (w - m, h - m, w - m, h - m - arm),
        ]
        for item, coords in zip(self._corner, corners):
            self.stage.coords(item, *coords)
        self._layout_auth()
        self._palette()

    def _palette(self):
        peak = _PEAK.get(self.state, 0.2)
        for i, item in enumerate(self._glow):
            frac = i / (len(self._glow) - 1)
            self.stage.itemconfigure(item, fill=mix(BG2, ACCENT, peak * frac ** 1.7))
        self.stage.itemconfigure(self._arcs[0], outline=ACCENT)
        self.stage.itemconfigure(self._arcs[1], outline=mix(ACCENT, BG2, 0.55))
        self.stage.itemconfigure(self._status, fill=MUTED)
        # When a surface is open the big wordmark steps back so the content leads.
        dim = 0.45 if self._surface is not None else 0.0
        for item in self._letters:
            self.stage.itemconfigure(item, fill=mix(TEXT, BG2, dim))
        for item in self._letters_shadow:
            self.stage.itemconfigure(item, fill=mix(mix(TEXT, BG2, dim), BG1, 0.82))
        self.dot.itemconfigure(self._dot_item,
                               fill=ACCENT if self.state != "idle" else
                               mix(MUTED, BG1, 0.45))

    def _animate(self):
        now = time.perf_counter()
        dt = min(0.12, now - self._last)
        self._last = now
        t = now - self._t0
        c = self.stage
        w, h = c.winfo_width(), c.winfo_height()
        if w < 20 or h < 20:
            return
        if (w, h) != self._geom and now >= self._resize_due:
            self._draw_background(w, h)
            self._layout_stage(w, h)
            self._geom = (w, h)

        s = self.s
        state = self.state
        if self._slide and self._intel_on:
            el = (now - self._slide["t0"]) / 0.22
            if el >= 1.0:
                self.stage.coords(self._intel_item,
                                  self._slide["x"], self._slide["y"])
                self._slide = None
            else:
                ease = 1 - (1 - el) ** 3
                self.stage.coords(self._intel_item,
                                  self._slide["x"] + (1 - ease) * 70 * s,
                                  self._slide["y"])
        pulse = 0.5 - 0.5 * math.cos(t * _SPEED.get(state, 1.5))
        target = self.level if state == "listening" else 0.0
        self._lvl += (target - self._lvl) * min(1.0, dt * 14)
        cx, cy, r = self._cx, self._cy, self._r

        # halo
        bump = 1 + 0.05 * pulse + 0.06 * self._lvl
        for i, item in enumerate(self._glow):
            frac = i / (len(self._glow) - 1)
            rad = r * (1.85 - 1.05 * frac) * bump
            c.coords(item, cx - rad, cy - rad, cx + rad, cy + rad)

        # ripples while listening
        if state == "listening":
            for i, item in enumerate(self._ripples):
                phase = ((t % 1.8) / 1.8 + i / 3.0) % 1.0
                rad = r * (1.02 + phase * 0.72)
                c.coords(item, cx - rad, cy - rad, cx + rad, cy + rad)
                c.itemconfigure(item, state="normal",
                                outline=mix(ACCENT, BG2, phase * 0.85),
                                width=max(1, int(2 * (1 - phase))))
        else:
            for item in self._ripples:
                c.itemconfigure(item, state="hidden")

        # rotating arcs
        ang = (t * 42) % 360
        for i, item in enumerate(self._arcs):
            rad = r * (1.30 + 0.05 * i)
            c.coords(item, cx - rad, cy - rad, cx + rad, cy + rad)
            c.itemconfigure(item, start=(ang + i * 180) % 360,
                            extent=150 - i * 95)

        # orbiting particles
        for i, item in enumerate(self._particles):
            a = i * 2.399963 + t * (0.20 + 0.06 * (i % 5))
            rad = r * (1.12 + 0.55 * ((i * 37 % 100) / 100.0)
                       + 0.03 * math.sin(t * 1.7 + i))
            px = cx + math.cos(a) * rad
            py = cy + math.sin(a) * rad * 0.86
            sz = (1.2 + 1.4 * (0.5 + 0.5 * math.sin(t * 2 + i))) * s
            c.coords(item, px - sz, py - sz, px + sz, py + sz)

        # letters (gentle float)
        bob = math.sin(t * 1.2) * 1.6 * s
        for i, item in enumerate(self._letters):
            c.coords(item, self._letter_x[i], cy + bob)
            c.coords(self._letters_shadow[i], self._letter_x[i], cy + bob + 2 * s)

        # equaliser
        n = len(self._bars)
        span = self._bar_span
        for i, item in enumerate(self._bars):
            x = cx - span / 2 + span * i / (n - 1)
            if state == "speaking":
                env = (0.25 + 0.75 * abs(math.sin(t * 6.5 + i * 0.55))
                       * abs(math.sin(t * 2.1 + i * 0.31)))
            elif state == "listening":
                env = self._lvl * (0.35 + 0.65 * abs(math.sin(t * 9 + i * 0.6)))
            elif state == "thinking":
                env = 0.10 + 0.08 * abs(math.sin(t * 3.4 + i * 0.8))
            else:
                env = 0.05 + 0.04 * abs(math.sin(t * 1.5 + i * 0.35))
            self._barv[i] += (env - self._barv[i]) * min(1.0, dt * 12)
            half = (2.5 * s) + self._barv[i] * 30 * s
            c.coords(item, x, self._bar_y - half, x, self._bar_y + half)
            c.itemconfigure(item, fill=mix(ACCENT_SOFT, ACCENT, self._barv[i]))
            c.itemconfigure(item, width=max(1, int(2 * s)))

        if CYBER:
            # scanlines
            sy = (t * 0.055 % 1.0) * h
            c.coords(self._scan, 0, sy, w, sy)
            sy2 = ((t * 0.017 + 0.35) % 1.0) * h
            c.coords(self._scan2, 0, sy2, w, sy2)

            # radial ticks around the halo
            base = t * 7.0
            for i, item in enumerate(self._ticks):
                a = math.radians(base + i * 7.5)
                r0 = r * 1.46
                r1 = r0 + (8 if i % 4 == 0 else 4) * s
                c.coords(item, cx + math.cos(a) * r0, cy + math.sin(a) * r0,
                         cx + math.cos(a) * r1, cy + math.sin(a) * r1)

            # data rain
            for item, st in zip(self._rain, self._rain_state):
                st["y"] += st["v"] * dt
                if st["y"] > 1.06:
                    st["y"] = -0.04
                    st["x"] = random.random()
                    st["v"] = 0.05 + random.random() * 0.09
                c.coords(item, st["x"] * w, st["y"] * h)
                if random.random() < 0.10:
                    c.itemconfigure(item, text=random.choice("0123456789ABCDEF·+×"))

            # telemetry readouts
            c.itemconfigure(self._tele_tl, text="SYS // IRIS-OS 2.6\nSECURE LINK · ONLINE")
            c.itemconfigure(self._tele_tr, text="MIC {0:02d}%  ·  {1}\nFRAME {2:06d}".format(
                int(self._lvl * 100), state.upper()[:8], int(t * FPS) % 999999))
            c.itemconfigure(self._tele_bl, text="AUTH // VOICE PRINT · {0}\nHUD // CYBER v2".format(
                "ACTIVE" if self._auth else "STANDBY"))
        self._animate_auth(now, t)

        # status line
        if self._toast and now < self._toast_until:
            status, color = self._toast, ACCENT_SOFT
        elif state == "thinking" and self._tool:
            status, color = self._tool + "…", MUTED
        elif state == "listening":
            status, color = "listening…", ACCENT_SOFT
        elif state == "speaking":
            status, color = "speaking…", ACCENT_SOFT
        elif state == "thinking":
            status, color = "thinking…", MUTED
        else:
            status, color = "ready", MUTED
        c.itemconfigure(self._status, text=status, fill=color)
        c.itemconfigure(self._latency,
                        text=("first word  %.1fs" % self._latency) if self._latency else "")

    def _animate_auth(self, now, t):
        if not self._auth:
            for item in self._auth_items:
                self.stage.itemconfigure(item, state="hidden")
            return
        if self._auth_until and now >= self._auth_until:
            self._auth = None
            for item in self._auth_items:
                self.stage.itemconfigure(item, state="hidden")
            return
        state = str(self._auth.get("state", "asking"))
        pulse = 0.5 + 0.5 * math.sin(t * 5.2)
        if state == "granted":
            title, outline = "ACCESS GRANTED", ACCENT
            hint = "voice print accepted"
        elif state == "denied":
            title, outline = "ACCESS DENIED", mix(ACCENT, SURFACE, 0.45)
            hint = "voice print rejected"
        elif state == "timeout":
            title, outline = "NO RESPONSE", mix(MUTED, BG2, 0.35)
            hint = "say yes or no next time"
        elif state == "fallback":
            title, outline = "MIC UNAVAILABLE", mix(ACCENT, SURFACE, 0.3)
            hint = "click to answer"
        else:
            title = "PERMISSION REQUIRED"
            outline = mix(ACCENT, SURFACE, 0.08 + 0.40 * pulse)
            hint = "listening · say yes or no" + "." * (1 + int(t * 2) % 3)
        self.stage.itemconfigure(self._auth_title, text=title,
                                 fill=MUTED if state in ("denied", "timeout")
                                 else ACCENT_SOFT)
        self.stage.itemconfigure(self._auth_q, text=self._auth.get("question", ""))
        self.stage.itemconfigure(self._auth_hint, text=hint.upper())
        self.stage.itemconfigure(self._auth_box, outline=outline)
        for item in self._auth_items:
            self.stage.itemconfigure(item, state="normal")

    # ------------------------------------------------------------- main loop
    def _drain(self):
        while True:
            try:
                kind, *rest = self._q.get_nowait()
            except queue.Empty:
                break
            if kind == "state":
                self._set_state(rest[0])
            elif kind == "say":
                self._on_say(rest[0])
            elif kind == "reply":
                self._on_reply(rest[0])
            elif kind == "heard":
                self._stream_bubble = None
                self._stream_idx = None
                self._add_bubble("you", str(rest[0]))
            elif kind == "tool":
                self._tool = str(rest[0])
            elif kind == "level":
                try:
                    self.level = max(0.0, min(1.0, float(rest[0])))
                except (TypeError, ValueError):
                    pass
            elif kind == "latency":
                try:
                    self._latency = float(rest[0])
                except (TypeError, ValueError):
                    pass
            elif kind == "toast":
                self._toast = str(rest[0])
                self._toast_until = time.perf_counter() + 4.5
            elif kind == "auth":
                self._on_auth(rest[0] if rest else {})
            elif kind == "surface":
                self._show_surface(rest[0] if rest else {})
            elif kind == "surface_frame":
                self._on_surface_frame(rest[0] if rest else {})
            elif kind == "view":
                self._show_view(rest[0] if rest else {})
            elif kind == "results":
                self._show_results(rest[0] if rest else {})

        while True:
            try:
                cmd = self._uiq.get_nowait()
            except queue.Empty:
                break
            if cmd[0] == "ask":
                self._show_ask(*cmd[1:])
            elif cmd[0] == "close":
                self._shutdown()

    def _set_state(self, state):
        if state == self.state:
            return
        self.state = state
        if state in ("idle", "listening"):
            self._tool = ""
        if state == "idle":
            self._stream_bubble = None
            self._stream_idx = None
        self._palette()

    def _on_say(self, sentence):
        text = str(sentence).strip()
        if not text:
            return
        if self._stream_idx is None or self._stream_idx >= len(self._log):
            self._log.append(("iris", text))
            self._stream_idx = len(self._log) - 1
            self._stream_bubble = self._append_widget("iris", text)
        else:
            merged = (self._log[self._stream_idx][1] + " " + text).strip()
            self._log[self._stream_idx] = ("iris", merged)
            if self._stream_bubble is not None:
                self._stream_bubble.set_text(merged)
        self._scroll_bottom()

    def _on_reply(self, reply):
        text = str(reply).strip()
        if not text:
            return
        if self._stream_idx is not None and self._stream_idx < len(self._log):
            self._log[self._stream_idx] = ("iris", text)
            if self._stream_bubble is not None:
                self._stream_bubble.set_text(text)
        else:
            self._add_bubble("iris", text)
        self._stream_bubble = None
        self._stream_idx = None
        self._scroll_bottom()

    def _tick(self):
        if self._closed:
            return
        try:
            self._drain()
            self._animate()
        except tk.TclError:
            return
        except Exception:
            # never let one odd event freeze the animation loop
            logging.getLogger("iris").exception("ui tick failed")
        self.root.after(max(16, int(1000 / FPS)), self._tick)

    def _shutdown(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self):
        self.root.after(0, self._tick)
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self._shutdown()


# ------------------------------------------------------------------ preview
if __name__ == "__main__":
    ui = IrisUI(send=lambda text: print("[send]", repr(text)),
                stop=lambda: print("[stop]"))
    demo = [("heard", "hey iris, put something calm on"),
            ("state", "thinking"),
            ("tool", "opening youtube"),
            ("say", "On it."),
            ("state", "speaking"),
            ("say", "Opening YouTube and looking for a lo-fi stream."),
            ("reply", "Opening YouTube and looking for a lo-fi stream."),
            ("state", "idle")]
    delay = 1200
    for topic, payload in demo:
        ui.root.after(delay, lambda t=topic, p=payload: bus.publish(t, p))
        delay += 1600
    ui.run()

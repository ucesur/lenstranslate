#!/usr/bin/env python3
"""
LensTranslate - a floating "magnifying glass" that translates the text
under it, on top of ANY window (browser, PDF viewer, game, ...).

How it works
  1. You place the lens over some text.
  2. It captures the screen area inside the lens.
  3. Tesseract OCR finds the text paragraphs.
  4. Each paragraph is translated and painted back over the original text,
     inside the lens.

Controls
  drag (anywhere on the lens)  move it
  drag the edges / corners     resize it
  mouse wheel                  resize (grow / shrink)
  Space / Enter / dbl-click    translate now
  T                            toggle translation / original
  right-click                  menu (languages, auto mode, quit)
  Esc                          quit
"""
import os
import sys
import shutil
import subprocess
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass

# On Linux force Qt's X11 backend (XWayland on Wayland sessions): frameless,
# always-on-top windows with absolute positions only work reliably there.
if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

import pytesseract
import requests
from deep_translator import GoogleTranslator
from PIL import Image, ImageOps
from PySide6.QtCore import QPoint, QRect, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import (QApplication, QFontDialog, QMenu, QPlainTextEdit,
                               QWidget)

def _setup_tesseract():
    """Find tesseract on Windows / inside a PyInstaller bundle when it is not on PATH."""
    if shutil.which("tesseract"):
        return
    roots = [getattr(sys, "_MEIPASS", None), os.path.dirname(os.path.abspath(sys.argv[0]))]
    cands = [os.path.join(r, "tesseract", "tesseract.exe") for r in roots if r]
    if sys.platform.startswith("win"):
        cands += [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
        ]
    for c in cands:
        if os.path.isfile(c):
            pytesseract.pytesseract.tesseract_cmd = c
            data = os.path.join(os.path.dirname(c), "tessdata")
            if os.path.isdir(data):
                os.environ.setdefault("TESSDATA_PREFIX", data)
            return


_setup_tesseract()


def _hide_child_consoles():
    """Windows: stop tesseract.exe (and any other child process) from flashing a
    console window every time we call it from the windowed app."""
    if not sys.platform.startswith("win"):
        return
    _orig = subprocess.Popen

    class _QuietPopen(_orig):
        def __init__(self, *a, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | 0x08000000  # CREATE_NO_WINDOW
            super().__init__(*a, **kw)

    subprocess.Popen = _QuietPopen


_hide_child_consoles()


# --------------------------------------------------------------------------
# Logging: file + in-memory buffer (shown from the right-click menu)
# --------------------------------------------------------------------------
import logging
import logging.handlers
from collections import deque

LOG_BUFFER: deque = deque(maxlen=2000)


class _BufferHandler(logging.Handler):
    def emit(self, record):
        LOG_BUFFER.append(self.format(record))


def _log_path() -> str:
    """lens.log next to the exe (or lens.py); falls back to a per-user folder
    when that location is not writable (e.g. Program Files)."""
    if getattr(sys, "frozen", False):
        app_dir = os.path.dirname(sys.executable)
    else:
        app_dir = os.path.dirname(os.path.abspath(__file__))
    if sys.platform.startswith("win"):
        user = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "LensTranslate")
    else:
        user = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "LensTranslate")
    for d in (os.path.join(app_dir, "logs"), user):
        try:
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, "lens.log")
            open(path, "a").close()
            return path
        except OSError:
            continue
    raise OSError("no writable log folder")


log = logging.getLogger("lens")
log.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s.%(msecs)03d [%(threadName)s] %(message)s", "%H:%M:%S")
_bh = _BufferHandler()
_bh.setFormatter(_fmt)
log.addHandler(_bh)
try:
    LOG_FILE = _log_path()
    _fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=500_000, backupCount=1, encoding="utf-8")
    _fh.setFormatter(_fmt)
    log.addHandler(_fh)
except OSError:
    LOG_FILE = ""
if not getattr(sys, "frozen", False):
    _sh = logging.StreamHandler(sys.stderr)
    _sh.setFormatter(_fmt)
    log.addHandler(_sh)

# name -> (translator code, tesseract code)
LANGS = {
    "English": ("en", "eng"),
    "Turkish": ("tr", "tur"),
    "German": ("de", "deu"),
    "French": ("fr", "fra"),
    "Spanish": ("es", "spa"),
    "Italian": ("it", "ita"),
    "Portuguese": ("pt", "por"),
    "Dutch": ("nl", "nld"),
    "Polish": ("pl", "pol"),
    "Russian": ("ru", "rus"),
    "Ukrainian": ("uk", "ukr"),
    "Greek": ("el", "ell"),
    "Arabic": ("ar", "ara"),
    "Japanese": ("ja", "jpn"),
    "Korean": ("ko", "kor"),
    "Chinese (Simplified)": ("zh-CN", "chi_sim"),
}
CJK_OCR = {"jpn", "kor", "chi_sim"}


# --------------------------------------------------------------------------
# Screen capture
# --------------------------------------------------------------------------
def _is_wayland() -> bool:
    if os.environ.get("LENS_CAPTURE", "").lower() == "qt":
        return False
    return (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        or bool(os.environ.get("WAYLAND_DISPLAY"))
    )


def _qimage_to_pil(img: QImage) -> Image.Image:
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    data = bytes(img.constBits())
    return Image.frombuffer(
        "RGB", (img.width(), img.height()), data, "raw", "RGB", img.bytesPerLine(), 1
    ).copy()


def _wayland_screenshot() -> Image.Image:
    """Full-desktop screenshot through an external tool (Wayland blocks
    direct grabbing). Tries the common tools in order."""
    path = tempfile.mktemp(suffix=".png")
    candidates = [
        ("gnome-screenshot", ["gnome-screenshot", "-f", path]),
        ("grim", ["grim", path]),
        ("spectacle", ["spectacle", "-b", "-n", "-f", "-o", path]),
        ("scrot", ["scrot", "-o", path]),
    ]
    for exe, cmd in candidates:
        if shutil.which(exe):
            try:
                subprocess.run(cmd, check=True, timeout=15,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if os.path.exists(path):
                    img = Image.open(path).convert("RGB")
                    img.load()
                    os.remove(path)
                    return img
            except Exception:
                continue
    raise RuntimeError(
        "No screenshot tool found for Wayland. Install one:\n"
        "sudo apt install gnome-screenshot   (or log in with an Xorg session)"
    )


def grab_region(rect: QRect) -> Image.Image:
    """Return a PIL image (physical pixels) of the given global logical rect."""
    log.info("capture: rect=%d,%d %dx%d  wayland=%s", rect.x(), rect.y(),
             rect.width(), rect.height(), _is_wayland())
    if _is_wayland():
        full = _wayland_screenshot()
        vg = QGuiApplication.primaryScreen().virtualGeometry()
        s = full.width / max(1, vg.width())
        box = (
            int((rect.x() - vg.x()) * s),
            int((rect.y() - vg.y()) * s),
            int((rect.x() + rect.width() - vg.x()) * s),
            int((rect.y() + rect.height() - vg.y()) * s),
        )
        return full.crop(box)

    screen = QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()
    sg = screen.geometry()
    pm = screen.grabWindow(0, rect.x() - sg.x(), rect.y() - sg.y(),
                           rect.width(), rect.height())
    return _qimage_to_pil(pm.toImage())


# --------------------------------------------------------------------------
# OCR + translation
# --------------------------------------------------------------------------
@dataclass
class Block:
    x: float          # original paragraph box (image pixels)
    y: float
    w: float
    h: float
    text: str
    bg: tuple         # original background colour
    fg: tuple         # original text colour
    font_px: float    # estimated original font size in pixels
    bold: bool = False
    line_h: float = 0.0   # height of the first original line
    pitch: float = 0.0    # original distance between lines (0 = single line)
    layout: object = None  # cached (font size, wrapped lines, pitch), filled when painting


def _dominant_color(crop: Image.Image) -> tuple:
    crop = crop.convert("RGB")
    crop.thumbnail((48, 48))
    raw = crop.tobytes()
    px = list(zip(raw[0::3], raw[1::3], raw[2::3]))
    cnt = Counter((r // 24, g // 24, b // 24) for r, g, b in px)
    top = cnt.most_common(1)[0][0]
    sel = [c for c in px if (c[0] // 24, c[1] // 24, c[2] // 24) == top]
    n = len(sel)
    return tuple(sum(c[i] for c in sel) // n for i in range(3))  # true average, no banding


BOLD_DENSITY = 0.385  # ink / OCR word-box area; measured: regular 0.27-0.38, bold 0.37-0.47


def _lum(c) -> float:
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def _text_style(img: Image.Image, box: tuple, bg: tuple, word_area: float):
    """Return (text colour, ink density) measured from the original pixels."""
    crop = img.crop(box).convert("RGB")
    raw = crop.tobytes()
    lb = _lum(bg)
    ink = []
    for c in zip(raw[0::3], raw[1::3], raw[2::3]):
        d = abs(_lum(c) - lb)
        if d > 60:
            ink.append((d, c))
    if len(ink) < 6:
        return ((20, 20, 20) if lb > 140 else (245, 245, 245)), 0.0
    ink.sort(key=lambda t: -t[0])
    core = [c for _, c in ink[: max(6, len(ink) * 2 // 5)]]  # strongest pixels = real text colour
    n = len(core)
    col = tuple(sum(c[i] for c in core) // n for i in range(3))
    return col, len(ink) / max(1.0, word_area)


def ocr_blocks(img: Image.Image, ocr_lang: str) -> list:
    langs = ocr_lang.split("+")
    available = set(pytesseract.get_languages())
    for code in langs:
        if code not in available:
            raise RuntimeError(
                f"Missing OCR data '{code}'.\n"
                f"sudo apt install tesseract-ocr-{code.replace('_', '-')}"
            )

    log.info("ocr: start, image %dx%d, langs=%s", img.width, img.height, ocr_lang)
    up = 2 if img.height < 700 else 1
    gray = ImageOps.autocontrast(ImageOps.grayscale(img))
    if up > 1:
        gray = gray.resize((gray.width * up, gray.height * up), Image.LANCZOS)

    d = pytesseract.image_to_data(
        gray, lang=ocr_lang, config="--psm 3", output_type=pytesseract.Output.DICT
    )

    log.info("ocr: tesseract returned %d tokens", len(d["text"]))
    paras = {}
    for i, raw in enumerate(d["text"]):
        word = raw.strip()
        if not word:
            continue
        try:
            if float(d["conf"][i]) < 40:
                continue
        except (TypeError, ValueError):
            continue
        l, t, w, h = (d[k][i] / up for k in ("left", "top", "width", "height"))
        key = (d["page_num"][i], d["block_num"][i], d["par_num"][i])
        p = paras.get(key)
        if p is None:
            p = paras[key] = dict(words=[], hs=[], x0=l, y0=t, x1=l + w, y1=t + h,
                                  lines={}, area=0.0)
        p["words"].append(word)
        p["hs"].append(h)
        p["area"] += w * h
        ln = p["lines"].setdefault(d["line_num"][i], [t, t + h])
        ln[0], ln[1] = min(ln[0], t), max(ln[1], t + h)
        p["x0"], p["y0"] = min(p["x0"], l), min(p["y0"], t)
        p["x1"], p["y1"] = max(p["x1"], l + w), max(p["y1"], t + h)

    joiner = "" if any(c in CJK_OCR for c in langs) else " "
    blocks = []
    for p in paras.values():
        text = joiner.join(p["words"])
        if len(text) < 2 or not any(c.isalpha() for c in text):
            continue
        x0, y0 = max(0, int(p["x0"])), max(0, int(p["y0"]))
        x1, y1 = min(img.width, int(p["x1"]) + 1), min(img.height, int(p["y1"]) + 1)
        if x1 <= x0 or y1 <= y0:
            continue
        box = (x0, y0, x1, y1)
        bg = _dominant_color(img.crop(box))
        fg, density = _text_style(img, box, bg, p["area"])

        hs = sorted(p["hs"])
        font_px = hs[len(hs) // 2] / 0.8      # word box height is ~0.8 em
        tops = sorted(v[0] for v in p["lines"].values())
        pitch = 0.0
        if len(tops) >= 2:
            diffs = sorted(b - a for a, b in zip(tops, tops[1:]))
            pitch = diffs[len(diffs) // 2]
        first = min(p["lines"].values(), key=lambda v: v[0])
        blocks.append(Block(x0, y0, x1 - x0, y1 - y0, text, bg, fg, font_px,
                            density >= BOLD_DENSITY, first[1] - first[0], pitch))
    log.info("ocr: %d text block(s) found", len(blocks))
    for b in blocks[:60]:
        log.debug("  block @%d,%d %dx%d font=%.0fpx bold=%s: %r", b.x, b.y, b.w, b.h,
                  b.font_px, b.bold, b.text[:70])
    return blocks[:60]


_cache: dict = {}


_http = requests.Session()
_http.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


_direct = requests.Session()   # same, but ignores system / environment proxy settings
_direct.trust_env = False
_direct.headers.update(_http.headers)
_proxy_logged = False


def _log_proxies_once():
    global _proxy_logged
    if _proxy_logged:
        return
    _proxy_logged = True
    try:
        import urllib.request  # noqa: PLC0415
        log.info("network: system proxies = %s", urllib.request.getproxies() or "none")
    except Exception:  # noqa: BLE001
        pass


def _backend_gtx(text: str, src: str, target: str) -> str:
    """Google's gtx endpoint (separate from the page deep-translator uses).
    Tries the system proxy settings first, then a direct connection."""
    _log_proxies_once()
    delay = 1.5
    last = None
    for sess, label in ((_http, "system settings"), (_direct, "direct, no proxy")):
        try:
            for _ in range(3):
                r = sess.post(
                    "https://translate.googleapis.com/translate_a/single",
                    params={"client": "gtx", "sl": "auto", "tl": target, "dt": "t"},
                    data={"q": text}, timeout=20,
                )
                if r.status_code in (429, 503):
                    time.sleep(delay)
                    delay *= 2
                    continue
                r.raise_for_status()
                if sess is _direct:
                    log.info("network: direct connection worked (system proxy settings did not)")
                return "".join(seg[0] for seg in r.json()[0] if seg and seg[0])
            raise RuntimeError("Google blocked requests from this IP (HTTP 429)")
        except requests.exceptions.ConnectionError as e:
            log.warning("network: %s failed: %s", label, type(e).__name__)
            last = e
    raise last


def _backend_deep(text: str, src: str, target: str) -> str:
    return GoogleTranslator(source="auto", target=target).translate(text) or text


def _backend_argos(text: str, src: str, target: str) -> str:
    """Offline fallback: pip install argostranslate, then e.g.
    argospm update && argospm install translate-en_tr"""
    import argostranslate.translate as at  # noqa: PLC0415
    s, t = src.split("-")[0], target.split("-")[0]
    try:
        return at.translate(text, s, t)
    except AttributeError:
        raise RuntimeError(f"Offline model {s}->{t} not installed: argospm install translate-{s}_{t}")


BACKENDS = [("Google", _backend_gtx), ("Google-2", _backend_deep), ("Offline", _backend_argos)]


def _translate_call(text: str, src: str, target: str) -> str:
    """Try each backend in turn until one works."""
    errors = []
    for name, fn in BACKENDS:
        try:
            out = fn(text, src, target)
            if out:
                log.info("translate: backend %s ok (%d chars -> %d chars)", name, len(text), len(out))
                return out
        except ImportError:
            log.debug("translate: backend %s not installed", name)
            continue  # optional backend not installed
        except Exception as e:  # noqa: BLE001
            root = e
            while root.__cause__ or root.__context__:
                root = root.__cause__ or root.__context__
            log.warning("translate: backend %s failed: %s: %s | root cause: %s: %s", name,
                        type(e).__name__, str(e).splitlines()[0][:100],
                        type(root).__name__, str(root)[:300])
            errors.append(f"{name}: {str(e).splitlines()[0][:90]}")
    raise RuntimeError(" | ".join(errors) or "no translation backend available")


def translate_blocks(blocks: list, src: str, target: str) -> list:
    """Translate all blocks with as few requests as possible: paragraphs are
    joined with newlines and sent together (chunks under 4500 chars)."""
    todo = [b for b in blocks if (target, b.text) not in _cache]

    log.info("translate: %d block(s), %d cached, %d to send (%s -> %s)",
             len(blocks), len(blocks) - len(todo), len(todo), src, target)
    chunks, cur, size = [], [], 0
    for b in todo:
        if cur and size + len(b.text) + 1 > 4500:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(b)
        size += len(b.text) + 1
    if cur:
        chunks.append(cur)

    for chunk in chunks:
        texts = [b.text.replace("\n", " ") for b in chunk]
        out = _translate_call("\n".join(texts), src, target).split("\n")
        if len(out) == len(chunk):
            for b, t in zip(chunk, out):
                _cache[(target, b.text)] = t.strip() or b.text
        else:  # separators got merged: one request each, throttled
            for b in chunk:
                _cache[(target, b.text)] = _translate_call(b.text, src, target)
                time.sleep(0.4)
        time.sleep(0.3)

    for b in blocks:
        b.text = _cache.get((target, b.text), b.text)
    return blocks


class Worker(QThread):
    done = Signal(list, int)
    failed = Signal(str, int)

    def __init__(self, img, ocr_lang, src, target, gen):
        super().__init__()
        self.img, self.ocr_lang, self.src, self.target, self.gen = img, ocr_lang, src, target, gen

    def run(self):
        threading.current_thread().name = f"worker-{self.gen}"
        t0 = time.time()
        try:
            blocks = ocr_blocks(self.img, self.ocr_lang)
            if blocks:
                blocks = translate_blocks(blocks, self.src, self.target)
            log.info("worker: done in %.1fs, %d block(s)", time.time() - t0, len(blocks))
            self.done.emit(blocks, self.gen)
        except Exception as e:  # noqa: BLE001 - shown to the user
            log.exception("worker: FAILED after %.1fs", time.time() - t0)
            self.failed.emit(str(e), self.gen)


# --------------------------------------------------------------------------
# The lens window
# --------------------------------------------------------------------------
class LogWindow(QPlainTextEdit):
    """Live view of LOG_BUFFER."""

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setWindowTitle(f"LensTranslate log  -  {LOG_FILE or '(no file)'}")
        self.resize(900, 420)
        self._n = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(400)
        self._refresh()

    def _refresh(self):
        lines = list(LOG_BUFFER)
        if len(lines) == self._n:
            return
        bar = self.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        self.setPlainText("\n".join(lines))
        self._n = len(lines)
        if at_end:
            bar.setValue(bar.maximum())


class Lens(QWidget):
    TOP = 10   # top strip height (drag handle)
    M = 5      # frame thickness (left / right / bottom)
    GRAB = 8   # minimum resize hit zone, independent of the drawn border
    MIN_W, MIN_H = 160, 100

    def __init__(self):
        super().__init__()
        self.setWindowTitle("LensTranslate")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        self.settings = QSettings("LensTranslate", "LensTranslate")
        self.target = self.settings.value("target", "Turkish")
        src = self.settings.value("sources", "English")
        self.sources = [s for s in str(src).split(",") if s in LANGS] or ["English"]
        self.auto = self.settings.value("auto", "false") in (True, "true")
        self.font_family = str(self.settings.value("font", "") or "")
        if self.target not in LANGS:
            self.target = "Turkish"
        # border thickness (instance values shadow the class defaults)
        self.TOP = self._clamp(self.settings.value("top", Lens.TOP), 1, 60)
        self.M = self._clamp(self.settings.value("side", Lens.M), 0.5, 40)

        self.blocks: list = []
        self.show_overlay = True
        self.img_size = (1, 1)
        self.workers = []
        self.status = "Space = translate  |  right-click = menu"
        self.gen = 0
        self.worker = None
        self._drag = None
        self._ready = False

        geo = str(self.settings.value("geometry", ""))
        try:
            x, y, w, h = (int(v) for v in geo.split(","))
            self.setGeometry(x, y, w, h)
        except ValueError:
            self.resize(560, 320)
            self.move(QGuiApplication.primaryScreen().geometry().center() - QPoint(280, 160))

        self.auto_timer = QTimer(self, singleShot=True, interval=800)
        self.auto_timer.timeout.connect(self._auto_fire)

        try:
            pytesseract.get_tesseract_version()
        except Exception:  # noqa: BLE001
            self.status = "Tesseract not found: sudo apt install tesseract-ocr"
        self._ready = True

    # ---- geometry helpers -------------------------------------------------
    def inner(self) -> QRect:
        return QRect(self.M, self.TOP, self.width() - 2 * self.M,
                     self.height() - self.TOP - self.M)

    def capture_rect(self) -> QRect:
        r = self.inner()
        return QRect(self.mapToGlobal(r.topLeft()), r.size())

    def _edges(self, pos: QPoint):
        e = Qt.Edges()
        g = max(self.M, self.GRAB)   # grab zone never gets thinner than GRAB px
        if pos.x() < g:
            e |= Qt.LeftEdge
        if pos.x() > self.width() - g:
            e |= Qt.RightEdge
        if pos.y() > self.height() - g:
            e |= Qt.BottomEdge
        return e

    # ---- painting ---------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h, M, TOP = self.width(), self.height(), self.M, self.TOP
        frame = QColor(24, 28, 38, 225)
        p.fillRect(0, 0, w, TOP, frame)
        p.fillRect(0, TOP, M, h - TOP, frame)
        p.fillRect(w - M, TOP, M, h - TOP, frame)
        p.fillRect(M, h - M, w - 2 * M, M, frame)

        inner = self.inner()
        p.fillRect(inner, QColor(0, 0, 0, 1))  # near-invisible, but catches the mouse

        # translated text
        if self.show_overlay and self.blocks:
            f = inner.width() / max(1, self.img_size[0])
            p.save()
            p.setClipRect(inner)
            p.translate(inner.topLeft())
            p.scale(f, f)
            for b in self.blocks:
                self._draw_block(p, b)
            p.restore()

        p.setPen(QPen(QColor(90, 170, 255), 1))
        p.drawRect(inner.adjusted(-1, -1, 0, 0))

        # resize grip
        p.setPen(QPen(QColor(160, 170, 190), 1.5))
        for i in (4, 8, 12):
            p.drawLine(w - i, h - 2, w - 2, h - i)
        p.end()

    def _make_font(self, b: Block, size: int) -> QFont:
        font = QFont(self.font_family) if self.font_family else QFont(self.font())
        font.setPixelSize(max(6, size))
        font.setBold(b.bold)
        return font

    @staticmethod
    def _wrap(text: str, fm: QFontMetrics, maxw: int) -> list:
        sep = " " if " " in text else ""
        tokens = text.split(" ") if sep else list(text)
        lines, cur = [], ""
        for t in tokens:
            trial = f"{cur}{sep}{t}" if cur else t
            if not cur or fm.horizontalAdvance(trial) <= maxw:
                cur = trial
            else:
                lines.append(cur)
                cur = t
        if cur:
            lines.append(cur)
        return lines

    def _layout_block(self, b: Block):
        """Same font size, weight and line spacing as the original; only shrink
        (down to 75 %) when the translation is longer than the space it had."""
        maxw = int(b.w) + 4
        orig = max(7, int(round(b.font_px)))
        orig_lines = max(1, round(b.h / b.pitch)) if b.pitch else 1
        allowed_h = b.h + 4
        size = orig
        while True:
            fm = QFontMetrics(self._make_font(b, size))
            lines = self._wrap(b.text, fm, maxw)
            pitch = (b.pitch * size / orig) if b.pitch else fm.lineSpacing()
            total = pitch * (len(lines) - 1) + (b.line_h or fm.height())
            if total <= allowed_h * 1.05 or size <= max(7, int(orig * 0.75)):
                break
            size -= 1
        b.layout = (size, lines, pitch, orig_lines)

    def _draw_block(self, p: QPainter, b: Block):
        if b.layout is None:
            self._layout_block(b)
        size, lines, pitch, _ = b.layout
        line_h = b.line_h or pitch
        total_h = pitch * (len(lines) - 1) + line_h
        p.fillRect(QRect(int(b.x) - 2, int(b.y) - 2, int(b.w) + 4,
                         int(max(b.h, total_h)) + 4), QColor(*b.bg))
        p.setFont(self._make_font(b, size))
        p.setPen(QColor(*b.fg))
        first_center = b.y + line_h / 2
        for i, line in enumerate(lines):
            c = first_center + i * pitch
            p.drawText(QRect(int(b.x), int(c - pitch / 2) - 1, int(b.w) + 400, int(pitch) + 2),
                       Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine, line)

    # ---- translate --------------------------------------------------------
    def translate_now(self):
        self.gen += 1
        log.info("--- translate #%d requested (%s -> %s)", self.gen,
                 "+".join(self.sources), self.target)
        self.blocks = []
        self.show_overlay = True
        self.status = "Capturing..."
        self.update()
        gen = self.gen
        # give the compositor a moment to repaint the lens without the overlay
        QTimer.singleShot(180, lambda: self._capture(gen))

    def _capture(self, gen):
        if gen != self.gen:
            return
        try:
            img = grab_region(self.capture_rect())
        except Exception as e:  # noqa: BLE001
            log.exception("capture failed")
            self.status = f"Capture failed: {e}"
            self.update()
            return
        self.img_size = img.size
        log.info("capture: got %dx%d image, starting worker", *img.size)
        self.status = "Reading + translating..."
        self.update()
        ocr_lang = "+".join(LANGS[s][1] for s in self.sources)
        w = Worker(img, ocr_lang, LANGS[self.sources[0]][0], LANGS[self.target][0], gen)
        w.done.connect(self._on_done)
        w.failed.connect(self._on_failed)
        self.workers.append(w)   # keep a reference until the thread has finished
        w.finished.connect(lambda w=w: self.workers.remove(w) if w in self.workers else None)
        self.worker = w
        w.start()

    def _on_done(self, blocks, gen):
        if gen != self.gen:
            log.info("result for #%d ignored (stale, now #%d)", gen, self.gen)
            return
        log.info("result #%d shown: %d block(s)", gen, len(blocks))
        self.blocks = blocks
        self.status = f"{len(blocks)} text block(s)" if blocks else "No text found"
        self.update()

    def _on_failed(self, msg, gen):
        if gen != self.gen:
            return
        log.error("error #%d: %s", gen, msg.splitlines()[0] if msg else msg)
        self.status = msg.splitlines()[0][:120]
        self.update()
        print("LensTranslate error:", msg, file=sys.stderr)

    def _auto_fire(self):
        if self.auto:
            self.translate_now()

    def _invalidate(self):
        """Lens moved / resized: the old translation no longer matches."""
        if not self._ready:
            return
        self.gen += 1
        if self.blocks:
            self.blocks = []
            self.update()
        self.auto_timer.start()

    def moveEvent(self, e):
        super().moveEvent(e)
        self._invalidate()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._invalidate()

    # ---- mouse / keyboard -------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        self.activateWindow()
        self.setFocus()
        edges = self._edges(e.position().toPoint())
        self._drag = (e.globalPosition().toPoint(), self.geometry(), edges)

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        if not (e.buttons() & Qt.LeftButton) or not self._drag:
            ed = self._edges(pos)
            both = (ed & Qt.LeftEdge and ed & Qt.BottomEdge) or (ed & Qt.RightEdge and ed & Qt.BottomEdge)
            if both:
                cur = Qt.SizeBDiagCursor if ed & Qt.LeftEdge else Qt.SizeFDiagCursor
            elif ed & (Qt.LeftEdge | Qt.RightEdge):
                cur = Qt.SizeHorCursor
            elif ed & Qt.BottomEdge:
                cur = Qt.SizeVerCursor
            else:
                cur = Qt.SizeAllCursor if pos.y() < self.TOP else Qt.CrossCursor
            self.setCursor(cur)
            return
        start, geo, edges = self._drag
        d = e.globalPosition().toPoint() - start
        if not edges:
            self.move(geo.topLeft() + d)
            return
        x, y, w, h = geo.x(), geo.y(), geo.width(), geo.height()
        if edges & Qt.RightEdge:
            w = max(self.MIN_W, geo.width() + d.x())
        if edges & Qt.BottomEdge:
            h = max(self.MIN_H, geo.height() + d.y())
        if edges & Qt.LeftEdge:
            w = max(self.MIN_W, geo.width() - d.x())
            x = geo.right() - w + 1
        self.setGeometry(x, y, w, h)

    def mouseReleaseEvent(self, _):
        self._drag = None

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.translate_now()

    def wheelEvent(self, e):
        k = 1.07 if e.angleDelta().y() > 0 else 1 / 1.07
        c = self.geometry().center()
        w = max(self.MIN_W, int(self.width() * k))
        h = max(self.MIN_H, int(self.height() * k))
        self.setGeometry(c.x() - w // 2, c.y() - h // 2, w, h)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.translate_now()
        elif e.key() == Qt.Key_T:
            self.show_overlay = not self.show_overlay
            self.update()
        elif e.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(e)

    # ---- context menu -----------------------------------------------------
    def contextMenuEvent(self, e):
        m = QMenu(self)
        m.addAction("Translate now  (Space)", self.translate_now)
        m.addAction("Toggle original / translation  (T)",
                    lambda: (setattr(self, "show_overlay", not self.show_overlay), self.update()))
        m.addSeparator()

        tm = m.addMenu("Translate to")
        grp = QActionGroup(tm)
        for name in LANGS:
            a = QAction(name, tm, checkable=True, checked=(name == self.target))
            a.triggered.connect(lambda _=False, n=name: self._set_target(n))
            grp.addAction(a)
            tm.addAction(a)

        sm = m.addMenu("Language of the text under the lens")
        for name in LANGS:
            a = QAction(name, sm, checkable=True, checked=(name in self.sources))
            a.triggered.connect(lambda checked, n=name: self._toggle_source(n, checked))
            sm.addAction(a)

        auto = QAction("Auto-translate when the lens stops moving", m,
                       checkable=True, checked=self.auto)
        auto.triggered.connect(self._set_auto)
        m.addAction(auto)
        m.addSeparator()
        m.addAction("Text font...  (default: system, sized like the original)", self._pick_font)
        if self.font_family:
            m.addAction("Use system font again", self._reset_font)
        self._border_menu(m)
        m.addSeparator()
        m.addAction("Show log...", self._show_log)
        m.addSeparator()
        m.addAction("Quit  (Esc)", self.close)
        m.exec(e.globalPos())

    def _show_log(self):
        if getattr(self, "_logwin", None) is None:
            self._logwin = LogWindow()
        self._logwin.show()
        self._logwin.raise_()

    @staticmethod
    def _clamp(v, lo, hi):
        try:
            return max(lo, min(hi, int(v)))
        except (TypeError, ValueError):
            return lo

    def _set_border(self, attr, v):
        setattr(self, attr, v)
        self._invalidate()
        self.update()

    def _border_menu(self, m):
        bm = m.addMenu("Window borders")
        for title, attr, sizes in (("Top bar", "TOP", (1, 2, 4, 6, 10, 16, 24, 32)),
                                   ("Sides / bottom", "M", (0.5, 1, 2, 3, 5, 8, 12, 16))):
            sub = bm.addMenu(title)
            grp = QActionGroup(sub)
            for px in sizes:
                a = QAction(f"{px} px", sub, checkable=True, checked=(getattr(self, attr) == px))
                a.triggered.connect(lambda _=False, at=attr, v=px: self._set_border(at, v))
                grp.addAction(a)
                sub.addAction(a)
        bm.addAction("Reset borders", lambda: (self._set_border("TOP", Lens.TOP),
                                               self._set_border("M", Lens.M)))

    def _set_target(self, name):
        self.target = name
        self.translate_now()

    def _toggle_source(self, name, checked):
        if checked and name not in self.sources:
            self.sources.append(name)
        elif not checked and name in self.sources and len(self.sources) > 1:
            self.sources.remove(name)
        self.update()

    def _pick_font(self):
        ok, f = QFontDialog.getFont(QFont(self.font_family) if self.font_family else self.font(), self)
        if ok:
            self.font_family = f.family()
            self._relayout()

    def _reset_font(self):
        self.font_family = ""
        self._relayout()

    def _relayout(self):
        for b in self.blocks:
            b.layout = None
        self.update()

    def _set_auto(self, v):
        self.auto = bool(v)

    # ---- shutdown ---------------------------------------------------------
    def closeEvent(self, e):
        if getattr(self, "_logwin", None) is not None:
            self._logwin.close()
        g = self.geometry()
        self.settings.setValue("geometry", f"{g.x()},{g.y()},{g.width()},{g.height()}")
        self.settings.setValue("target", self.target)
        self.settings.setValue("sources", ",".join(self.sources))
        self.settings.setValue("auto", self.auto)
        self.settings.setValue("font", self.font_family)
        self.settings.setValue("top", self.TOP)
        self.settings.setValue("side", self.M)
        self.gen += 1
        for w in list(self.workers):
            if w.isRunning() and not w.wait(5000):
                w.terminate()
                w.wait(1000)
        super().closeEvent(e)
        QApplication.quit()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("LensTranslate")
    lens = Lens()
    lens.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

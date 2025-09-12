
# -*- coding: utf-8 -*-
import os, sys, time, threading, webbrowser, importlib, importlib.util, pathlib
import requests
import signal
import subprocess
import re
import json
import socket
from functools import lru_cache
import hashlib, io, pathlib, functools
from concurrent.futures import ThreadPoolExecutor
from posters_gui import poster
from gui_main_apperance import (
    setup_graphics_env,      # <-- NOWE
    apply_theme,             # <-- NOWE
    #_DARK_COLORS,            # jeśli używasz
    add_drop_shadow as _add_drop_shadow,   # jeżeli masz nową nazwę w module
    make_card as _card_container,          # <-- NOWE: alias bez ruszania reszty kodu
    progress_bar as _progress_bar,         # jeżeli masz nową nazwę w module
    hline as _hline,                       # jeżeli masz nową nazwę w module
    PrettyStatusBar,
    LoadingSplash,
    style_danger,            # <-- NOWE
    style_success,           # <-- NOWE
    style_accent,            # <-- NOWE
    style_tab_button,        # <-- NOWE
)




APP_NAME = "Piotrflix"
APP_VERSION = "1.0.7"
BIND_HOST = os.environ.get("PFLIX_BIND", "0.0.0.0")
BIND_PORT = int(os.environ.get("PFLIX_PORT", "5000"))

BACKEND_URL = f"http://127.0.0.1:{BIND_PORT}/"
STATUS_URL  = f"http://127.0.0.1:{BIND_PORT}/status"

BASE_DIR = os.path.abspath(".")
APP_ICON_PATH = os.path.join(BASE_DIR, "static", "icon.png")
APP_LOGO_PATH = os.path.join(BASE_DIR, "static", "logo.png")
_EXITING = False
_SHUTDOWN_THREAD = None
_SHUTDOWN_WORKER = None
GITHUB_OWNER = os.environ.get("PFLIX_GH_OWNER", "pkawa95")
GITHUB_REPO  = os.environ.get("PFLIX_GH_REPO",  "PiotrFlix")
ALLOW_PRERELEASES = False
setup_graphics_env()


# ───────────────────────────── grafika / flagi ───────────────────────────────
# >>> ADD: prosty wybór klienta Plex i start castu
def _cast_start(item_id: str, title: str | None = None, parent=None):
    try:
        r = requests.get(f"{BACKEND_URL}plex/players", timeout=8)
        j = r.json() if r.ok else {}
        devices = j.get("devices") or []
        if not devices:
            _error("❌ Brak dostępnych klientów Plex.", parent);
            return
        names = [f"{d.get('name', 'Plex Client')} ({d.get('platform', '')})" for d in devices]
        idx, ok = QtWidgets.QInputDialog.getItem(parent, "Wybierz urządzenie", "Klient Plex:", names, 0, False)
        if not ok: return
        pick = devices[names.index(idx)]
        payload = {"client_id": pick.get("id"), "item_id": str(item_id)}
        rr = requests.post(f"{BACKEND_URL}plex/cast/start", json=payload, timeout=10)
        if rr.ok and not rr.json().get("error"):
            QtWidgets.QMessageBox.information(parent, "CAST", f"▶️ Odtwarzam: {title or item_id}")
        else:
            _error("❌ Nie udało się uruchomić odtwarzania.", parent)
    except Exception as e:
        _error(f"❌ Błąd CAST: {e}", parent)
def _user_state_dir(app_name: str = APP_NAME) -> str:
    if sys.platform.startswith("win"):
        root = os.environ.get("APPDATA") or os.path.expanduser(r"~\\AppData\\Roaming")
    elif sys.platform == "darwin":
        root = os.path.expanduser("~/Library/Application Support")
    else:
        root = os.path.expanduser("~/.local/share")
    d = os.path.join(root, app_name, "state")
    os.makedirs(d, exist_ok=True)
    return d

STATE_DIR = _user_state_dir()

POSTERS_DIR = os.path.join(STATE_DIR, "posters")
os.makedirs(POSTERS_DIR, exist_ok=True)
POSTER_EXTS = ("jpg", "jpeg", "png", "webp", "avif")

def _slugify_title(t: str) -> str:
    t = (t or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^a-z0-9]+", "-", t)
    return t.strip("-")

def _find_local_poster(meta: dict) -> str | None:
    try:
        # 0) relatywne /static/... albo static/...
        thumb_rel = str(meta.get("thumb") or "")
        if thumb_rel.startswith("/static/posters/") or thumb_rel.startswith("static/posters/"):
            cand = os.path.join(POSTERS_DIR, os.path.basename(thumb_rel))
            if os.path.isfile(cand):
                return cand

        # 1) bezpośrednie pola
        for k in ("poster_path", "local_poster", "poster_file"):
            p = meta.get(k)
            if p and os.path.isfile(p):
                return p
            if p:
                cand = os.path.join(POSTERS_DIR, os.path.basename(p))
                if os.path.isfile(cand):
                    return cand

        # 2) po id
        ids = [str(meta.get(k) or "") for k in ("id", "tmdb_id", "imdb_id", "ratingKey")]
        ids = [x for x in ids if x]
        for ident in ids:
            for ext in POSTER_EXTS:
                p = os.path.join(POSTERS_DIR, f"{ident}.{ext}")
                if os.path.isfile(p):
                    return p

        # 3) po tytule (slug)
        title = meta.get("title") or meta.get("name")
        if title:
            slug = _slugify_title(title)
            if os.path.isdir(POSTERS_DIR):
                for fn in os.listdir(POSTERS_DIR):
                    low = fn.lower()
                    if slug in low and low.rsplit(".", 1)[-1] in POSTER_EXTS:
                        p = os.path.join(POSTERS_DIR, fn)
                        if os.path.isfile(p):
                            return p
    except Exception:
        pass
    return None

# ───────────────────────────── WERSJA: zapis atomowy ─────────────────────────
def _app_root_dir() -> str:
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else BASE_DIR

def _localappdata_dir() -> str:
    lad = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    return os.path.join(lad, APP_NAME)

def _write_json_atomic(path: str, payload: dict):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass

def ensure_version_files():
    data = {"version": APP_VERSION}
    app_root = _app_root_dir()
    la_dir   = _localappdata_dir()
    for p in [os.path.join(app_root, "version.json"), os.path.join(la_dir, "version.json")]:
        need = True
        try:
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    cur = json.load(f) or {}
                if str(cur.get("version", "")).strip() == APP_VERSION:
                    need = False
        except Exception:
            pass
        if need:
            _write_json_atomic(p, data)

# ───────────────────────────────────────── Qt ────────────────────────────────
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

# config + onboarding
from config_store import config_exists, save_config
from gui_onboarding import run_onboarding



# ───────────────────────────── util: wymuś konfigurację ─────────────────────
def ensure_configuration() -> bool:
    try:
        if config_exists():
            print("✅ Konfiguracja istnieje.")
            return True
        print("ℹ️ Brak konfiguracji – uruchamiam onboarding…")
        new_cfg = run_onboarding()
        if not new_cfg:
            print("❌ Konfiguracja przerwana – zamykam.")
            return False
        save_config(new_cfg)
        print("✅ Konfiguracja zapisana – kontynuuję start.")
        return True
    except Exception as e:
        print(f"❌ ensure_configuration: {e}")
        return False

# ──────────────────────────── RESET/SHUTDOWN – core ─────────────────────────
BACKEND_MOD = None
FLASK_THREAD = None

def _delete_config_files():
    try:
        import config_store as _cfg
        if hasattr(_cfg, "delete_config"):
            _cfg.delete_config()
    except Exception:
        pass
    for p in [
        os.path.join(os.getcwd(), "config.json"),
        os.path.join(os.path.dirname(__file__), "config.json"),
        os.path.join(STATE_DIR, "config.json"),
    ]:
        try:
            if os.path.isfile(p):
                os.remove(p)
        except Exception:
            pass

def _flush_backend_quietly(timeout_sec: float = 6.0):
    called = False
    try:
        if BACKEND_MOD:
            if hasattr(BACKEND_MOD, "graceful_shutdown"):
                BACKEND_MOD.graceful_shutdown(); called = True
            elif hasattr(BACKEND_MOD, "_graceful_shutdown"):
                BACKEND_MOD._graceful_shutdown(); called = True
            elif hasattr(BACKEND_MOD, "tclient") and getattr(BACKEND_MOD, "tclient"):
                try:
                    BACKEND_MOD.tclient.shutdown(); called = True
                except Exception:
                    pass
    except Exception:
        pass
    if not called:
        try:
            requests.post(BACKEND_URL.rstrip("/") + "/admin/shutdown",
                          json={"reason": "launcher-exit"}, timeout=1.0)
        except Exception:
            pass
    t0 = time.time()
    while time.time() - t0 < max(0.0, timeout_sec):
        time.sleep(0.05)

CREATE_NO_WINDOW         = 0x08000000
DETACHED_PROCESS         = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

def _find_updater() -> str | None:
    exe_dir  = os.path.dirname(sys.executable)
    base_dir = BASE_DIR
    for p in [
        os.path.join(exe_dir, "updater.exe"),
        os.path.join(exe_dir, "tools", "updater.exe"),
        os.path.join(base_dir, "updater.exe"),
        os.path.join(base_dir, "tools", "updater.exe"),
    ]:
        if os.path.isfile(p):
            return p
    return None

def _update_now():
    try:
        app_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
                  else os.path.dirname(os.path.abspath(__file__))
        updater_path = os.path.join(app_dir, "updater.exe")
        if not os.path.isfile(updater_path):
            maybe = _find_updater()
            if maybe:
                updater_path = maybe
        if os.path.isfile(updater_path):
            subprocess.Popen([updater_path], close_fds=True,
                             creationflags=(CREATE_NO_WINDOW if os.name == "nt" else 0))
    except Exception:
        pass
    quit_app_graceful()

def quit_app_graceful():
    global _EXITING, _SPLASH, _MAIN_WIN, _SHUTDOWN_THREAD, _SHUTDOWN_WORKER
    if _EXITING:
        return
    _EXITING = True
    app = QtWidgets.QApplication.instance() or _ensure_qapp()
    try:
        if _MAIN_WIN is not None:
            _MAIN_WIN.hide()
            if getattr(_MAIN_WIN, "tray", None):
                _MAIN_WIN.tray.hide()
    except Exception:
        pass
    if _SPLASH is None or not isinstance(_SPLASH, LoadingSplash):
        _SPLASH = LoadingSplash(app_name=APP_NAME, icon_path=APP_ICON_PATH)
    try:
        _SPLASH.title.setText(APP_NAME)
    except Exception:
        pass
    _SPLASH.set_status("Trwa zapisywanie ustawień… Proszę czekać")
    _SPLASH.show()
    app.processEvents()
    _SHUTDOWN_THREAD = QtCore.QThread()
    _SHUTDOWN_WORKER = ShutdownWorker()
    _SHUTDOWN_WORKER.moveToThread(_SHUTDOWN_THREAD)
    _SHUTDOWN_THREAD.started.connect(_SHUTDOWN_WORKER.run)
    _SHUTDOWN_WORKER.progress.connect(lambda msg: _SPLASH.set_status(msg))
    _SHUTDOWN_WORKER.finished.connect(_SHUTDOWN_THREAD.quit)
    _SHUTDOWN_WORKER.finished.connect(lambda: QtCore.QTimer.singleShot(50, lambda: app.quit()))
    _SHUTDOWN_THREAD.finished.connect(_SHUTDOWN_WORKER.deleteLater)
    _SHUTDOWN_THREAD.finished.connect(_SHUTDOWN_THREAD.deleteLater)
    _SHUTDOWN_THREAD.start()

# >>> ADD: prosty FlowLayout (układ „chipsów”)
class FlowLayout(QtWidgets.QLayout):
    def __init__(self, parent=None, margin=0, spacing=6):
        super().__init__(parent)
        self.itemList = []
        self.setContentsMargins(margin, margin, margin, margin)
        self._hSpacing = spacing
        self._vSpacing = spacing
    def addItem(self, item): self.itemList.append(item)
    def count(self): return len(self.itemList)
    def itemAt(self, i): return self.itemList[i] if 0 <= i < len(self.itemList) else None
    def takeAt(self, i): return self.itemList.pop(i) if 0 <= i < len(self.itemList) else None
    def expandingDirections(self): return Qt.Orientations(Qt.Orientation(0))
    def hasHeightForWidth(self): return True
    def heightForWidth(self, w): return self.doLayout(QtCore.QRect(0, 0, w, 0), True)
    def setGeometry(self, r): super().setGeometry(r); self.doLayout(r, False)
    def sizeHint(self): return self.minimumSize()
    def minimumSize(self):
        s = QtCore.QSize()
        for i in self.itemList:
            s = s.expandedTo(i.minimumSize())
        s += QtCore.QSize(2*self.contentsMargins().top(), 2*self.contentsMargins().top())
        return s
    def doLayout(self, rect, testOnly):
        x = rect.x(); y = rect.y(); lineHeight = 0
        for item in self.itemList:
            wid = item.widget(); spaceX = self._hSpacing; spaceY = self._vSpacing
            nextX = x + item.sizeHint().width() + spaceX
            if nextX - spaceX > rect.right() and lineHeight > 0:
                x = rect.x(); y = y + lineHeight + spaceY; nextX = x + item.sizeHint().width() + spaceX; lineHeight = 0
            if not testOnly: item.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), item.sizeHint()))
            x = nextX; lineHeight = max(lineHeight, item.sizeHint().height())
        return y + lineHeight - rect.y()

def reset_app_full():
    try:
        _flush_backend_quietly()
    finally:
        _delete_config_files()
        python = sys.executable
        args = [python] + sys.argv
        env = dict(os.environ)
        os.execve(python, args, env)

# ─────────────────────────────── Okno Ustawień ──────────────────────────────
class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ustawienia")
        if os.path.isfile(APP_ICON_PATH):
            self.setWindowIcon(QtGui.QIcon(APP_ICON_PATH))
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16); lay.setSpacing(12)

        box = QtWidgets.QFrame(); box.setObjectName("card")
        _add_drop_shadow(box, 24, 0.35)
        form = QtWidgets.QVBoxLayout(box); form.setContentsMargins(16,16,16,16); form.setSpacing(8)

        self.chk_autostart = QtWidgets.QCheckBox("Uruchamiaj przy starcie systemu (Windows)")
        self.chk_min_to_tray = QtWidgets.QCheckBox("Kliknięcie w krzyżyk minimalizuje do traya")
        form.addWidget(self.chk_autostart); form.addWidget(self.chk_min_to_tray)

        row = QtWidgets.QHBoxLayout()
        self.btn_reset = QtWidgets.QPushButton("Zresetuj aplikację"); self.btn_reset.setObjectName("danger")
        self.btn_update = QtWidgets.QPushButton("Sprawdź aktualizacje")
        row.addWidget(self.btn_reset); row.addStretch(1); row.addWidget(self.btn_update)
        form.addLayout(row)
        lay.addWidget(box)

        row2 = QtWidgets.QHBoxLayout(); row2.addStretch(1)
        ok = QtWidgets.QPushButton("OK"); ok.setProperty("default", True)
        cancel = QtWidgets.QPushButton("Anuluj")
        row2.addWidget(cancel); row2.addWidget(ok); lay.addLayout(row2)
        ok.clicked.connect(self.accept); cancel.clicked.connect(self.reject)
        self.btn_update.clicked.connect(_update_now)
        self.btn_reset.clicked.connect(self._reset)
        self.chk_autostart.setChecked(False)
        self.chk_min_to_tray.setChecked(True)

    def accept(self):
        super().accept()

    def _reset(self):
        if QtWidgets.QMessageBox.question(
            self, "Potwierdź", "Usunąć konfigurację i zrestartować?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No
        ) != QtWidgets.QMessageBox.Yes:
            return
        reset_app_full()

# ──────────────────────────────── Wspólne utils ─────────────────────────────
def _format_speed(bps: float) -> str:
    kbps = bps / 1024.0
    return (f"{kbps/1024.0:.2f} MB/s") if kbps >= 1024 else (f"{kbps:.2f} KB/s")

def _error(msg: str, parent=None):
    QtWidgets.QMessageBox.critical(parent, "Błąd", msg)

def _confirm(msg: str, parent=None) -> bool:
    return QtWidgets.QMessageBox.question(parent, "Potwierdź", msg,
        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No
    ) == QtWidgets.QMessageBox.Yes

def _abs_img(img_url: str | None) -> str | None:
    if not img_url or not isinstance(img_url, str):
        return img_url
    u = img_url.strip()
    # ścieżki względne typu "static/posters/..." lub "posters/..."
    if u.startswith("static/") or u.startswith("posters/"):
        return BACKEND_URL.rstrip("/") + "/" + u
    # ścieżki zaczynające od "/" (np. /static/... lub plexowe /library/...)
    if u.startswith("/"):
        return BACKEND_URL.rstrip("/") + u
    # protokół względny //cdn...
    if u.startswith("//"):
        return "http:" + u
    # już absolutny http(s) lub ścieżka do pliku -> bez zmian
    return u


class ShutdownWorker(QtCore.QObject):
    progress = QtCore.Signal(str)
    finished = QtCore.Signal()

    @QtCore.Slot()
    def run(self):
        # komunikat startowy
        self.progress.emit("Trwa zapisywanie ustawień… Proszę czekać")
        try:
            _flush_backend_quietly(timeout_sec=8.0)
        except Exception:
            # nic nie robimy — zamykamy „po cichu”
            pass

        # komunikat końcowy i lekkie opóźnienie dla UX
        self.progress.emit("Zamykanie… Do zobaczenia!")
        QtCore.QThread.msleep(250)
        self.finished.emit()

# >>> ADD: mapowanie motywów badge wg gatunku (klucze obie formy PL/EN, normalizowane)
_GENRE_STYLES = {
    'horror': ('#0b0004', '#ff2a2a'),
    'komedia': ('#ffb300', '#ff5a00'), 'comedy': ('#ffb300', '#ff5a00'),
    'akcja': ('#c600ff', '#00f0ff'), 'action': ('#c600ff', '#00f0ff'),
    'dramat': ('#2b2c6f', '#6a5acd'), 'drama': ('#2b2c6f', '#6a5acd'),
    'thriller': ('#1b1b1b', '#7cff00'),
    'sci-fi': ('#00e0ff', '#00b894'), 'science-fiction': ('#00e0ff', '#00b894'),
    'fantasy': ('#6728ff', '#00e0ff'),
    'romans': ('#ff3d77', '#ff7eb3'), 'romance': ('#ff3d77', '#ff7eb3'),
    'animacja': ('#ff6bd6', '#ff9a3d'), 'animation': ('#ff6bd6', '#ff9a3d'),
    'kryminal': ('#0d0d15', '#f5d000'), 'crime': ('#0d0d15', '#f5d000'),
    'dokument': ('#243949', '#517fa4'), 'documentary': ('#243949', '#517fa4'),
    'familijny': ('#56ccf2', '#2f80ed'), 'family': ('#56ccf2', '#2f80ed'),
    'wojenny': ('#3a6073', '#16222a'), 'war': ('#3a6073', '#16222a'),
    'western': ('#a770ef', '#fdb99b'),
    'history': ('#8360c3', '#2ebf91'),
    'muzyka': ('#ff512f', '#dd2476'), 'music': ('#ff512f', '#dd2476'),
    'mystery': ('#0f2027', '#203a43'),
    'przygodowy': ('#00b09b', '#96c93d'), 'adventure': ('#00b09b', '#96c93d'),
}

def _norm_genre_key(name: str) -> str:
    t = (name or "").strip().lower()
    t = t.replace("ś","s").replace("ł","l").replace("ó","o").replace("ą","a") \
         .replace("ę","e").replace("ż","z").replace("ź","z").replace("ć","c").replace("ń","n")
    return t

def _make_badge(text: str) -> QtWidgets.QLabel:
    key = _norm_genre_key(text)
    c1, c2 = _GENRE_STYLES.get(key, ('#6a6aff', '#39d1ff'))
    lab = QtWidgets.QLabel(text)
    lab.setStyleSheet(f"""
        QLabel {{
          color: white; font-weight: 700; font-size: 11px;
          padding: 3px 8px; border-radius: 999px;
          background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 {c1}, stop:1 {c2});
        }}
    """)
    lab.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
    return lab

# >>> ADD: cache gatunków z backendu
_GENRES_CACHE = {}  # id -> [genres]

def _fetch_genres_for_id(obj_id: str) -> list[str]:
    obj_id = str(obj_id)
    if obj_id in _GENRES_CACHE:
        return _GENRES_CACHE[obj_id]
    try:
        r = requests.get(f"{BACKEND_URL}genres/for-id/{obj_id}", timeout=6)
        j = r.json() if r.ok else {}
        arr = j.get("genres") or []
        arr = [str(x) for x in arr if x]
        _GENRES_CACHE[obj_id] = arr
        return arr
    except Exception:
        _GENRES_CACHE[obj_id] = []
        return []


# ──────────────────────────────── Strona: Torrenty ──────────────────────────
class TorrentsPage(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(12,12,12,12); v.setSpacing(10)

        # ── filtr górny
        topCard = _card_container()
        topLay = QtWidgets.QHBoxLayout(topCard)
        topLay.setContentsMargins(14,12,14,12); topLay.setSpacing(10)
        lblSort = QtWidgets.QLabel("Sortuj:")
        self.sort = QtWidgets.QComboBox(); self.sort.addItems(["name","progress","state"])
        topLay.addWidget(lblSort); topLay.addWidget(self.sort)
        topLay.addSpacing(12)
        topLay.addWidget(QtWidgets.QLabel("🌐 Limit pobierania:"))
        self.limit = QtWidgets.QComboBox()
        self.limit.addItems(["Unlimited","1 MB/s","2 MB/s","5 MB/s","10 MB/s","15 MB/s"])
        topLay.addWidget(self.limit, 1)
        self.feedback = QtWidgets.QLabel("Aktualnie: Unlimited"); self.feedback.setObjectName("subtle")
        topLay.addWidget(self.feedback, 1, Qt.AlignRight)
        v.addWidget(topCard)

        # ── nagłówek listy
        self.summary = QtWidgets.QLabel("—")
        self.summary.setStyleSheet("font-weight:600; margin:6px 0;")
        v.addWidget(self.summary)

        # ── zakładki (pigułki) + EXCLUSIVE
        tabsCard = _card_container()
        tabsRow = QtWidgets.QHBoxLayout(tabsCard); tabsRow.setContentsMargins(10,8,10,8)
        self.btnActive  = QtWidgets.QPushButton("⚡ AKTYWNE")
        self.btnHistory = QtWidgets.QPushButton("📁 HISTORIA")
        for b in (self.btnActive, self.btnHistory):
            b.setCheckable(True); b.setObjectName("pill")
        self.tabGroup = QtWidgets.QButtonGroup(self)
        self.tabGroup.setExclusive(True)
        self.tabGroup.addButton(self.btnActive, 0)
        self.tabGroup.addButton(self.btnHistory, 1)
        self.btnActive.setChecked(True)

        # styl pigułek
        from gui_main_apperance import style_tab_button
        style_tab_button(self.btnActive,  active=True)
        style_tab_button(self.btnHistory, active=False)

        # reaguj na przełączenie (zmiana stylu + odświeżenie listy)
        self.tabGroup.idToggled.connect(self._on_tab_toggled)

        tabsRow.addWidget(self.btnActive)
        tabsRow.addWidget(self.btnHistory)
        tabsRow.addStretch(1)
        v.addWidget(tabsCard)

        # ── karta z listą torrentów
        listCard = _card_container()
        listLay = QtWidgets.QVBoxLayout(listCard); listLay.setContentsMargins(10,10,10,10)
        self.list = QtWidgets.QListWidget(); self.list.setSpacing(8)
        listLay.addWidget(self.list)
        v.addWidget(listCard, 1)

        # ── connections
        self.sort.currentTextChanged.connect(self.refresh_now)
        self.limit.currentIndexChanged.connect(self._apply_limit)

        # ── timer odświeżania
        self.timer = QtCore.QTimer(self); self.timer.setInterval(2000)
        self.timer.timeout.connect(self.refresh_now); self.timer.start()

        # ── przywrócenie limitu
        QtCore.QTimer.singleShot(300, self._restore_limit)

        # start
        self._on_tab_toggled(0, True)

    # ───────────────────────── helpers ─────────────────────────
    def _on_tab_toggled(self, bid: int, on: bool):
        if not on:
            return
        # styl aktywnej pigułki
        from gui_main_apperance import style_tab_button
        style_tab_button(self.btnActive,  active=(bid == 0))
        style_tab_button(self.btnHistory, active=(bid == 1))
        self.refresh_now()

    def _restore_limit(self):
        try:
            s = QtCore.QSettings(APP_NAME, APP_NAME)
            val = s.value("global-speed-limit", "Unlimited")
            idx = self.limit.findText(val)
            if idx >= 0:
                self.limit.setCurrentIndex(idx)
                self._apply_limit()
        except Exception:
            pass

    def _apply_limit(self):
        txt = self.limit.currentText()
        mbs = 0 if "Unlimited" in txt else int(txt.split()[0])
        try:
            r = requests.post(f"{BACKEND_URL}set-global-limit", json={"limit": mbs}, timeout=4)
            j = r.json() if r.ok else {}
            shown = (f"{j.get('limit_mbs')} MB/s") if (j.get("limit_mbs") or 0) > 0 else "Unlimited"
            self.feedback.setText(f"✅ Globalny limit pobierania: {shown}")
            QtCore.QSettings(APP_NAME, APP_NAME).setValue("global-speed-limit", txt)
        except Exception as e:
            self.feedback.setText(f"❌ Błąd ustawiania limitu: {e}")

    def _active_view(self) -> str:
        # 0 = aktywne, 1 = historia
        return "active" if self.tabGroup.checkedId() == 0 else "history"



    # ───────────────────────── refresh ─────────────────────────
    def refresh_now(self):
        try:
            r = requests.get(STATUS_URL, timeout=6)
            data = r.json()
        except Exception as e:
            self.summary.setText(f"❌ Brak połączenia: {e}")
            return

        arr = list(data.items())
        key = self.sort.currentText()
        if key == "progress":
            arr.sort(key=lambda kv: float(kv[1].get("progress", 0.0)))
        elif key == "name":
            arr.sort(key=lambda kv: str(kv[1].get("name","")).lower())
        else:
            arr.sort(key=lambda kv: str(kv[1].get(key,"")))

        total_speed = 0.0; active_count = 0; shown = 0
        self.list.clear()

        for tid, t in arr:
            done = (float(t.get("progress", 0)) >= 100.0)
            if (self._active_view() == "active" and done) or (self._active_view() == "history" and not done):
                continue

            total_speed += float(t.get("download_payload_rate", 0) or 0)
            if t.get("state") == "Downloading":
                active_count += 1

            # ——— karta elementu
            item = QtWidgets.QListWidgetItem()
            wCard = _card_container()
            gl = QtWidgets.QGridLayout(wCard)
            gl.setContentsMargins(12,10,12,12)
            gl.setHorizontalSpacing(10)
            gl.setVerticalSpacing(8)

            name    = QtWidgets.QLabel(f"<b>{t.get('name','')}</b>")
            details = QtWidgets.QLabel(f"📥 {_format_speed(t.get('download_payload_rate',0))} – {t.get('state','')}")
            details.setObjectName("subtle")

            bar = _progress_bar(float(t.get("progress", 0)))

            # ——— przyciski (większe, czytelne, osobny rząd po prawej)
            btnPause   = QtWidgets.QPushButton("⏯️");   btnPause.setObjectName("icon");   btnPause.setToolTip("Pauzuj/Wznów")
            btnDel     = QtWidgets.QPushButton("🗑️");   btnDel.setObjectName("icon");     btnDel.setToolTip("Usuń torrent")
            btnDelData = QtWidgets.QPushButton("🗑️+📁"); btnDelData.setObjectName("icon"); btnDelData.setToolTip("Usuń torrent + dane")
            style_accent(btnPause); style_danger(btnDel); style_danger(btnDelData)
            for b in (btnPause, btnDel, btnDelData):
                b.setFixedSize(40, 36)
                b.setCursor(Qt.PointingHandCursor)

            # ——— układ
            gl.addWidget(name, 0, 0, 1, 5)
            gl.addWidget(details, 1, 0, 1, 3)

            btnRow = QtWidgets.QHBoxLayout()
            btnRow.setSpacing(8)
            btnRow.addStretch(1)
            btnRow.addWidget(btnPause)
            btnRow.addWidget(btnDel)
            btnRow.addWidget(btnDelData)
            btnRow.setContentsMargins(0, 0, 0, 8)  # lekko podnieś przyciski nad progressbarem

            gl.addLayout(btnRow, 1, 3, 1, 2)

            gl.addWidget(bar, 2, 0, 1, 5)  # progress przez całą szerokość

            gl.setColumnStretch(0, 3)  # treść
            gl.setColumnStretch(1, 1)
            gl.setColumnStretch(2, 1)
            gl.setColumnStretch(3, 0)  # przyciski
            gl.setColumnStretch(4, 0)

            item.setSizeHint(QtCore.QSize(0, 104))
            self.list.addItem(item)
            self.list.setItemWidget(item, wCard)

            # ——— akcje
            btnPause.clicked.connect(lambda _, x=tid: self._toggle(x))
            btnDel.clicked.connect(lambda _, x=tid: self._remove(x, False))
            btnDelData.clicked.connect(lambda _, x=tid: self._remove(x, True))
            shown += 1

        self.summary.setText(
            f"📊 Torrenty: {len(arr)}, 🚀 Aktywne: {active_count}, ⚡️ Prędkość: {_format_speed(total_speed)}"
        )
        if shown == 0 and self._active_view() == "active":
            self.summary.setText(self.summary.text() + " • (brak aktywnych)")

    # ───────────────────────── akcje ─────────────────────────
    def _toggle(self, tid: str):
        try:
            requests.post(f"{BACKEND_URL}toggle/{tid}", timeout=6)
            QtCore.QTimer.singleShot(150, self.refresh_now)
        except Exception as e:
            _error(f"Nie udało się przełączyć: {e}", self)

    def _remove(self, tid: str, with_data: bool):
        if not _confirm("Na pewno usunąć torrent?" + (" (+ dane)" if with_data else ""), self):
            return
        try:
            requests.post(f"{BACKEND_URL}remove/{tid}", params={"data": str(with_data).lower()}, timeout=8)
            QtCore.QTimer.singleShot(150, self.refresh_now)
        except Exception as e:
            _error(f"Nie udało się usunąć: {e}", self)

# >>> ADD: dialog losowania
class RandomizerDialog(QtWidgets.QDialog):
    def __init__(self, films_snapshot: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Losowanie filmu")
        self.setModal(True)
        lay = QtWidgets.QVBoxLayout(self); lay.setContentsMargins(12,12,12,12); lay.setSpacing(10)

        # 1) wybór gatunków
        box = _card_container(); bl = QtWidgets.QVBoxLayout(box); bl.setContentsMargins(12,12,12,12)
        self.chk_all = QtWidgets.QCheckBox("Zaznacz wszystkie"); self.chk_all.setChecked(True)
        bl.addWidget(self.chk_all)
        self.scroll = QtWidgets.QScrollArea(); self.scroll.setWidgetResizable(True)
        inner = QtWidgets.QWidget(); self.flow = FlowLayout(inner, spacing=8)
        self.scroll.setWidget(inner); bl.addWidget(self.scroll, 1)
        lay.addWidget(box, 1)

        # 2) animacja + przyciski
        animCard = _card_container(); al = QtWidgets.QVBoxLayout(animCard)
        self.poster = QtWidgets.QLabel(); self.poster.setAlignment(Qt.AlignCenter)
        self.poster.setFixedSize(220, 330)
        al.addWidget(self.poster, alignment=Qt.AlignCenter)
        self.status = QtWidgets.QLabel("Wybierz gatunki i kliknij LOSUJ"); self.status.setAlignment(Qt.AlignCenter)
        al.addWidget(self.status)
        lay.addWidget(animCard)

        btns = QtWidgets.QHBoxLayout()
        self.btnCancel = QtWidgets.QPushButton("Zamknij")
        self.btnGo = QtWidgets.QPushButton("🎲 Losuj"); style_accent(self.btnGo)
        btns.addStretch(1); btns.addWidget(self.btnCancel); btns.addWidget(self.btnGo)
        lay.addLayout(btns)

        self._films = films_snapshot
        self._pool_imgs = []  # (pixmap, id, title)
        self._timer = QtCore.QTimer(self); self._timer.setInterval(90)
        self._timer.timeout.connect(self._spin_once)
        self._spin_idx = 0

        self._build_genres()
        self.chk_all.toggled.connect(self._toggle_all)
        self.btnCancel.clicked.connect(self.reject)
        self.btnGo.clicked.connect(self._start_spin)

    def _build_genres(self):
        # unikalne gatunki z cache (pobierz leniwie)
        genres = set()
        for f in self._films:
            gid = str(f.get("id") or "")
            for g in _fetch_genres_for_id(gid):
                genres.add(g)
        self._checks = []
        for g in sorted(genres, key=lambda x: str(x).lower()):
            w = QtWidgets.QCheckBox(g); w.setChecked(True)
            cw = QtWidgets.QWidget(); hl = QtWidgets.QHBoxLayout(cw); hl.setContentsMargins(0,0,0,0)
            hl.addWidget(_make_badge(g)); hl.addWidget(w); hl.addStretch(1)
            self.flow.addWidget(cw)
            self._checks.append(w)

    def _toggle_all(self, on: bool):
        for c in self._checks: c.setChecked(on)

    def _eligible_films(self) -> list[dict]:
        chosen = {c.text() for c in self._checks if c.isChecked()}
        if not chosen: return []
        out = []
        for f in self._films:
            if int(float(f.get("progress", 0))) >= 100:  # pomijamy obejrzane na 100%
                continue
            gid = str(f.get("id") or "")
            gs = set(_fetch_genres_for_id(gid))
            if gs & chosen:
                out.append(f)
        return out

    def _start_spin(self):
        cand = self._eligible_films()
        if not cand:
            self.status.setText("Brak kandydatów w wybranych gatunkach.")
            return
        # przygotuj „bęben” z plakatami
        self._pool_imgs = []
        for f in cand[:40]:  # ogranicz do ~40 dla płynności
            pm = QtGui.QPixmap(220, 330); pm.fill(QtGui.QColor(20,20,25))
            thumb = _find_local_poster(f) or _abs_img(f.get("thumb") or f.get("poster") or f.get("image"))
            try:
                if thumb and str(thumb).startswith("http"):
                    # szybkie pobranie do pixmap (bez blokowania UX nadmiernie)
                    try:
                        rr = requests.get(thumb, timeout=2)
                        if rr.ok:
                            pm.loadFromData(rr.content)
                    except Exception:
                        pass
            except Exception:
                pass
            self._pool_imgs.append((pm, str(f.get("id") or ""), f.get("title") or ""))

        if not self._pool_imgs:
            self.status.setText("Brak obrazków – ale losowanie zadziała.")
        self.status.setText("🎰 Losuję…")
        self._spin_idx = 0
        self._timer.start()
        QtCore.QTimer.singleShot(2400, self._stop_and_show)  # ~2.4s animacji

    def _spin_once(self):
        if not self._pool_imgs: return
        self._spin_idx = (self._spin_idx + 1) % len(self._pool_imgs)
        pm, _, _ = self._pool_imgs[self._spin_idx]
        self.poster.setPixmap(pm.scaled(self.poster.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _stop_and_show(self):
        self._timer.stop()
        # wybór wyniku
        cand = self._eligible_films()
        if not cand:
            self.status.setText("Brak kandydatów.")
            return
        import random
        pick = random.choice(cand)
        self.status.setText(f"🎉 Wylosowano: {pick.get('title','—')}")
        # przyciski CAST/Zamknij
        cast = QtWidgets.QPushButton("📺 CAST")
        style_accent(cast)
        cast.clicked.connect(lambda: (_cast_start(str(pick.get('id')), pick.get('title'), self), self.accept()))
        bb = QtWidgets.QDialogButtonBox(Qt.Horizontal)
        bb.addButton(cast, QtWidgets.QDialogButtonBox.ActionRole)
        bb.addButton("Zamknij", QtWidgets.QDialogButtonBox.RejectRole)
        bb.rejected.connect(self.reject)
        # wstaw pod status (jednorazowo)
        self.layout().addWidget(bb)

# >>> ADD: dialog filtra
class FilterDialog(QtWidgets.QDialog):
    def __init__(self, films_snapshot: list[dict], active_genres: set[str], text_query: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Filtr – Dostępne (filmy)")
        self.setModal(True)
        lay = QtWidgets.QVBoxLayout(self); lay.setContentsMargins(12,12,12,12); lay.setSpacing(10)

        # tekst
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Wyszukaj:"))
        self.q = QtWidgets.QLineEdit(text_query or ""); row.addWidget(self.q, 1)
        lay.addLayout(row)

        # gatunki (checkboxy)
        box = _card_container(); bl = QtWidgets.QVBoxLayout(box); bl.setContentsMargins(12,12,12,12)
        self.chk_all = QtWidgets.QCheckBox("Zaznacz wszystkie"); bl.addWidget(self.chk_all)
        self.scroll = QtWidgets.QScrollArea(); self.scroll.setWidgetResizable(True)
        inner = QtWidgets.QWidget(); self.flow = FlowLayout(inner, spacing=8)
        self.scroll.setWidget(inner); bl.addWidget(self.scroll, 1)
        lay.addWidget(box, 1)

        btns = QtWidgets.QDialogButtonBox(Qt.Horizontal)
        ok = btns.addButton("Zastosuj", QtWidgets.QDialogButtonBox.AcceptRole)
        cancel = btns.addButton("Anuluj", QtWidgets.QDialogButtonBox.RejectRole)
        lay.addWidget(btns)
        cancel.clicked.connect(self.reject); ok.clicked.connect(self.accept)
        self.chk_all.toggled.connect(self._toggle_all)

        # zbuduj listę gatunków
        genres = set()
        for f in films_snapshot:
            gid = str(f.get("id") or "")
            for g in _fetch_genres_for_id(gid):
                genres.add(g)
        self._checks = []
        for g in sorted(genres, key=lambda x: str(x).lower()):
            w = QtWidgets.QCheckBox(g); w.setChecked((not active_genres) or (g in active_genres))
            cw = QtWidgets.QWidget(); hl = QtWidgets.QHBoxLayout(cw); hl.setContentsMargins(0,0,0,0)
            hl.addWidget(_make_badge(g)); hl.addWidget(w); hl.addStretch(1)
            self.flow.addWidget(cw); self._checks.append(w)

    def _toggle_all(self, on: bool):
        for c in self._checks: c.setChecked(on)

    def selected_genres(self) -> set[str]:
        return {c.text() for c in self._checks if c.isChecked()}

    def text_query(self) -> str:
        return self.q.text().strip().lower()



# ──────────────────────────────── Strona: Szukaj ────────────────────────────
class SearchPage(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(12,12,12,12); v.setSpacing(10)

        # Tabs: movies, premium, series
        self.typeTabs = QtWidgets.QTabBar()
        self.typeTabs.addTab("🎬 Filmy"); self.typeTabs.addTab("💎 Filmy+"); self.typeTabs.addTab("📺 Seriale")
        v.addWidget(self.typeTabs)

        formCard = _card_container()
        form = QtWidgets.QHBoxLayout(formCard); form.setContentsMargins(12,12,12,12); form.setSpacing(10)
        self.query = QtWidgets.QLineEdit(); self.query.setPlaceholderText("Szukaj tytułu")
        self.quality = QtWidgets.QComboBox(); self.quality.addItems(["Wszystkie","720p","1080p","2160p"])
        self.btnSearch = QtWidgets.QPushButton("🔍 Szukaj"); self.btnSearch.setObjectName("pill")
        style_accent(self.btnSearch)
        form.addWidget(self.query, 3); form.addWidget(QtWidgets.QLabel("Jakość:")); form.addWidget(self.quality)
        form.addWidget(self.btnSearch)
        v.addWidget(formCard)

        listCard = _card_container()
        listLay = QtWidgets.QVBoxLayout(listCard); listLay.setContentsMargins(10,10,10,10); listLay.setSpacing(8)
        self.status = QtWidgets.QLabel(""); listLay.addWidget(self.status)
        self.list = QtWidgets.QListWidget(); self.list.setSpacing(8)
        listLay.addWidget(self.list, 1)
        v.addWidget(listCard, 1)

        self.btnSearch.clicked.connect(self._search)

    def _endpoint(self) -> str:
        idx = self.typeTabs.currentIndex()
        return {0:"search", 1:"search-premium", 2:"search-series"}.get(idx, "search")

    def _search(self):
        self.list.clear();
        self.status.setText("🔄 Szukam…")
        endpoint = self._endpoint()
        q = self.query.text().strip()
        qual = self.quality.currentText()
        if qual == "Wszystkie": qual = ""
        try:
            r = requests.post(f"{BACKEND_URL}{endpoint}", data={"query": q, "quality": qual}, timeout=20)
            j = r.json()
            self.status.setText("")
            results = j.get("results", [])
            if not results:
                self.status.setText("❌ Nic nie znaleziono.");
                return
            for res in results:
                item = QtWidgets.QListWidgetItem()
                wCard = _card_container()
                gl = QtWidgets.QGridLayout(wCard)
                gl.setContentsMargins(12, 10, 12, 10)
                gl.setHorizontalSpacing(10)
                gl.setVerticalSpacing(6)

                # 1) mini-poster po lewej
                thumb = poster.create_label(68, 102, radius=10)
                gl.addWidget(thumb, 0, 0, 3, 1)

                # 2) treść
                title = QtWidgets.QLabel(f"<b>{res.get('title', '')}</b>")
                desc = QtWidgets.QLabel(res.get("description") or res.get("size", "") or "—")
                desc.setWordWrap(True);
                desc.setObjectName("subtle")
                rating = QtWidgets.QLabel(f"⭐ {res.get('rating', '–')}")
                btn = QtWidgets.QPushButton("🎯 Pobierz");
                style_accent(btn)
                btn.setObjectName("pill")

                gl.addWidget(title, 0, 1, 1, 3)
                gl.addWidget(rating, 0, 4, alignment=Qt.AlignRight)
                gl.addWidget(desc, 1, 1, 1, 4)
                gl.addWidget(btn, 2, 4, alignment=Qt.AlignRight)

                item.setSizeHint(QtCore.QSize(0, 140))
                self.list.addItem(item)
                self.list.setItemWidget(item, wCard)

                # 3) podłącz okładkę (kilka możliwych kluczy)
                img_url = res.get("image") or res.get("poster") or res.get("thumb")
                poster.attach(thumb, _abs_img(img_url), radius=12)

                # 4) akcja pobierania
                url_or_magnet = res.get("url") or res.get("magnet")
                btn.clicked.connect(lambda _, x=url_or_magnet: self._download_from_result(x))
        except Exception as e:
            self.status.setText(f"❌ Błąd: {e}")

    def _download_from_result(self, s: str | None):
        if not s: return
        try:
            if s.startswith("magnet:"):
                requests.post(f"{BACKEND_URL}", data={"magnet": s, "source": self._endpoint()}, timeout=12)
                QtWidgets.QMessageBox.information(self, "OK", "✅ Torrent dodany")
            else:
                # wybór jakości → /yts → / (magnet)
                quality, ok = QtWidgets.QInputDialog.getItem(self, "Wybierz jakość", "Jakość:",
                                                             ["2160p","1080p","720p"], 1, False)
                if not ok: return
                y = requests.post(f"{BACKEND_URL}yts", data={"yts_url": s, "quality": quality}, timeout=20).json()
                magnet = y.get("magnet")
                if not magnet:
                    _error("❌ Brak magnet linku dla tego wyniku.", self); return
                requests.post(f"{BACKEND_URL}", data={"magnet": magnet, "source": self._endpoint()}, timeout=12)
                QtWidgets.QMessageBox.information(self, "OK", "✅ Torrent dodany")
        except Exception as e:
            _error(f"Nie udało się dodać: {e}", self)

# ──────────────────────────────── Strona: Przeglądaj ────────────────────────
class BrowsePage(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(12,12,12,12); v.setSpacing(10)

        gridCard = _card_container()
        grid = QtWidgets.QGridLayout(gridCard); grid.setContentsMargins(12,12,12,12); grid.setHorizontalSpacing(10); grid.setVerticalSpacing(8)
        self.quality = QtWidgets.QComboBox(); self.quality.addItems(["All","480p","720p","1080p","1080p.x265","2160p","3D"])
        self.genre = QtWidgets.QComboBox(); self.genre.addItems([
            "All","Action","Adventure","Animation","Biography","Comedy","Crime","Documentary","Drama","Family","Fantasy",
            "Film-Noir","Game-Show","History","Horror","Music","Musical","Mystery","News","Reality-TV","Romance","Sci-Fi",
            "Sport","Talk-Show","Thriller","War","Western"
        ])
        self.rating = QtWidgets.QComboBox(); self.rating.addItems(["All","9","8","7","6","5","4","3","2","1"])
        self.year = QtWidgets.QComboBox(); self.year.addItems([
            "All","2025","2024","2020-now","2010-now","2010-2019","2000-2009","1990-1999",
            "1980-1989","1970-1979","1950-1969","1900-1949"
        ])
        self.order = QtWidgets.QComboBox(); self.order.addItems(
            ["Latest","Oldest","Featured","Seeds","Peers","Year","IMDb Rating","YTS Likes","RT Audience","Alphabetical","Downloads"]
        )
        grid.addWidget(QtWidgets.QLabel("🎥 Jakość:"), 0,0); grid.addWidget(self.quality,0,1)
        grid.addWidget(QtWidgets.QLabel("🎭 Gatunek:"), 0,2); grid.addWidget(self.genre,0,3)
        grid.addWidget(QtWidgets.QLabel("⭐ IMDb:"),   1,0); grid.addWidget(self.rating,1,1)
        grid.addWidget(QtWidgets.QLabel("📅 Rok:"),    1,2); grid.addWidget(self.year,1,3)
        grid.addWidget(QtWidgets.QLabel("🔃 Sort:"),   2,0); grid.addWidget(self.order,2,1)
        v.addWidget(gridCard)

        btnCard = _card_container()
        btnLay = QtWidgets.QHBoxLayout(btnCard); btnLay.setContentsMargins(12,10,12,10)
        self.btn = QtWidgets.QPushButton("📂 Przeglądaj"); self.btn.setObjectName("pill")
        style_accent(self.btn)
        btnLay.addStretch(1); btnLay.addWidget(self.btn); btnLay.addStretch(1)
        v.addWidget(btnCard)

        listCard = _card_container(); lLay = QtWidgets.QVBoxLayout(listCard); lLay.setContentsMargins(10,10,10,10)
        self.status = QtWidgets.QLabel(""); lLay.addWidget(self.status)
        self.list = QtWidgets.QListWidget(); self.list.setSpacing(8); lLay.addWidget(self.list, 1)
        v.addWidget(listCard, 1)

        nav = QtWidgets.QHBoxLayout()
        self.prev = QtWidgets.QPushButton("⬅️"); self.prev.setObjectName("pill")
        self.next = QtWidgets.QPushButton("➡️"); self.next.setObjectName("pill")
        style_accent(self.prev)
        style_accent(self.next)
        nav.addStretch(1); nav.addWidget(self.prev); nav.addWidget(QtWidgets.QLabel(f"Strona")); nav.addWidget(self.next); nav.addStretch(1)
        v.addLayout(nav)

        self.btn.clicked.connect(lambda: self._browse(reset=True))
        self.prev.clicked.connect(lambda: self._browse(page_delta=-1))
        self.next.clicked.connect(lambda: self._browse(page_delta=+1))

    def _parse_year(self, val: str) -> str:
        if not val or val == "All": return "0"
        if re.fullmatch(r"\d{4}", val): return val
        return {"2020-now":"2020", "2010-now":"2010"}.get(val, "0")

    def _browse(self, reset=False, page_delta=0):
        if reset: self.page = 1
        self.page = max(1, getattr(self, "page", 1) + page_delta)
        self.list.clear();
        self.status.setText("🔄 Ładowanie…")
        params = {
            "quality": self.quality.currentText() if self.quality.currentText() != "All" else "0",
            "genre": self.genre.currentText() if self.genre.currentText() != "All" else "0",
            "rating": self.rating.currentText() if self.rating.currentText() != "All" else "0",
            "year": self._parse_year(self.year.currentText()),
            "order": {"IMDb Rating": "rating", "YTS Likes": "likes", "RT Audience": "rt_audience"}.get(
                self.order.currentText(), self.order.currentText().lower()),
            "page": str(self.page),
            "language": "0", "sort_by": "0"
        }
        try:
            r = requests.get(f"{BACKEND_URL}browse", params=params, timeout=20)
            j = r.json()
            self.status.setText("")
            results = j.get("results", [])
            if not results:
                self.status.setText("❌ Brak wyników.");
                return

            for it in results:
                item = QtWidgets.QListWidgetItem()
                wCard = _card_container()
                gl = QtWidgets.QGridLayout(wCard)
                gl.setContentsMargins(12, 10, 12, 10)
                gl.setHorizontalSpacing(10)
                gl.setVerticalSpacing(6)

                # 1) mini-poster
                thumb = poster.create_label(68, 102, radius=10)
                gl.addWidget(thumb, 0, 0, 3, 1)

                # 2) treść
                title = QtWidgets.QLabel(f"<b>{it.get('title', '')}</b>")
                rating = QtWidgets.QLabel(f"⭐ {it.get('rating', '–')}")
                desc = QtWidgets.QLabel(it.get("description") or "Brak opisu");
                desc.setWordWrap(True);
                desc.setObjectName("subtle")
                btn = QtWidgets.QPushButton("🎯 Pobierz");
                style_accent(btn)
                btn.setObjectName("pill")

                gl.addWidget(title, 0, 1, 1, 3)
                gl.addWidget(rating, 0, 4, alignment=Qt.AlignRight)
                gl.addWidget(desc, 1, 1, 1, 4)
                gl.addWidget(btn, 2, 4, alignment=Qt.AlignRight)

                item.setSizeHint(QtCore.QSize(0, 140))
                self.list.addItem(item)
                self.list.setItemWidget(item, wCard)

                # 3) okładka
                img_url = it.get("image") or it.get("poster") or it.get("thumb")
                poster.attach(thumb, _abs_img(img_url), radius=12)

                # 4) akcja
                url = it.get("url")
                btn.clicked.connect(lambda _, x=url: self._download_yts(x))

            self.status.setText(f"Strona {self.page}")
        except Exception as e:
            self.status.setText(f"❌ Błąd: {e}")

    def _download_yts(self, url: str | None):
        if not url: return
        try:
            quality, ok = QtWidgets.QInputDialog.getItem(self, "Wybierz jakość", "Jakość:",
                                                         ["2160p","1080p","720p"], 1, False)
            if not ok: return
            y = requests.post(f"{BACKEND_URL}yts", data={"yts_url": url, "quality": quality}, timeout=20).json()
            magnet = y.get("magnet")
            if not magnet:
                _error("❌ Brak magnet linku.", self); return
            requests.post(f"{BACKEND_URL}", data={"magnet": magnet, "source": "browse"}, timeout=12)
            QtWidgets.QMessageBox.information(self, "OK", "✅ Torrent dodany")
        except Exception as e:
            _error(f"Nie udało się dodać: {e}", self)

# ──────────────────────────────── Strona: Dostępne ──────────────────────────
# ====== Background fetcher (Qt worker) ======
class _AvailDataWorker(QtCore.QObject):
    films_ready  = QtCore.Signal(list)
    series_ready = QtCore.Signal(list)
    error        = QtCore.Signal(str)

    @QtCore.Slot()
    def load_films(self):
        try:
            r = requests.get(f"{BACKEND_URL}plex/films", timeout=15)
            r.raise_for_status()
            self.films_ready.emit(r.json() or [])
        except Exception as e:
            self.error.emit(f"Błąd filmów: {e}")

    @QtCore.Slot()
    def load_series(self):
        try:
            r = requests.get(f"{BACKEND_URL}plex/series", timeout=20)
            r.raise_for_status()
            self.series_ready.emit(r.json() or [])
        except Exception as e:
            self.error.emit(f"Błąd seriali: {e}")


# ====== Incremental list rendering to avoid UI stalls ======
def _render_incremental(
    list_widget: QtWidgets.QListWidget,
    data_iterable,
    build_item_fn,          # fn(meta) -> (QListWidgetItem, QWidget)
    batch_size: int = 20,   # ile elementów na jedną porcję
    done_cb=None
):
    data = list(data_iterable)
    total = len(data)
    list_widget.clear()
    list_widget.setUpdatesEnabled(False)
    idx = {"i": 0}

    def _add_batch():
        start = idx["i"]
        end = min(start + batch_size, total)
        for i in range(start, end):
            item, widget = build_item_fn(data[i])
            list_widget.addItem(item)
            list_widget.setItemWidget(item, widget)
        idx["i"] = end

        if idx["i"] < total:
            QtCore.QTimer.singleShot(0, _add_batch)
        else:
            list_widget.setUpdatesEnabled(True)
            if callable(done_cb):
                done_cb()

    QtCore.QTimer.singleShot(0, _add_batch)


class AvailablePage(QtWidgets.QWidget):
    """
    Zakładka 'Dostępne' z:
      • Filmy: kapsel czasu tylko przy progress==100%, Reset i Usuń.
      • Seriale: nagłówek jak w web + ROZWIJANIE sezonów → odcinki z mini '🗑️'.
      • Reset timera: POST /plex/reset-delete-timer (7 dni od teraz)
      • Usuwanie: DELETE /plex/delete (film: force=False; serial/odcinek: force=True)
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(12,12,12,12); v.setSpacing(10)

        tabsCard = _card_container()
        tLay = QtWidgets.QVBoxLayout(tabsCard); tLay.setContentsMargins(10,10,10,10)
        self.tabs = QtWidgets.QTabWidget()
        self.films = QtWidgets.QListWidget();  self.films.setSpacing(8)
        self.series = QtWidgets.QListWidget(); self.series.setSpacing(8)
        self.tabs.addTab(self.films, "🎬 Filmy")
        self.tabs.addTab(self.series, "📺 Seriale")
        tLay.addWidget(self.tabs)
        v.addWidget(tabsCard, 1)

        infoCard = _card_container()
        iLay = QtWidgets.QVBoxLayout(infoCard); iLay.setContentsMargins(12,10,12,10)
        self.loading = QtWidgets.QLabel("")
        iLay.addWidget(self.loading)
        v.addWidget(infoCard)
        # >>> ADD: pasek akcji (Filtruj / Losuj)
        actions = QtWidgets.QHBoxLayout(); actions.setContentsMargins(0,0,0,0)
        self.btnFilter = QtWidgets.QPushButton("🔎 Filtruj"); style_accent(self.btnFilter); self.btnFilter.setObjectName("pill")
        self.btnRandom = QtWidgets.QPushButton("🎲 Losuj"); style_accent(self.btnRandom); self.btnRandom.setObjectName("pill")
        actions.addStretch(1); actions.addWidget(self.btnFilter); actions.addWidget(self.btnRandom)
        iLay.addLayout(actions)

        # stan filtrów
        self._filter_text = ""
        self._filter_genres = set()

        self.btnFilter.clicked.connect(self._open_filter)
        self.btnRandom.clicked.connect(self._open_randomizer)


        self.tabs.currentChanged.connect(self.refresh)
        QtCore.QTimer.singleShot(400, lambda: self.refresh(0))

        # ticker do odliczania
        self._count_timer = QtCore.QTimer(self); self._count_timer.setInterval(1000)
        self._count_timer.timeout.connect(self._tick_countdowns)
        self._count_timer.start()

    # ───────────────────────────── helpers: czas ──────────────────────────────
    def _tick_countdowns(self):
        for lst in (self.films, self.series):
            for i in range(lst.count()):
                w = lst.itemWidget(lst.item(i))
                if not w: continue
                for lbl in w.findChildren(QtWidgets.QLabel, "deadline"):
                    ts = lbl.property("target_ts")
                    if ts:
                        lbl.setText(self._countdown_text(int(ts)))

    def _countdown_text(self, target_ms: int) -> str:
        diff = target_ms - int(time.time()*1000)
        if diff <= 0: return "✅ Do usunięcia"
        s = diff//1000; d = s//86400; h = (s%86400)//3600; m = (s%3600)//60
        if d>0: return f"⏳ {d}d {h}h"
        if h>0: return f"⏳ {h}h {m}m"
        return f"⏳ {m}m"

    def _set_deadline_label(self, label: QtWidgets.QLabel, new_ts: int | None):
        if new_ts:
            label.setProperty("target_ts", int(new_ts))
            label.setText(self._countdown_text(int(new_ts)))
        else:
            label.setProperty("target_ts", None)
            label.setText("–")

    # ───────────────────────────── helpers: API ───────────────────────────────
    def _reset_timer(self, obj_id: str, label: QtWidgets.QLabel):
        if not _confirm("Zresetować czas usunięcia (7 dni od teraz)?", self): return
        try:
            r = requests.post(f"{BACKEND_URL}plex/reset-delete-timer",
                              json={"id": obj_id}, timeout=10)
            j = r.json() if r.ok else {}
            if r.ok and j.get("success") and j.get("newDeleteAt"):
                self._set_deadline_label(label, int(j["newDeleteAt"]))
                QtWidgets.QMessageBox.information(self, "OK", "✅ Zresetowano timer")
            else:
                _error("❌ Błąd resetowania timera.", self)
        except Exception as e:
            _error(f"❌ Błąd połączenia: {e}", self)

    def _delete_item(self, obj_id: str, type_: str, title: str | None = None):
        pretty = title or "ten element"
        if not _confirm(f"Czy na pewno chcesz usunąć: {pretty}?", self): return
        try:
            r = requests.delete(f"{BACKEND_URL}plex/delete",
                                json={"id": obj_id, "force": (type_ != "film")},
                                timeout=15)
            j = r.json() if r.ok else {}
            if r.ok and j.get("success"):
                QtWidgets.QMessageBox.information(self, "OK", "🗑️ Usunięto")
                self.refresh(self.tabs.currentIndex())
            else:
                _error("❌ Błąd usuwania.", self)
        except Exception as e:
            _error(f"❌ Błąd połączenia: {e}", self)

    def _open_filter(self):
        dlg = FilterDialog(getattr(self, "_films_cache", []), self._filter_genres, self._filter_text, self)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self._filter_genres = dlg.selected_genres()
            self._filter_text   = dlg.text_query()
            self._apply_filters()

    def _open_randomizer(self):
        snap = getattr(self, "_films_cache", [])
        if not snap:
            QtWidgets.QMessageBox.information(self, "Losowanie", "Brak załadowanych filmów.")
            return
        dlg = RandomizerDialog(snap, self)
        dlg.exec()

    def _apply_filters(self):
        """Filtr po tytule i gatunkach; działa na liście filmów."""
        t = (self._filter_text or "").lower()
        gset = set(self._filter_genres or [])
        for i in range(self.films.count()):
            it = self.films.item(i)
            w  = self.films.itemWidget(it)
            if not w:
                continue
            title = (w.property("p_title") or "").lower()
            gtags = set(w.property("p_genres") or [])
            ok_text = (t in title) if t else True
            ok_gen  = True if not gset else bool(gtags & gset)
            it.setHidden(not (ok_text and ok_gen))


    # ───────────────────────────── odświeżanie ────────────────────────────────
    def refresh(self, idx: int):
        # anti-reentrancy + anulowanie poprzedniego wątku
        if getattr(self, "_busy", False):
            return
        self._busy = True
        self.loading.setText("⏳ Ładowanie…")

        # sprzątanie starego wątku
        try:
            if getattr(self, "_wk_thread", None):
                self._wk_thread.quit()
                self._wk_thread.wait(50)
        except Exception:
            pass

        self._wk_thread = QtCore.QThread(self)
        self._wk = _AvailDataWorker()
        self._wk.moveToThread(self._wk_thread)

        self._wk.error.connect(lambda msg: (self.loading.setText(f"❌ {msg}"), self._finish_refresh()))
        if idx == 0:
            self._wk.films_ready.connect(self._on_films_ready)
            self._wk_thread.started.connect(self._wk.load_films)
        else:
            self._wk.series_ready.connect(self._on_series_ready)
            self._wk_thread.started.connect(self._wk.load_series)

        self._wk_thread.start()

    def _finish_refresh(self):
        self._busy = False
        try:
            if getattr(self, "_wk_thread", None):
                self._wk_thread.quit()
                self._wk_thread.wait(10)
        except Exception:
            pass

    # ───────────────────────────────── FILMY ──────────────────────────────────
    def _on_films_ready(self, data: list):
        self.loading.setText("")
        # snapshot do filtra/losowania (raz!)
        self._films_cache = list(data)

        def _build_item(f: dict):
            # filtr asekuracyjny, by seriale nie trafiały do filmów
            if str(f.get("type", "")).lower() not in ("", "film", "movie"):
                # w razie gdyby trafił tu serial, zwróć pusty widget, ale nie dodawaj go do listy
                dummy = QtWidgets.QListWidgetItem()
                dummy.setSizeHint(QtCore.QSize(0, 0))
                return dummy, QtWidgets.QWidget()

            progress = int(float(f.get("progress", 0)))
            del_ts = None
            if progress >= 100:
                del_ts = f.get("deleteAt") or (
                    (f.get("watchedAt") or 0) + 7 * 24 * 3600 * 1000 if f.get("watchedAt") else None
                )

            item = QtWidgets.QListWidgetItem()
            wCard = _card_container()
            gl = QtWidgets.QGridLayout(wCard)
            gl.setContentsMargins(12, 10, 12, 10)
            gl.setHorizontalSpacing(10)
            gl.setVerticalSpacing(6)

            thumb = poster.create_label(68, 102, radius=10)
            gl.addWidget(thumb, 0, 0, 4, 1)

            title_txt = f.get('title', '')
            title = QtWidgets.QLabel(f"<b>{title_txt}</b>")
            bar = _progress_bar(float(progress))
            small = QtWidgets.QLabel(f"{progress}% obejrzane");
            small.setObjectName("subtle")

            # GENRES container (pod progress barem)
            genresWrap = QtWidgets.QWidget()
            flow = FlowLayout(genresWrap, spacing=6)

            # deadline + przyciski
            deadline = QtWidgets.QLabel("–");
            deadline.setObjectName("deadline")
            self._set_deadline_label(deadline, del_ts)

            btnCast = QtWidgets.QPushButton("📺 CAST");
            style_accent(btnCast)
            btnReset = QtWidgets.QPushButton("🔁 Resetuj");
            style_success(btnReset);
            btnReset.setEnabled(bool(del_ts))
            btnRemove = QtWidgets.QPushButton("🗑️ Usuń teraz");
            style_danger(btnRemove)

            gl.addWidget(title, 0, 1, 1, 3)
            gl.addWidget(deadline, 0, 4, alignment=Qt.AlignRight)
            gl.addWidget(bar, 1, 1, 1, 4)
            gl.addWidget(genresWrap, 2, 1, 1, 4)  # << badge'y tu
            gl.addWidget(small, 3, 1, 1, 2)
            gl.addWidget(btnCast, 3, 3)
            gl.addWidget(btnReset, 3, 4)
            gl.addWidget(btnRemove, 3, 5)
            for c in range(6): gl.setColumnStretch(c, 0)
            gl.setColumnStretch(2, 1)

            # okładka „po ticku”, żeby nie blokować
            def _attach_poster():
                local = _find_local_poster(f)
                if local:
                    poster.attach(thumb, local, radius=10)
                else:
                    poster.attach(thumb, _abs_img(f.get("thumb") or f.get("poster") or f.get("image")), radius=10)

            QtCore.QTimer.singleShot(0, _attach_poster)

            # badge'y + właściwości do filtra
            mid = str(f.get("id") or "")
            genres = _fetch_genres_for_id(mid)
            for g in genres:
                flow.addWidget(_make_badge(g))
            wCard.setProperty("p_genres", genres)
            wCard.setProperty("p_title", title_txt)

            # akcje
            btnCast.clicked.connect(lambda _, x=mid, t=title_txt: _cast_start(x, t, self))
            btnReset.clicked.connect(lambda _, x=mid, lbl=deadline: self._reset_timer(x, lbl))
            btnRemove.clicked.connect(lambda _, x=mid, t='film', ttitle=title_txt: self._delete_item(x, t, ttitle))

            item.setSizeHint(QtCore.QSize(0, 140))
            return item, wCard

        # render porcjami
        _render_incremental(self.films, data, _build_item, batch_size=20, done_cb=self._finish_refresh)
        # zastosuj aktywne filtry po dosypaniu pierwszej porcji
        QtCore.QTimer.singleShot(0, self._apply_filters)

    # ─────────────────────────────── SERIALE ──────────────────────────────────
    def _on_series_ready(self, data: list):
        self.loading.setText("")

        def _build_item(s: dict):
            series_id = str(s.get("id") or "")
            title_txt = s.get("title", "")
            progress = int(s.get("progress", 0) or 0)
            del_ts = s.get("deleteAt")

            seasons_map: dict[int, list] = {}
            for ep in (s.get("episodes") or []):
                seasons_map.setdefault(int(ep.get("season") or 0), []).append(ep)

            item = QtWidgets.QListWidgetItem()
            card = _card_container()
            root = QtWidgets.QVBoxLayout(card);
            root.setContentsMargins(12, 10, 12, 10);
            root.setSpacing(8)

            head = QtWidgets.QWidget();
            hl = QtWidgets.QGridLayout(head)
            hl.setContentsMargins(0, 0, 0, 0);
            hl.setHorizontalSpacing(10);
            hl.setVerticalSpacing(6)
            thumb = poster.create_label(68, 102, radius=10)
            hl.addWidget(thumb, 0, 0, 3, 1)

            title = QtWidgets.QLabel(f"<b>{title_txt}</b>")
            bar = _progress_bar(float(progress))
            small = QtWidgets.QLabel(f"{progress}% obejrzane");
            small.setObjectName("subtle")

            deadline = QtWidgets.QLabel("–");
            deadline.setObjectName("deadline")
            self._set_deadline_label(deadline, del_ts)

            btnCastS = QtWidgets.QPushButton("📺 CAST");
            style_accent(btnCastS)
            btnReset = QtWidgets.QPushButton("🔁 Resetuj");
            style_success(btnReset);
            btnReset.setEnabled(bool(del_ts))
            btnRemove = QtWidgets.QPushButton("🗑️ Usuń");
            style_danger(btnRemove)

            hl.addWidget(title, 0, 1, 1, 3)
            hl.addWidget(deadline, 0, 4, alignment=Qt.AlignRight)
            hl.addWidget(bar, 1, 1, 1, 4)
            hl.addWidget(small, 2, 1, 1, 2)
            hl.addWidget(btnCastS, 2, 2)
            hl.addWidget(btnReset, 2, 3)
            hl.addWidget(btnRemove, 2, 4)

            root.addWidget(head)

            body = QtWidgets.QWidget();
            body.setVisible(False)
            bl = QtWidgets.QVBoxLayout(body);
            bl.setContentsMargins(4, 4, 4, 4);
            bl.setSpacing(6)

            for snum in sorted(seasons_map.keys()):
                eps = sorted(seasons_map[snum], key=lambda e: int(e.get("episode") or 0))
                done = sum(1 for e in eps if int(e.get("progress") or 0) >= 100)
                total = len(eps)

                seasonBox = QtWidgets.QGroupBox(f"📁 Sezon {snum} — {done}/{total}")
                seasonBox.setFlat(True)
                vb = QtWidgets.QVBoxLayout(seasonBox);
                vb.setContentsMargins(8, 6, 8, 6);
                vb.setSpacing(4)

                for e in eps:
                    row = QtWidgets.QWidget()
                    rl = QtWidgets.QHBoxLayout(row);
                    rl.setContentsMargins(0, 0, 0, 0);
                    rl.setSpacing(6)
                    ep_num = int(e.get("episode") or 0)
                    ep_title = e.get("title") or ""
                    ep_prog = int(e.get("progress") or 0)
                    lbl = QtWidgets.QLabel(f"{ep_num}. {ep_title} — <b>{ep_prog}%</b>")
                    btnDel = QtWidgets.QPushButton("🗑️");
                    btnDel.setFixedHeight(26);
                    style_danger(btnDel)
                    rl.addWidget(lbl);
                    rl.addStretch(1);
                    rl.addWidget(btnDel)
                    vb.addWidget(row)

                    ep_id = str(e.get("id") or "")
                    btnDel.clicked.connect(
                        lambda _, x=ep_id, t="episode", ttitle=ep_title: self._delete_item(x, t, ttitle))

                bl.addWidget(seasonBox)

            caretRow = QtWidgets.QHBoxLayout();
            caretRow.setContentsMargins(0, 0, 0, 0)
            btnToggle = QtWidgets.QToolButton();
            btnToggle.setText("›");
            btnToggle.setToolTip("Pokaż sezony")
            btnToggle.setCheckable(True);
            btnToggle.setChecked(False);
            btnToggle.setFixedSize(28, 28)

            def _on_toggle(ch: bool):
                body.setVisible(ch)
                btnToggle.setText("‹" if ch else "›")
                btnToggle.setToolTip("Ukryj sezony" if ch else "Pokaż sezony")

            btnToggle.toggled.connect(_on_toggle)

            caretRow.addStretch(1);
            caretRow.addWidget(btnToggle)
            root.addLayout(caretRow)
            root.addWidget(body)

            # okładka po ticku
            def _attach_poster():
                local = _find_local_poster(s)
                if local:
                    poster.attach(thumb, local, radius=10)
                else:
                    poster.attach(thumb, _abs_img(s.get("thumb") or s.get("poster") or s.get("image")), radius=10)

            QtCore.QTimer.singleShot(0, _attach_poster)

            btnCastS.clicked.connect(lambda _, x=series_id, t=title_txt: _cast_start(x, t, self))
            btnReset.clicked.connect(lambda _, x=series_id, lbl=deadline: self._reset_timer(x, lbl))
            btnRemove.clicked.connect(
                lambda _, x=series_id, t='series', ttitle=title_txt: self._delete_item(x, t, ttitle))

            item.setSizeHint(QtCore.QSize(0, 160))
            return item, card

        _render_incremental(self.series, data, _build_item, batch_size=15, done_cb=self._finish_refresh)


# ─────────────────────────────── Główne okno (NATYWNE) ──────────────────────
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        if os.path.isfile(APP_ICON_PATH):
            self.setWindowIcon(QtGui.QIcon(APP_ICON_PATH))
        self.resize(1280, 800)

        # centralny TabWidget z sekcjami
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        self.pageT = TorrentsPage()
        self.pageS = SearchPage()
        self.pageB = BrowsePage()
        self.pageA = AvailablePage()
        self.tabs.addTab(self.pageT, "🎞️ Torrenty")
        self.tabs.addTab(self.pageS, "🔍 Szukaj")
        self.tabs.addTab(self.pageB, "📂 Przeglądaj")
        self.tabs.addTab(self.pageA, "✅ Dostępne")
        self.setCentralWidget(self.tabs)

        # MENUBAR
        mb = self.menuBar()
        m_file = mb.addMenu("Plik")
        act_update = QtGui.QAction("Aktualizuj teraz", self); act_update.triggered.connect(_update_now); m_file.addAction(act_update)
        act_reset  = QtGui.QAction("Zresetuj aplikację", self); act_reset.triggered.connect(reset_app_full); m_file.addAction(act_reset)
        act_exit   = QtGui.QAction("Wyjście", self); act_exit.triggered.connect(quit_app_graceful); m_file.addAction(act_exit)

        m_settings = mb.addMenu("Ustawienia")
        act_settings = QtGui.QAction("Ustawienia…", self); act_settings.triggered.connect(self._open_settings); m_settings.addAction(act_settings)

        m_about = mb.addMenu("O mnie")
        act_me = QtGui.QAction("O mnie", self); act_me.triggered.connect(lambda: webbrowser.open("http://pkportfolio.pl/")); m_about.addAction(act_me)

        # Statusbar + aktualizacje
        self.status = PrettyStatusBar(self, logo_path=APP_LOGO_PATH);
        self.setStatusBar(self.status)
        self.status.set_center_text(f"© Piotrflix • v{APP_VERSION} • aktualność: sprawdzam…")
        self._init_updates()

        # Tray
        self._min_to_tray = True
        if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QtWidgets.QSystemTrayIcon(QtGui.QIcon(APP_ICON_PATH), self)
            menu = QtWidgets.QMenu()
            menu.addAction("Pokaż", self._show_from_tray)
            menu.addSeparator()
            menu.addAction("Aktualizuj teraz", _update_now)
            menu.addAction("Zresetuj aplikację", reset_app_full)
            menu.addAction("Zamknij", quit_app_graceful)
            self.tray.setContextMenu(menu); self.tray.show()
        else:
            self.tray = None

    def _open_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self._min_to_tray = dlg.chk_min_to_tray.isChecked()

    def _show_from_tray(self):
        self.show(); self.raise_(); self.activateWindow()

    def closeEvent(self, e: QtGui.QCloseEvent):
        if self.tray and self._min_to_tray:
            e.ignore(); self.hide()
            self.tray.showMessage(APP_NAME, "Zminimalizowano do zasobnika.",
                                  QtWidgets.QSystemTrayIcon.Information, 2000)
        else:
            e.ignore()
            quit_app_graceful()

    def _init_updates(self):
        self._update_version_target = None
        self._act_update_ref = None
        for m in self.menuBar().findChildren(QtWidgets.QMenu):
            for a in m.actions():
                if a.text().startswith("Aktualizuj"):
                    self._act_update_ref = a; break

        self._upd_thread = QtCore.QThread(self)
        self._upd_worker = UpdateChecker(GITHUB_OWNER, GITHUB_REPO, APP_VERSION)
        self._upd_worker.moveToThread(self._upd_thread)

        self._upd_thread.started.connect(self._upd_worker.check_once)
        self._upd_worker.info.connect(lambda msg: self.status.set_center_text(
            f"© Piotrflix • v{APP_VERSION} • {msg}"
        ))
        self._upd_worker.none.connect(self._on_update_none)
        self._upd_worker.available.connect(self._on_update_available)
        self._upd_worker.error.connect(self._on_update_error)

        self._upd_worker.none.connect(self._upd_thread.quit)
        self._upd_worker.available.connect(self._upd_thread.quit)
        self._upd_worker.error.connect(self._upd_thread.quit)
        self._upd_thread.finished.connect(self._upd_worker.deleteLater)
        self._upd_thread.finished.connect(self._upd_thread.deleteLater)
        QtCore.QTimer.singleShot(1200, self._upd_thread.start)

    def _on_update_none(self):
        self.status.set_center_text(f"© Piotrflix • v{APP_VERSION} • aktualność: aktualna")

    def _on_update_error(self, msg: str):
        self.status.set_center_text(f"© Piotrflix • v{APP_VERSION} • aktualność: offline")

    def _on_update_available(self, info: dict):
        ver = info.get("version", "").strip() or "nowa wersja"
        self._update_version_target = ver
        self.status.set_center_text(f"© Piotrflix • v{APP_VERSION} • dostępna aktualizacja: {ver}")
        if self.tray:
            self.tray.showMessage(APP_NAME, f"Dostępna aktualizacja: {ver}",
                                  QtWidgets.QSystemTrayIcon.Information, 4000)
        if self._act_update_ref:
            self._act_update_ref.setText(f"Aktualizuj do {ver}")
        try:
            ret = QtWidgets.QMessageBox.question(
                self, "Aktualizacja dostępna",
                f"Wykryto nową wersję: {ver}\nCzy chcesz zainstalować teraz?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.Yes
            )
            if ret == QtWidgets.QMessageBox.Yes:
                _update_now()
        except Exception:
            pass

# ────────────────────────────── Helpers GUI (API) ───────────────────────────
_QAPP = None
_SPLASH = None
_MAIN_WIN = None

def _ensure_qapp():
    global _QAPP
    app = QtWidgets.QApplication.instance()
    if app is None:
        try:
            QtCore.QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
        except Exception:
            pass
        app = QtWidgets.QApplication(sys.argv)
        app.setApplicationName(APP_NAME)
        app.setOrganizationName(APP_NAME)
        app.setQuitOnLastWindowClosed(False)
        if os.path.isfile(APP_ICON_PATH):
            app.setWindowIcon(QtGui.QIcon(APP_ICON_PATH))
        # >>> nowy motyw globalnie <<<
        apply_theme(app, theme="dark")  # albo "light"
    if not getattr(app, "_pflx_about_connected", False):
        app.aboutToQuit.connect(lambda: _flush_backend_quietly())
        app._pflx_about_connected = True  # type: ignore[attr-defined]
    _QAPP = app
    return _QAPP

def show_splash():
    global _SPLASH
    app = _ensure_qapp()
    _SPLASH = LoadingSplash(app_name=APP_NAME, icon_path=APP_ICON_PATH);
    _SPLASH.show(); app.processEvents()
    return _SPLASH


def show_main_window():
    global _MAIN_WIN
    _MAIN_WIN = MainWindow()
    _MAIN_WIN.show(); _MAIN_WIN.raise_(); _MAIN_WIN.activateWindow()
    return _MAIN_WIN

# ────────────────────────────── Backend import & fallbacks ───────────────────
def import_backend_safe():
    os.environ["PFLIX_DEFER_INIT"] = "1"
    try:
        mod = importlib.import_module("app")
        if hasattr(mod, "start_flask_blocking") and hasattr(mod, "init_backend_after_splash"):
            print("✅ Załadowano backend jako moduł 'app':", getattr(mod, "__file__", None))
            return mod
        print("ℹ️ 'app' bez pełnego API – spróbuję zbudować fallbacki…")
        _wire_fallbacks(mod); return mod
    except Exception as e:
        print("⚠️ Import 'app' nieudany:", e)

    backend_path = pathlib.Path(__file__).with_name("app.py")
    if not backend_path.exists():
        raise RuntimeError(f"Nie znaleziono backendu: {backend_path}")
    spec = importlib.util.spec_from_file_location("pflx_backend", str(backend_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pflx_backend"] = mod
    spec.loader.exec_module(mod)  # type: ignore
    print("✅ Załadowano backend z pliku:", backend_path)
    _wire_fallbacks(mod); return mod

def _wire_fallbacks(mod):
    if not hasattr(mod, "start_flask_blocking"):
        if hasattr(mod, "app") and hasattr(mod.app, "run"):
            def _start_flask_blocking():
                mod.app.run(host=BIND_HOST, port=BIND_PORT, debug=False,
                            use_reloader=False, threaded=True)
            mod.start_flask_blocking = _start_flask_blocking
        else:
            raise RuntimeError("Backend nie ma obiektu Flask 'app'")
    if not hasattr(mod, "init_backend_after_splash"):
        def _init_backend_after_splash():
            if hasattr(mod, "_do_available_bootstrap"): mod._do_available_bootstrap()
            if hasattr(mod, "_start_all_backgrounds"): mod._start_all_backgrounds()
        mod.init_backend_after_splash = _init_backend_after_splash

def _norm_ver(v: str) -> tuple:
    nums = [int(x) for x in re.findall(r"\d+", v or "")][:4]
    while len(nums) < 4: nums.append(0)
    return tuple(nums)

class UpdateChecker(QtCore.QObject):
    info = QtCore.Signal(str)
    available = QtCore.Signal(dict)
    none = QtCore.Signal()
    error = QtCore.Signal(str)

    def __init__(self, owner: str, repo: str, cur_ver: str):
        super().__init__(); self.owner=owner; self.repo=repo; self.cur_ver=cur_ver

    @QtCore.Slot()
    def check_once(self):
        try:
            self.info.emit("Sprawdzam aktualizacje…")
            if not self.owner or not self.repo or "YOUR_GH_" in self.owner+self.repo:
                self.error.emit("Repozytorium nie skonfigurowane"); return
            url = f"https://api.github.com/repos/{self.owner}/{self.repo}/releases"
            r = requests.get(url, timeout=6); r.raise_for_status()
            releases = r.json() or []
            rel = next((x for x in releases if ALLOW_PRERELEASES or not x.get("prerelease")), None)
            if not rel: self.none.emit(); return
            tag = rel.get("tag_name") or rel.get("name") or ""
            if _norm_ver(tag) > _norm_ver(self.cur_ver):
                self.available.emit({"version": tag, "url": rel.get("html_url") or "", "body": rel.get("body") or ""})
            else:
                self.none.emit()
        except Exception as e:
            self.error.emit(str(e))

# ───────────────────────────── Poller: czekanie na /status ───────────────────
class StatusWaiter(QtCore.QObject):
    ready = QtCore.Signal(str)
    timeout = QtCore.Signal()
    info = QtCore.Signal(str)
    def __init__(self, base_urls: list[str], timeout_sec: int = 40, interval: float = 0.35):
        super().__init__(); self.base_urls=base_urls; self.timeout_sec=timeout_sec; self.interval=interval; self._th=None
    def start(self):
        def _run():
            t0 = time.time()
            while time.time() - t0 < self.timeout_sec:
                for base in self.base_urls:
                    try:
                        self.info.emit(f"Łączenie z serwerem… ({base})")
                        r = requests.get(base.rstrip("/") + "/status", timeout=1.5)
                        if r.status_code == 200:
                            self.ready.emit(base); return
                    except Exception:
                        pass
                time.sleep(self.interval)
            self.timeout.emit()
        self._th = threading.Thread(target=_run, daemon=True); self._th.start()

def _lan_ip_guess() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        try: return socket.gethostbyname(socket.gethostname())
        except Exception: return "127.0.0.1"

def _candidate_backend_urls() -> list[str]:
    hosts = ["127.0.0.1"]; lan = _lan_ip_guess()
    if lan and lan != "127.0.0.1": hosts.append(lan)
    seen, urls = set(), []
    for h in hosts:
        if h and h not in seen:
            seen.add(h); urls.append(f"http://{h}:{BIND_PORT}/")
    return urls

# ──────────────────────────────── Runner ─────────────────────────────────────
def _install_signal_handlers():
    def _handler(signum, frame):
        quit_app_graceful()
    try:
        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _handler)
    except Exception:
        pass

def run():
    ensure_version_files()
    if not ensure_configuration():
        sys.exit(1)

    splash = show_splash(); splash.set_status("Ładowanie modułów…")
    global BACKEND_MOD, FLASK_THREAD
    BACKEND_MOD = import_backend_safe()

    splash.set_status("Uruchamianie serwera…")
    FLASK_THREAD = threading.Thread(target=BACKEND_MOD.start_flask_blocking, daemon=True, name="flask")
    FLASK_THREAD.start()

    candidates = _candidate_backend_urls()
    waiter = StatusWaiter(candidates, timeout_sec=40, interval=0.35)
    waiter.info.connect(lambda msg: splash.set_status(msg))
    waiter.ready.connect(lambda base: _open_ui_and_close_splash(splash, base))
    waiter.timeout.connect(lambda: _open_ui_and_close_splash(splash, candidates[0], warn=True))
    waiter.start()

    def _heavy_init():
        try: splash.set_status("Startowanie aplikacji…")
        except Exception: pass
        BACKEND_MOD.init_backend_after_splash()
    threading.Thread(target=_heavy_init, daemon=True, name="backend-init").start()

    _install_signal_handlers()
    app = QtWidgets.QApplication.instance()
    sys.exit(app.exec())


def _open_ui_and_close_splash(splash: LoadingSplash, base_url: str, warn: bool = False):
    def _show():
        global BACKEND_URL
        BACKEND_URL = base_url
        splash.set_status("Gotowe. Uruchamianie interfejsu…" if not warn else "Nie potwierdzono statusu — uruchamiam interfejs…")
        show_main_window(); splash.close()
    QtCore.QTimer.singleShot(0, _show)

if __name__ == "__main__":
    run()

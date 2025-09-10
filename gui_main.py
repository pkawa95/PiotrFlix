# launcher.py
# -*- coding: utf-8 -*-
import os, sys, time, threading, webbrowser, importlib, importlib.util, pathlib
import requests
import signal
import subprocess
import re
import json
import socket
import hashlib, io, pathlib, functools
from concurrent.futures import ThreadPoolExecutor


APP_NAME = "Piotrflix"
APP_VERSION = "1.0.2"
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

# ───────────────────────────── grafika / flagi ───────────────────────────────
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

def _setup_graphics():
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
_setup_graphics()

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
        _SPLASH = LoadingSplash()
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

def reset_app_full():
    try:
        _flush_backend_quietly()
    finally:
        _delete_config_files()
        python = sys.executable
        args = [python] + sys.argv
        env = dict(os.environ)
        os.execve(python, args, env)

# ───────────────────────────────── Splash ───────────────────────────────────
class LoadingSplash(QtWidgets.QWidget):
    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(420, 340)
        container = QtWidgets.QFrame(self)
        container.setObjectName("card")
        container.setFixedSize(420, 340)
        container.setStyleSheet("""
        #card { background-color:#1c1c1c; border-radius:20px; }
        QLabel { color:#eaeaea; }
        """)
        v = QtWidgets.QVBoxLayout(container)
        v.setContentsMargins(24, 24, 24, 24); v.setSpacing(16)
        self.icon_label = QtWidgets.QLabel(); self.icon_label.setFixedSize(128, 128)
        self._set_rounded_icon(APP_ICON_PATH, 20)
        self.icon_label.setAlignment(Qt.AlignCenter)
        self.title = QtWidgets.QLabel(APP_NAME)
        self.title.setStyleSheet("font-size:20px;font-weight:600;"); self.title.setAlignment(Qt.AlignCenter)
        self.status = QtWidgets.QLabel("Inicjalizacja…")
        self.status.setStyleSheet("font-size:14px;"); self.status.setAlignment(Qt.AlignCenter)
        v.addStretch(1); v.addWidget(self.icon_label, 0, Qt.AlignCenter)
        v.addWidget(self.title, 0, Qt.AlignCenter); v.addWidget(self.status, 0, Qt.AlignCenter); v.addStretch(1)
        scr = QtWidgets.QApplication.primaryScreen().availableGeometry()
        self.move(scr.center() - self.rect().center())

    def _set_rounded_icon(self, path: str, radius: int):
        pix = QtGui.QPixmap(path)
        if pix.isNull():
            pix = QtGui.QPixmap(128, 128); pix.fill(Qt.transparent)
        pix = pix.scaled(128, 128, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        mask = QtGui.QBitmap(128, 128); mask.clear()
        painter = QtGui.QPainter(mask); painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setBrush(Qt.black); painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(0, 0, 128, 128, radius, radius); painter.end()
        rounded = QtGui.QPixmap(128, 128); rounded.fill(Qt.transparent)
        p2 = QtGui.QPainter(rounded); p2.setRenderHint(QtGui.QPainter.Antialiasing)
        p2.setClipRegion(QtGui.QRegion(mask)); p2.drawPixmap(0, 0, pix); p2.end()
        self.icon_label.setPixmap(rounded)

    def set_status(self, txt: str):
        self.status.setText(txt)

# ─────────────────────────────── Okno Ustawień ──────────────────────────────
class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ustawienia")
        if os.path.isfile(APP_ICON_PATH):
            self.setWindowIcon(QtGui.QIcon(APP_ICON_PATH))
        lay = QtWidgets.QVBoxLayout(self)
        self.chk_autostart = QtWidgets.QCheckBox("Uruchamiaj przy starcie systemu (Windows)")
        self.chk_min_to_tray = QtWidgets.QCheckBox("Kliknięcie w krzyżyk minimalizuje do traya")
        lay.addWidget(self.chk_autostart); lay.addWidget(self.chk_min_to_tray)
        row = QtWidgets.QHBoxLayout()
        self.btn_reset = QtWidgets.QPushButton("Zresetuj aplikację")
        self.btn_update = QtWidgets.QPushButton("Sprawdź aktualizacje")
        row.addWidget(self.btn_reset); row.addWidget(self.btn_update); lay.addLayout(row)
        row2 = QtWidgets.QHBoxLayout(); row2.addStretch(1)
        ok = QtWidgets.QPushButton("OK"); cancel = QtWidgets.QPushButton("Anuluj")
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

# ─────────────────────────────── ładna stopka ────────────────────────────────
class PrettyStatusBar(QtWidgets.QStatusBar):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(False)
        self.setStyleSheet("""
        QStatusBar{
            background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #161616, stop:1 #0f0f0f);
            border-top: 1px solid #2a2a2a;
        }
        QStatusBar::item{ border: none; }
        QLabel{ color:#eaeaea; }
        """)
        self._container = QtWidgets.QWidget(self)
        h = QtWidgets.QHBoxLayout(self._container)
        h.setContentsMargins(8, 2, 8, 2); h.setSpacing(10)
        self.logo = QtWidgets.QLabel(self._container)
        pix = QtGui.QPixmap(APP_LOGO_PATH)
        if pix.isNull(): pix = QtGui.QPixmap(18, 18); pix.fill(QtGui.QColor("#444"))
        self.logo.setPixmap(pix.scaledToHeight(18, Qt.SmoothTransformation)); self.logo.setFixedHeight(18)
        self.center = QtWidgets.QLabel(self._container); self.center.setAlignment(Qt.AlignCenter)
        self.center.setStyleSheet("font-size:12px; letter-spacing:0.2px;")
        h.addWidget(self.logo, 0, Qt.AlignLeft | Qt.AlignVCenter); h.addStretch(1)
        h.addWidget(self.center, 0, Qt.AlignCenter); h.addStretch(1)
        self.addPermanentWidget(self._container, 1)

    def set_center_text(self, txt: str):
        self.center.setText(txt)

class ShutdownWorker(QtCore.QObject):
    progress = QtCore.Signal(str)
    finished = QtCore.Signal()
    @QtCore.Slot()
    def run(self):
        self.progress.emit("Trwa zapisywanie ustawień… Proszę czekać")
        try: _flush_backend_quietly(timeout_sec=8.0)
        except Exception: pass
        self.progress.emit("Zamykanie… Do zobaczenia!")
        time.sleep(0.25); self.finished.emit()

# ──────────────────────────────── Wspólne utils ─────────────────────────────
def _format_speed(bps: float) -> str:
    kbps = bps / 1024.0
    return (f"{kbps/1024.0:.2f} MB/s") if kbps >= 1024 else (f"{kbps:.2f} KB/s")

def _progress_bar(value: float) -> QtWidgets.QProgressBar:
    bar = QtWidgets.QProgressBar()
    bar.setRange(0, 100); bar.setValue(int(value or 0))
    bar.setTextVisible(True); bar.setFormat("%p%")
    return bar

def _hline() -> QtWidgets.QFrame:
    f = QtWidgets.QFrame(); f.setFrameShape(QtWidgets.QFrame.HLine); f.setFrameShadow(QtWidgets.QFrame.Sunken)
    return f

def _error(msg: str, parent=None):
    QtWidgets.QMessageBox.critical(parent, "Błąd", msg)

def _confirm(msg: str, parent=None) -> bool:
    return QtWidgets.QMessageBox.question(parent, "Potwierdź", msg,
        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No
    ) == QtWidgets.QMessageBox.Yes

# ──────────────────────────────── Strona: Torrenty ──────────────────────────
class TorrentsPage(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        v = QtWidgets.QVBoxLayout(self)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Sortuj:"))
        self.sort = QtWidgets.QComboBox(); self.sort.addItems(["name","progress","state"])
        row.addWidget(self.sort)
        row.addSpacing(20)
        row.addWidget(QtWidgets.QLabel("🌐 Limit pobierania:"))
        self.limit = QtWidgets.QComboBox()
        self.limit.addItems(["Unlimited","1 MB/s","2 MB/s","5 MB/s","10 MB/s","15 MB/s"])
        row.addWidget(self.limit, 1)
        v.addLayout(row)
        self.feedback = QtWidgets.QLabel("Aktualnie: Unlimited")
        self.feedback.setStyleSheet("color:#c8c8c8;")
        v.addWidget(self.feedback)
        self.summary = QtWidgets.QLabel("—")
        self.summary.setStyleSheet("font-weight:600; margin:6px 0;")
        v.addWidget(self.summary)
        # Active / History toggle
        tabsRow = QtWidgets.QHBoxLayout()
        self.btnActive = QtWidgets.QPushButton("⚡ AKTYWNE"); self.btnHistory = QtWidgets.QPushButton("📁 HISTORIA")
        for b in (self.btnActive,self.btnHistory):
            b.setCheckable(True)
        self.btnActive.setChecked(True)
        tabsRow.addWidget(self.btnActive); tabsRow.addWidget(self.btnHistory); tabsRow.addStretch(1)
        v.addLayout(tabsRow)
        # List
        self.list = QtWidgets.QListWidget()
        v.addWidget(self.list, 1)

        # connections
        self.sort.currentTextChanged.connect(self.refresh_now)
        self.btnActive.toggled.connect(lambda _: self.refresh_now())
        self.btnHistory.toggled.connect(lambda _: self.refresh_now())
        self.limit.currentIndexChanged.connect(self._apply_limit)

        # timer
        self.timer = QtCore.QTimer(self); self.timer.setInterval(2000)
        self.timer.timeout.connect(self.refresh_now); self.timer.start()

        # restore saved limit
        QtCore.QTimer.singleShot(300, self._restore_limit)

    def _restore_limit(self):
        try:
            # backend nie ma zapamiętywania — trzymamy w QSettings
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
        return "active" if self.btnActive.isChecked() else "history"

    def refresh_now(self):
        try:
            r = requests.get(STATUS_URL, timeout=6)
            data = r.json()
        except Exception as e:
            self.summary.setText(f"❌ Brak połączenia: {e}")
            return

        arr = list(data.items())
        key = self.sort.currentText()
        arr.sort(key=lambda kv: kv[1].get(key, ""))

        total_speed = 0.0; active_count = 0; shown = 0
        self.list.clear()
        for tid, t in arr:
            is_done = (t.get("progress", 0) >= 100)
            if (self._active_view() == "active" and is_done) or (self._active_view() == "history" and not is_done):
                continue
            total_speed += float(t.get("download_payload_rate", 0) or 0)
            if t.get("state") == "Downloading": active_count += 1

            item = QtWidgets.QListWidgetItem()
            w = QtWidgets.QWidget(); gl = QtWidgets.QGridLayout(w); gl.setContentsMargins(6,6,6,6)
            name = QtWidgets.QLabel(f"<b>{t.get('name','')}</b>")
            details = QtWidgets.QLabel(f"📥 {_format_speed(t.get('download_payload_rate',0))} – {t.get('state','')}")
            bar = _progress_bar(float(t.get("progress", 0)))
            btnPause = QtWidgets.QPushButton("⏯️")
            btnDel = QtWidgets.QPushButton("🗑️")
            btnDelData = QtWidgets.QPushButton("🗑️+📁")
            gl.addWidget(name, 0, 0, 1, 4)
            gl.addWidget(details, 1, 0, 1, 2); gl.addWidget(bar, 2, 0, 1, 4)
            gl.addWidget(btnPause, 1, 2); gl.addWidget(btnDel, 1, 3); gl.addWidget(btnDelData, 1, 4)
            item.setSizeHint(QtCore.QSize(0, 68))
            self.list.addItem(item); self.list.setItemWidget(item, w)

            btnPause.clicked.connect(lambda _, x=tid: self._toggle(x))
            btnDel.clicked.connect(lambda _, x=tid: self._remove(x, False))
            btnDelData.clicked.connect(lambda _, x=tid: self._remove(x, True))
            shown += 1

        self.summary.setText(f"📊 Torrenty: {len(arr)}, 🚀 Aktywne: {active_count}, ⚡️ Prędkość: {_format_speed(total_speed)}")
        if shown == 0 and self._active_view() == "active":
            self.summary.setText(self.summary.text() + " • (brak aktywnych)")

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

# ──────────────────────────────── Strona: Szukaj ────────────────────────────
class SearchPage(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        v = QtWidgets.QVBoxLayout(self)

        # Tabs: movies, premium, series
        self.typeTabs = QtWidgets.QTabBar()
        self.typeTabs.addTab("🎬 Filmy"); self.typeTabs.addTab("💎 Filmy+"); self.typeTabs.addTab("📺 Seriale")
        v.addWidget(self.typeTabs)

        form = QtWidgets.QHBoxLayout()
        self.query = QtWidgets.QLineEdit(); self.query.setPlaceholderText("Szukaj tytułu")
        self.quality = QtWidgets.QComboBox(); self.quality.addItems(["Wszystkie","720p","1080p","2160p"])
        self.btnSearch = QtWidgets.QPushButton("🔍 Szukaj")
        form.addWidget(self.query, 3); form.addWidget(QtWidgets.QLabel("Jakość:")); form.addWidget(self.quality)
        form.addWidget(self.btnSearch)
        v.addLayout(form)

        self.status = QtWidgets.QLabel("")
        v.addWidget(self.status)
        self.list = QtWidgets.QListWidget()
        v.addWidget(self.list, 1)

        self.btnSearch.clicked.connect(self._search)

    def _endpoint(self) -> str:
        idx = self.typeTabs.currentIndex()
        return {0:"search", 1:"search-premium", 2:"search-series"}.get(idx, "search")

    def _search(self):
        self.list.clear(); self.status.setText("🔄 Szukam…")
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
                self.status.setText("❌ Nic nie znaleziono."); return
            for r in results:
                item = QtWidgets.QListWidgetItem()
                w = QtWidgets.QWidget(); gl = QtWidgets.QGridLayout(w); gl.setContentsMargins(6,6,6,6)
                title = QtWidgets.QLabel(f"<b>{r.get('title','')}</b>")
                desc = QtWidgets.QLabel(r.get("description") or r.get("size","") or "—")
                desc.setWordWrap(True)
                rating = QtWidgets.QLabel(f"⭐ {r.get('rating','–')}")
                btn = QtWidgets.QPushButton("🎯 Pobierz")
                gl.addWidget(title, 0, 0, 1, 3); gl.addWidget(rating, 0, 3)
                gl.addWidget(desc, 1, 0, 1, 4); gl.addWidget(btn, 2, 3)
                item.setSizeHint(QtCore.QSize(0, 92)); self.list.addItem(item); self.list.setItemWidget(item, w)
                url_or_magnet = r.get("url") or r.get("magnet")
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

        grid = QtWidgets.QGridLayout()
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
        v.addLayout(grid)

        self.btn = QtWidgets.QPushButton("📂 Przeglądaj")
        v.addWidget(self.btn)
        self.status = QtWidgets.QLabel(""); v.addWidget(self.status)
        self.list = QtWidgets.QListWidget(); v.addWidget(self.list, 1)
        self.page = 1

        nav = QtWidgets.QHBoxLayout()
        self.prev = QtWidgets.QPushButton("⬅️"); self.next = QtWidgets.QPushButton("➡️")
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
        self.page = max(1, self.page + page_delta)
        self.list.clear(); self.status.setText("🔄 Ładowanie…")
        params = {
            "quality": self.quality.currentText() if self.quality.currentText()!="All" else "0",
            "genre":   self.genre.currentText() if self.genre.currentText()!="All" else "0",
            "rating":  self.rating.currentText() if self.rating.currentText()!="All" else "0",
            "year":    self._parse_year(self.year.currentText()),
            "order":   {"IMDb Rating":"rating","YTS Likes":"likes","RT Audience":"rt_audience"}.get(self.order.currentText(), self.order.currentText().lower()),
            "page":    str(self.page),
            "language":"0", "sort_by":"0"
        }
        try:
            r = requests.get(f"{BACKEND_URL}browse", params=params, timeout=20)
            j = r.json()
            self.status.setText("")
            results = j.get("results", [])
            if not results:
                self.status.setText("❌ Brak wyników."); return
            for it in results:
                item = QtWidgets.QListWidgetItem()
                w = QtWidgets.QWidget(); gl = QtWidgets.QGridLayout(w); gl.setContentsMargins(6,6,6,6)
                title = QtWidgets.QLabel(f"<b>{it.get('title','')}</b>")
                rating = QtWidgets.QLabel(f"⭐ {it.get('rating','–')}")
                desc = QtWidgets.QLabel(it.get("description") or "Brak opisu"); desc.setWordWrap(True)
                btn = QtWidgets.QPushButton("🎯 Pobierz")
                gl.addWidget(title,0,0,1,3); gl.addWidget(rating,0,3); gl.addWidget(desc,1,0,1,4); gl.addWidget(btn,2,3)
                item.setSizeHint(QtCore.QSize(0, 92)); self.list.addItem(item); self.list.setItemWidget(item, w)
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
class AvailablePage(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        v = QtWidgets.QVBoxLayout(self)
        self.tabs = QtWidgets.QTabWidget()
        self.films = QtWidgets.QListWidget()
        self.series = QtWidgets.QListWidget()
        self.tabs.addTab(self.films, "🎬 Filmy")
        self.tabs.addTab(self.series, "📺 Seriale")
        v.addWidget(self.tabs, 1)
        self.loading = QtWidgets.QLabel(""); v.addWidget(self.loading)

        self.tabs.currentChanged.connect(self.refresh)
        QtCore.QTimer.singleShot(400, lambda: self.refresh(0))

        # odświeżaj odliczanie co 1s
        self._count_timer = QtCore.QTimer(self); self._count_timer.setInterval(1000)
        self._count_timer.timeout.connect(self._tick_countdowns)
        self._count_timer.start()

    def _tick_countdowns(self):
        # odśwież tylko podpisy z deadline’ami
        for lst in (self.films, self.series):
            for i in range(lst.count()):
                w = lst.itemWidget(lst.item(i))
                if not w: continue
                lbl = w.findChild(QtWidgets.QLabel, "deadline")
                if not lbl: continue
                ts = lbl.property("target_ts")
                if ts: lbl.setText(self._countdown_text(int(ts)))

    def _countdown_text(self, target_ms: int) -> str:
        diff = target_ms - int(time.time()*1000)
        if diff <= 0: return "✅ Do usunięcia"
        s = diff//1000; d = s//86400; h = (s%86400)//3600; m = (s%3600)//60
        if d>0: return f"⏳ {d}d {h}h"
        if h>0: return f"⏳ {h}h {m}m"
        return f"⏳ {m}m"

    def refresh(self, idx: int):
        self.loading.setText("⏳ Ładowanie…")
        if idx == 0:
            self._load_films()
        else:
            self._load_series()

    def _load_films(self):
        self.films.clear()
        try:
            r = requests.get(f"{BACKEND_URL}plex/films", timeout=15)
            data = r.json()
        except Exception as e:
            self.loading.setText(f"❌ Błąd filmów: {e}"); return
        self.loading.setText("")
        for f in data:
            item = QtWidgets.QListWidgetItem()
            w = QtWidgets.QWidget(); gl = QtWidgets.QGridLayout(w); gl.setContentsMargins(6,6,6,6)
            title = QtWidgets.QLabel(f"<b>{f.get('title','')}</b>")
            bar = _progress_bar(float(f.get("progress",0)))
            small = QtWidgets.QLabel(f"{int(f.get('progress',0))}% obejrzane")

            # deadline tylko jeśli progress 100%
            del_ts = None
            if int(float(f.get("progress",0))) >= 100:
                del_ts = f.get("deleteAt") or ( (f.get("watchedAt") or 0) + 7*24*3600*1000 if f.get("watchedAt") else None )

            deadline = QtWidgets.QLabel("--"); deadline.setObjectName("deadline")
            if del_ts:
                deadline.setProperty("target_ts", int(del_ts))
                deadline.setText(self._countdown_text(int(del_ts)))

            btnReset = QtWidgets.QPushButton("🔁 Resetuj")
            btnRemove = QtWidgets.QPushButton("🗑️ Usuń teraz")

            gl.addWidget(title,0,0,1,3); gl.addWidget(deadline,0,3)
            gl.addWidget(bar,1,0,1,4); gl.addWidget(small,2,0,1,2)
            gl.addWidget(btnReset,2,2); gl.addWidget(btnRemove,2,3)
            item.setSizeHint(QtCore.QSize(0, 92)); self.films.addItem(item); self.films.setItemWidget(item, w)

            fid = f.get("id")
            btnReset.setEnabled(bool(del_ts))
            btnReset.clicked.connect(lambda _, x=fid: self._reset_delete(x))
            btnRemove.clicked.connect(lambda _, x=fid: self._delete_item(x, "film"))

    def _load_series(self):
        self.series.clear()
        try:
            r = requests.get(f"{BACKEND_URL}plex/series", timeout=20)
            data = r.json()
        except Exception as e:
            self.loading.setText(f"❌ Błąd seriali: {e}"); return
        self.loading.setText("")
        for s in data:
            item = QtWidgets.QListWidgetItem()
            w = QtWidgets.QWidget(); gl = QtWidgets.QGridLayout(w); gl.setContentsMargins(6,6,6,6)
            title = QtWidgets.QLabel(f"<b>{s.get('title','')}</b>")
            bar = _progress_bar(float(s.get("progress",0)))
            small = QtWidgets.QLabel(f"{int(s.get('progress',0))}% obejrzane")
            deadline = QtWidgets.QLabel("--"); deadline.setObjectName("deadline")
            del_ts = s.get("deleteAt");
            if del_ts:
                deadline.setProperty("target_ts", int(del_ts))
                deadline.setText(self._countdown_text(int(del_ts)))
            btnReset = QtWidgets.QPushButton("🔁 Resetuj")
            btnRemove = QtWidgets.QPushButton("🗑️ Usuń")
            btnReset.setEnabled(bool(del_ts))
            gl.addWidget(title,0,0,1,3); gl.addWidget(deadline,0,3)
            gl.addWidget(bar,1,0,1,4); gl.addWidget(small,2,0,1,2)
            gl.addWidget(btnReset,2,2); gl.addWidget(btnRemove,2,3)
            item.setSizeHint(QtCore.QSize(0, 92)); self.series.addItem(item); self.series.setItemWidget(item, w)
            sid = s.get("id")
            btnReset.clicked.connect(lambda _, x=sid: self._reset_delete(x))
            btnRemove.clicked.connect(lambda _, x=sid: self._delete_item(x, "series"))

    def _reset_delete(self, _id: str):
        if not _confirm("Zresetować czas usunięcia (7 dni od teraz)?", self): return
        try:
            j = requests.post(f"{BACKEND_URL}plex/reset-delete-timer", json={"id": _id}, timeout=10).json()
            if not (j.get("success") and j.get("newDeleteAt")):
                _error("❌ Błąd resetowania.", self); return
            self.refresh(self.tabs.currentIndex())
        except Exception as e:
            _error(f"❌ Błąd połączenia: {e}", self)

    def _delete_item(self, _id: str, type_: str):
        if not _confirm("Na pewno usunąć?", self): return
        try:
            j = requests.delete(f"{BACKEND_URL}plex/delete", json={"id": _id, "force": (type_!="film")}, timeout=15).json()
            if not j.get("success"):
                _error("❌ Błąd usuwania.", self); return
            QtWidgets.QMessageBox.information(self, "OK", "🗑️ Usunięto")
            self.refresh(self.tabs.currentIndex())
        except Exception as e:
            _error(f"❌ Błąd połączenia: {e}", self)

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
        self.status = PrettyStatusBar(self); self.setStatusBar(self.status)
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
    if not getattr(app, "_pflx_about_connected", False):
        app.aboutToQuit.connect(lambda: _flush_backend_quietly())
        app._pflx_about_connected = True  # type: ignore[attr-defined]
    _QAPP = app
    return _QAPP

def show_splash():
    global _SPLASH
    app = _ensure_qapp()
    _SPLASH = LoadingSplash(); _SPLASH.show(); app.processEvents()
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

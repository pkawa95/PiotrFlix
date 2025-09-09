# launcher.py
# -*- coding: utf-8 -*-
import os, sys, time, threading, webbrowser, importlib, importlib.util, pathlib
import requests
import signal
import subprocess
import re
import json  # ⬅️ NOWE: potrzebne do version.json

APP_NAME = "Piotrflix"
APP_VERSION = "1.0.0"
BACKEND_URL = "http://127.0.0.1:5000/"
STATUS_URL  = "http://127.0.0.1:5000/status"
BASE_DIR = os.path.abspath(".")
APP_ICON_PATH = os.path.join(BASE_DIR, "static", "icon.png")
APP_LOGO_PATH = os.path.join(BASE_DIR, "static", "logo.png")
_EXITING = False
_SHUTDOWN_THREAD = None
_SHUTDOWN_WORKER = None
GITHUB_OWNER = os.environ.get("PFLIX_GH_OWNER", "pkawa95")
GITHUB_REPO  = os.environ.get("PFLIX_GH_REPO",  "PiotrFlix")
ALLOW_PRERELEASES = False  # jeśli chcesz brać prereleasy, ustaw True

# ───────────────────────────── grafika / flagi (USTAW PRZED importem Qt) ────
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
    mode = (os.environ.get("PFLIX_RENDER") or "gpu").strip().lower()
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    os.environ.setdefault("QTWEBENGINE_PROFILE_STORAGE_NAME", os.path.join(STATE_DIR, "webstorage"))
    os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

    flags = ["--no-sandbox"]
    if mode == "gpu":
        os.environ["QT_OPENGL"] = "angle"
        flags += [
            "--ignore-gpu-blocklist",
            "--enable-gpu-rasterization",
            "--enable-zero-copy",
            "--enable-accelerated-video-decode",
            "--enable-features=CanvasOopRasterization,AcceleratedVideoDecode,VaapiVideoDecode",
        ]
    else:
        os.environ["QT_OPENGL"] = "software"
        flags += ["--disable-gpu", "--use-gl=swiftshader", "--enable-software-rasterizer"]

    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags)

_setup_graphics()

# ───────────────────────────── WERSJA: pliki i zapis atomowy ─────────────────
def _app_root_dir() -> str:
    """Katalog aplikacji: obok exe (frozen) lub katalog źródeł (dev)."""
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else BASE_DIR

def _localappdata_dir() -> str:
    lad = os.environ.get("LOCALAPPDATA")
    if not lad:
        # fallback – powinno rzadko być potrzebne
        lad = os.path.expanduser(r"~\AppData\Local")
    return os.path.join(lad, APP_NAME)

def _write_json_atomic(path: str, payload: dict):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        # celowo cicho – nie blokujemy startu aplikacji, updater i tak poradzi sobie później
        pass

def ensure_version_files():
    """
    Zapisuje {"version": APP_VERSION} do:
      1) APP_ROOT/version.json
      2) %LOCALAPPDATA%/Piotrflix/version.json
    (Drugi jest czytany przez updater.)
    """
    data = {"version": APP_VERSION}
    app_root = _app_root_dir()
    la_dir   = _localappdata_dir()
    paths = [
        os.path.join(app_root, "version.json"),
        os.path.join(la_dir, "version.json"),
    ]
    for p in paths:
        # tylko jeśli brak albo inna wersja → nadpisz; to bezpieczny, szybki check
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

# config + onboarding (GUI jest źródłem prawdy)
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
BACKEND_MOD = None          # przypiszemy w run()
FLASK_THREAD = None         # referencja do wątku Flaska

def _delete_config_files():
    """Usuń config gdzie to ma sens – jeśli masz w config_store delete_config(), użyj go."""
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
    """
    Elegancko zamyka backend:
    1) app.graceful_shutdown() → app._graceful_shutdown() → tclient.shutdown()
    2) fallback: POST /admin/shutdown
    3) krótkie czekanie na zejście wątków
    """
    called = False
    try:
        if BACKEND_MOD:
            if hasattr(BACKEND_MOD, "graceful_shutdown"):
                BACKEND_MOD.graceful_shutdown()
                called = True
            elif hasattr(BACKEND_MOD, "_graceful_shutdown"):
                BACKEND_MOD._graceful_shutdown()
                called = True
            elif hasattr(BACKEND_MOD, "tclient") and getattr(BACKEND_MOD, "tclient"):
                try:
                    BACKEND_MOD.tclient.shutdown()
                    called = True
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

# ── Windows creation flags do updatera
CREATE_NO_WINDOW         = 0x08000000
DETACHED_PROCESS         = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

def _find_updater() -> str | None:
    """
    Szuka updater.exe w typowych miejscach instalacyjnych i dev.
    Priorytet: obok głównego EXE, potem tools/.
    """
    exe_dir  = os.path.dirname(sys.executable)  # w instalce to katalog aplikacji
    base_dir = BASE_DIR

    candidates = [
        os.path.join(exe_dir, "updater.exe"),
        os.path.join(exe_dir, "tools", "updater.exe"),
        os.path.join(base_dir, "updater.exe"),
        os.path.join(base_dir, "tools", "updater.exe"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None

def _update_now():
    try:
        # folder aplikacji (działa i w trybie dev, i po spakowaniu)
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

    # pokaż splash „Trwa zapisywanie ustawień…” i elegancko zamknij
    quit_app_graceful()

def quit_app_graceful():
    """
    Sekwencja wyjścia z ładnym splashem:
    - chowa okno główne natychmiast,
    - pokazuje splash,
    - flush backendu w QThread,
    - po zakończeniu wychodzi.
    """
    global _EXITING, _SPLASH, _MAIN_WIN, _SHUTDOWN_THREAD, _SHUTDOWN_WORKER
    if _EXITING:
        return
    _EXITING = True

    app = QtWidgets.QApplication.instance() or _ensure_qapp()

    # schowaj główne okno i tacę (opcjonalnie)
    try:
        if _MAIN_WIN is not None:
            _MAIN_WIN.hide()
            if getattr(_MAIN_WIN, "tray", None):
                _MAIN_WIN.tray.hide()
    except Exception:
        pass

    # pokaż splash (w wątku GUI)
    if _SPLASH is None or not isinstance(_SPLASH, LoadingSplash):
        _SPLASH = LoadingSplash()
    try:
        _SPLASH.title.setText(APP_NAME)
    except Exception:
        pass
    _SPLASH.set_status("Trwa zapisywanie ustawień… Proszę czekać")
    _SPLASH.show()
    app.processEvents()

    # odpal QThread z workerem – GUI zostaje responsywne
    _SHUTDOWN_THREAD = QtCore.QThread()
    _SHUTDOWN_WORKER = ShutdownWorker()
    _SHUTDOWN_WORKER.moveToThread(_SHUTDOWN_THREAD)

    _SHUTDOWN_THREAD.started.connect(_SHUTDOWN_WORKER.run)
    _SHUTDOWN_WORKER.progress.connect(lambda msg: _SPLASH.set_status(msg))
    _SHUTDOWN_WORKER.finished.connect(_SHUTDOWN_THREAD.quit)
    _SHUTDOWN_WORKER.finished.connect(lambda: QtCore.QTimer.singleShot(50, lambda: app.quit()))

    # sprzątanie obiektów po zejściu
    _SHUTDOWN_THREAD.finished.connect(_SHUTDOWN_WORKER.deleteLater)
    _SHUTDOWN_THREAD.finished.connect(_SHUTDOWN_THREAD.deleteLater)

    _SHUTDOWN_THREAD.start()

def reset_app_full():
    """
    Pełny reset: flush backendu, skasuj config, restart procesu (execve).
    """
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
        v.setContentsMargins(24, 24, 24, 24)
        v.setSpacing(16)

        # ikona
        self.icon_label = QtWidgets.QLabel()
        self.icon_label.setFixedSize(128, 128)
        self._set_rounded_icon(APP_ICON_PATH, 20)
        self.icon_label.setAlignment(Qt.AlignCenter)

        # tytuł
        self.title = QtWidgets.QLabel(APP_NAME)
        self.title.setStyleSheet("font-size:20px;font-weight:600;")
        self.title.setAlignment(Qt.AlignCenter)

        # status (zostaje, bo go aktualizujesz przy starcie/zamykaniu)
        self.status = QtWidgets.QLabel("Inicjalizacja…")
        self.status.setStyleSheet("font-size:14px;")
        self.status.setAlignment(Qt.AlignCenter)

        v.addStretch(1)
        v.addWidget(self.icon_label, 0, Qt.AlignCenter)
        v.addWidget(self.title, 0, Qt.AlignCenter)
        v.addWidget(self.status, 0, Qt.AlignCenter)
        v.addStretch(1)

        scr = QtWidgets.QApplication.primaryScreen().availableGeometry()
        self.move(scr.center() - self.rect().center())

    def closeEvent(self, e: QtGui.QCloseEvent) -> None:
        super().closeEvent(e)

    def _set_rounded_icon(self, path: str, radius: int):
        pix = QtGui.QPixmap(path)
        if pix.isNull():
            pix = QtGui.QPixmap(128, 128); pix.fill(Qt.transparent)
        pix = pix.scaled(128, 128, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)

        mask = QtGui.QBitmap(128, 128); mask.clear()
        painter = QtGui.QPainter(mask)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setBrush(Qt.black); painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(0, 0, 128, 128, radius, radius)
        painter.end()

        rounded = QtGui.QPixmap(128, 128); rounded.fill(Qt.transparent)
        p2 = QtGui.QPainter(rounded)
        p2.setRenderHint(QtGui.QPainter.Antialiasing)
        p2.setClipRegion(QtGui.QRegion(mask))
        p2.drawPixmap(0, 0, pix); p2.end()
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
        row.addWidget(self.btn_reset); row.addWidget(self.btn_update)
        lay.addLayout(row)

        row2 = QtWidgets.QHBoxLayout(); row2.addStretch(1)
        ok = QtWidgets.QPushButton("OK"); cancel = QtWidgets.QPushButton("Anuluj")
        row2.addWidget(cancel); row2.addWidget(ok); lay.addLayout(row2)

        ok.clicked.connect(self.accept); cancel.clicked.connect(self.reject)
        self.btn_update.clicked.connect(_update_now)      # ⬅️ uruchom updater.exe
        self.btn_reset.clicked.connect(self._reset)

        self.chk_autostart.setChecked(False)
        self.chk_min_to_tray.setChecked(True)

    def accept(self):
        # TODO: zapisz ustawienia / autostart
        super().accept()

    def _reset(self):
        if QtWidgets.QMessageBox.question(
            self, "Potwierdź", "Usunąć konfigurację i zrestartować?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No
        ) != QtWidgets.QMessageBox.Yes:
            return
        reset_app_full()  # PELNY RESET (flush + delete config + execve)

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
        # kontener, który zajmuje całą szerokość statusbara
        self._container = QtWidgets.QWidget(self)
        h = QtWidgets.QHBoxLayout(self._container)
        h.setContentsMargins(8, 2, 8, 2)
        h.setSpacing(10)

        # mini logo po lewej
        self.logo = QtWidgets.QLabel(self._container)
        pix = QtGui.QPixmap(APP_LOGO_PATH)
        if pix.isNull():
            pix = QtGui.QPixmap(18, 18); pix.fill(QtGui.QColor("#444"))
        self.logo.setPixmap(pix.scaledToHeight(18, Qt.SmoothTransformation))
        self.logo.setFixedHeight(18)

        # centralny tekst/status
        self.center = QtWidgets.QLabel(self._container)
        self.center.setAlignment(Qt.AlignCenter)
        self.center.setStyleSheet("font-size:12px; letter-spacing:0.2px;")

        h.addWidget(self.logo, 0, Qt.AlignLeft | Qt.AlignVCenter)
        h.addStretch(1)
        h.addWidget(self.center, 0, Qt.AlignCenter)
        h.addStretch(1)

        self.addPermanentWidget(self._container, 1)

    def set_center_text(self, txt: str):
        self.center.setText(txt)

class ShutdownWorker(QtCore.QObject):
    progress = QtCore.Signal(str)
    finished = QtCore.Signal()

    @QtCore.Slot()
    def run(self):
        # krok 1: info
        self.progress.emit("Trwa zapisywanie ustawień… Proszę czekać")
        try:
            _flush_backend_quietly(timeout_sec=8.0)
        except Exception:
            pass
        # krok 2: finał
        self.progress.emit("Zamykanie… Do zobaczenia!")
        time.sleep(0.25)
        self.finished.emit()

# ─────────────────────────────── Główne okno (WebView) ──────────────────────
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        if os.path.isfile(APP_ICON_PATH):
            self.setWindowIcon(QtGui.QIcon(APP_ICON_PATH))
        self.resize(1280, 800)

        self._tune_webengine_globals()
        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView
            self.web = QWebEngineView(self)
            self.web.setAttribute(Qt.WA_OpaquePaintEvent, True)

            s = self.web.settings()
            from PySide6.QtWebEngineCore import QWebEngineSettings
            s.setAttribute(QWebEngineSettings.WebGLEnabled, True)
            s.setAttribute(QWebEngineSettings.Accelerated2dCanvasEnabled, True)
            s.setAttribute(QWebEngineSettings.ScrollAnimatorEnabled, True)
            s.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
            s.setAttribute(QWebEngineSettings.PluginsEnabled, False)

            self.web.page().renderProcessTerminated.connect(self._on_render_crash)

            self.setCentralWidget(self.web)
            self.web.setUrl(QtCore.QUrl(BACKEND_URL))
            self._web_ok = True
        except Exception as e:
            print("⚠️ QWebEngineView nie wystartował:", e)
            self._web_ok = False
            wid = QtWidgets.QWidget(self); self.setCentralWidget(wid)
            lay = QtWidgets.QVBoxLayout(wid)
            lbl = QtWidgets.QLabel(
                "Nie udało się uruchomić wbudowanej przeglądarki.\n"
                "Otwieram interfejs w domyślnej przeglądarce…"
            )
            lbl.setAlignment(Qt.AlignCenter)
            lay.addWidget(lbl)
            QtCore.QTimer.singleShot(200, lambda: webbrowser.open(BACKEND_URL))

        # ── MENUBAR: Plik, Ustawienia, O mnie (w tej kolejności)
        mb = self.menuBar()

        m_file = mb.addMenu("Plik")
        act_update = QtGui.QAction("Aktualizuj teraz", self)
        act_update.triggered.connect(_update_now)
        m_file.addAction(act_update)

        act_reset = QtGui.QAction("Zresetuj aplikację", self)
        act_reset.triggered.connect(reset_app_full)
        m_file.addAction(act_reset)

        act_exit = QtGui.QAction("Wyjście", self)
        act_exit.triggered.connect(quit_app_graceful)
        m_file.addAction(act_exit)

        m_settings = mb.addMenu("Ustawienia")
        act_settings = QtGui.QAction("Ustawienia…", self)
        act_settings.triggered.connect(self._open_settings)
        m_settings.addAction(act_settings)

        m_about = mb.addMenu("O mnie")
        act_me = QtGui.QAction("O mnie", self)
        act_me.triggered.connect(lambda: webbrowser.open("http://pkportfolio.pl/"))
        m_about.addAction(act_me)

        # ── ładna stopka
        self.status = PrettyStatusBar(self)
        self.setStatusBar(self.status)
        self.status.set_center_text(f"© Piotrflix • v{APP_VERSION} • aktualność: sprawdzam…")
        self._init_updates()

        # ── tray
        self._min_to_tray = True
        if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QtWidgets.QSystemTrayIcon(QtGui.QIcon(APP_ICON_PATH), self)
            menu = QtWidgets.QMenu()
            menu.addAction("Pokaż", self._show_from_tray)
            menu.addSeparator()
            menu.addAction("Aktualizuj teraz", _update_now)
            menu.addAction("Zresetuj aplikację", reset_app_full)
            menu.addAction("Zamknij", quit_app_graceful)
            self.tray.setContextMenu(menu)
            self.tray.show()
        else:
            self.tray = None

    def _tune_webengine_globals(self):
        try:
            from PySide6.QtWebEngineCore import QWebEngineProfile
            prof = QWebEngineProfile.defaultProfile()
            prof.setCachePath(os.path.join(STATE_DIR, "webcache"))
            prof.setPersistentStoragePath(os.path.join(STATE_DIR, "webstorage"))
            prof.setPersistentCookiesPolicy(QWebEngineProfile.AllowPersistentCookies)
            prof.setHttpCacheType(QWebEngineProfile.DiskHttpCache)
        except Exception as e:
            print("ℹ️ WebEngineProfile tuning skip:", e)

    def _on_render_crash(self, status, code):
        print(f"💥 Render process terminated (status={status}, code={code}). Fallback → software…")
        if (os.environ.get("PFLIX_RENDER") or "gpu").lower() != "software":
            env = dict(os.environ); env["PFLIX_RENDER"] = "software"
            python = sys.executable; args = [python] + sys.argv
            QtCore.QTimer.singleShot(50, lambda: os.execve(python, args, env))

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
        # przygotuj akcję menu (jeśli istnieje – dostosuj tekst po wykryciu)
        self._update_version_target = None  # np. "v1.2.3"
        # znajdź akcję „Aktualizuj teraz” jeśli chcesz dynamicznie zmieniać jej tekst
        self._act_update_ref = None
        for m in self.menuBar().findChildren(QtWidgets.QMenu):
            for a in m.actions():
                if a.text().startswith("Aktualizuj"):
                    self._act_update_ref = a
                    break

        # wątek + worker
        self._upd_thread = QtCore.QThread(self)
        self._upd_worker = UpdateChecker(GITHUB_OWNER, GITHUB_REPO, APP_VERSION)
        self._upd_worker.moveToThread(self._upd_thread)

        # sygnały
        self._upd_thread.started.connect(self._upd_worker.check_once)
        self._upd_worker.info.connect(lambda msg: self.status.set_center_text(
            f"© Piotrflix • v{APP_VERSION} • {msg}"
        ))
        self._upd_worker.none.connect(self._on_update_none)
        self._upd_worker.available.connect(self._on_update_available)
        self._upd_worker.error.connect(self._on_update_error)

        # sprzątanie
        self._upd_worker.none.connect(self._upd_thread.quit)
        self._upd_worker.available.connect(self._upd_thread.quit)
        self._upd_worker.error.connect(self._upd_thread.quit)
        self._upd_thread.finished.connect(self._upd_worker.deleteLater)
        self._upd_thread.finished.connect(self._upd_thread.deleteLater)

        # odpal jednorazowo po starcie okna (krótkie opóźnienie, żeby UI zdążyło wstać)
        QtCore.QTimer.singleShot(1200, self._upd_thread.start)

    # ——— Handlery statusów aktualizacji ———
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
    if _QAPP is None:
        QtCore.QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
        _QAPP = QtWidgets.QApplication(sys.argv)
        _QAPP.setApplicationName(APP_NAME)
        _QAPP.setOrganizationName(APP_NAME)
        _QAPP.setQuitOnLastWindowClosed(False)  # ważne dla traya i splash-exitu
        if os.path.isfile(APP_ICON_PATH):
            _QAPP.setWindowIcon(QtGui.QIcon(APP_ICON_PATH))
        try:
            from PySide6.QtWebEngineCore import QtWebEngine
            QtWebEngine.initialize()
        except Exception:
            pass
        _QAPP.aboutToQuit.connect(lambda: _flush_backend_quietly())
    return _QAPP

def show_splash():
    global _SPLASH
    app = _ensure_qapp()
    _SPLASH = LoadingSplash()
    _SPLASH.show()
    app.processEvents()
    return _SPLASH

def show_main_webview(url: str = BACKEND_URL):
    global _MAIN_WIN
    _MAIN_WIN = MainWindow()
    try:
        if hasattr(_MAIN_WIN, "web") and url and getattr(_MAIN_WIN, "_web_ok", False):
            _MAIN_WIN.web.setUrl(QtCore.QUrl(url))
    except Exception:
        pass
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
        _wire_fallbacks(mod)
        return mod
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
    _wire_fallbacks(mod)
    return mod

def _wire_fallbacks(mod):
    if not hasattr(mod, "start_flask_blocking"):
        if hasattr(mod, "app") and hasattr(mod.app, "run"):
            def _start_flask_blocking():
                mod.app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False, threaded=True)
            mod.start_flask_blocking = _start_flask_blocking
        else:
            raise RuntimeError("Backend nie ma obiektu Flask 'app' – nie mogę uruchomić serwera.")
    if not hasattr(mod, "init_backend_after_splash"):
        def _init_backend_after_splash():
            if hasattr(mod, "_do_available_bootstrap"):
                mod._do_available_bootstrap()
            if hasattr(mod, "_start_all_backgrounds"):
                mod._start_all_backgrounds()
        mod.init_backend_after_splash = _init_backend_after_splash

def _norm_ver(v: str) -> tuple:
    nums = [int(x) for x in re.findall(r"\d+", v or "")][:4]
    while len(nums) < 4:
        nums.append(0)
    return tuple(nums)

class UpdateChecker(QtCore.QObject):
    info = QtCore.Signal(str)                 # np. "sprawdzam…"
    available = QtCore.Signal(dict)           # {version, url, body}
    none = QtCore.Signal()                    # brak update
    error = QtCore.Signal(str)                # opis błędu

    def __init__(self, owner: str, repo: str, cur_ver: str):
        super().__init__()
        self.owner = owner
        self.repo = repo
        self.cur_ver = cur_ver

    @QtCore.Slot()
    def check_once(self):
        try:
            self.info.emit("Sprawdzam aktualizacje…")
            if not self.owner or not self.repo or "YOUR_GH_" in self.owner+self.repo:
                self.error.emit("Repozytorium nie skonfigurowane")
                return
            url = f"https://api.github.com/repos/{self.owner}/{self.repo}/releases"
            r = requests.get(url, timeout=6)
            r.raise_for_status()
            releases = r.json() or []
            rel = next((x for x in releases
                        if ALLOW_PRERELEASES or not x.get("prerelease")), None)
            if not rel:
                self.none.emit(); return
            tag = rel.get("tag_name") or rel.get("name") or ""
            if _norm_ver(tag) > _norm_ver(self.cur_ver):
                self.available.emit({
                    "version": tag,
                    "url": rel.get("html_url") or "",
                    "body": rel.get("body") or ""
                })
            else:
                self.none.emit()
        except Exception as e:
            self.error.emit(str(e))

# ───────────────────────────── Poller: czekanie na /status ───────────────────
class StatusWaiter(QtCore.QObject):
    ready = QtCore.Signal(str)     # url
    timeout = QtCore.Signal()
    info = QtCore.Signal(str)      # komunikaty dla splash

    def __init__(self, url: str, timeout_sec: int = 40, interval: float = 0.35):
        super().__init__()
        self.url = url
        self.timeout_sec = timeout_sec
        self.interval = interval
        self._th = None

    def start(self):
        def _run():
            t0 = time.time()
            while time.time() - t0 < self.timeout_sec:
                try:
                    self.info.emit("Łączenie z serwerem…")
                    r = requests.get(self.url, timeout=1.5)
                    if r.status_code == 200:
                        self.ready.emit(self.url)
                        return
                except Exception:
                    pass
                time.sleep(self.interval)
            self.timeout.emit()
        self._th = threading.Thread(target=_run, daemon=True)
        self._th.start()

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
    # ⬅️⬅️⬅️ 1) NA SAMYM POCZĄTKU: utwórz/odśwież pliki wersji
    ensure_version_files()

    if not ensure_configuration():
        sys.exit(1)

    splash = show_splash()
    splash.set_status("Ładowanie modułów…")

    global BACKEND_MOD, FLASK_THREAD
    BACKEND_MOD = import_backend_safe()

    splash.set_status("Uruchamianie serwera…")
    FLASK_THREAD = threading.Thread(target=BACKEND_MOD.start_flask_blocking, daemon=True, name="flask")
    FLASK_THREAD.start()

    def _heavy_init():
        try:
            splash.set_status("Startowanie aplikacji…")
        except Exception:
            pass
        BACKEND_MOD.init_backend_after_splash()
    threading.Thread(target=_heavy_init, daemon=True, name="backend-init").start()

    waiter = StatusWaiter(STATUS_URL, timeout_sec=40, interval=0.35)
    waiter.info.connect(lambda msg: splash.set_status(msg))
    waiter.ready.connect(lambda _: _open_ui_and_close_splash(splash))
    waiter.timeout.connect(lambda: _open_ui_and_close_splash(splash, warn=True))
    waiter.start()

    _install_signal_handlers()

    app = QtWidgets.QApplication.instance()
    sys.exit(app.exec())

def _open_ui_and_close_splash(splash: LoadingSplash, warn: bool = False):
    def _show():
        if warn:
            splash.set_status("Nie potwierdzono statusu — uruchamiam interfejs…")
        else:
            splash.set_status("Gotowe. Uruchamianie interfejsu…")
        show_main_webview(BACKEND_URL)
        splash.close()
    QtCore.QTimer.singleShot(0, _show)

if __name__ == "__main__":
    run()

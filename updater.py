# tools/updater.py
# -*- coding: utf-8 -*-
import os, sys, time, shutil, hashlib, zipfile, tempfile, subprocess, ctypes
import requests
import json

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

# ───────────────────────────── KONFIG ─────────────────────────────
APP_NAME   = "Piotrflix"
EXE_NAME   = "PiotrFlix.exe"  # nazwa pliku EXE obok updatera

def _exe_dir():
    # działa zarówno w PyInstaller (sys._MEIPASS) jak i przy uruchamianiu .py/.exe
    return os.path.abspath(os.path.dirname(sys.argv[0]))

# Instalacja i pliki w TYM SAMYM FOLDERZE co updater
INSTALL_DIR = _exe_dir()
APP_EXE     = os.path.join(INSTALL_DIR, EXE_NAME)
LOCAL_VERSION_FILE = os.path.join(INSTALL_DIR, "version.json")

# GitHub API – repo z wydaniami
GITHUB_OWNER = "pkawa95"
GITHUB_REPO  = "PiotrFlix"

# Nazwy assetów w release
ASSET_ZIP_NAME  = "Piotrflix-Windows-x64.zip"  # paczka z całą aplikacją
ASSET_SUMS_NAME = "SHA256SUMS"                 # plik z sumami (zawiera wiersz dla powyższego ZIPa)

# Endpoints aplikacji (do eleganckiego zamykania backendu)
STATUS_URL = "http://127.0.0.1:5000/status"
ADMIN_SHUTDOWN = "http://127.0.0.1:5000/admin/shutdown"

# ───────────────────────────── UTIL: wersje / GitHub ────────────────────────
def get_local_version() -> str:
    try:
        with open(LOCAL_VERSION_FILE, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("version", "").strip()
    except Exception:
        return ""

def _parse_version(v: str):
    parts = []
    for x in (v or "").strip().lstrip("v").split("."):
        try:
            parts.append(int(x))
        except Exception:
            parts.append(0)
    return tuple(parts or [0])

def compare_versions(v_local: str, v_remote: str) -> bool:
    """True jeśli zdalna wersja jest nowsza."""
    return _parse_version(v_remote) > _parse_version(v_local)

def get_latest_release():
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return r.json()

def find_asset(release, name):
    for a in release.get("assets", []):
        if a.get("name") == name:
            return a.get("browser_download_url")
    return None

# ───────────────────────────── UTIL: pliki / hash ───────────────────────────
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def ensure_dir(p):
    if p:
        os.makedirs(p, exist_ok=True)

# Windows: opóźniona podmiana po restarcie (gdy plik zablokowany)
MOVEFILE_REPLACE_EXISTING = 0x1
MOVEFILE_DELAY_UNTIL_REBOOT = 0x4
def _schedule_replace_on_reboot(temp_path: str, dest_path: str) -> bool:
    try:
        MoveFileExW = ctypes.windll.kernel32.MoveFileExW
        MoveFileExW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        MoveFileExW.restype = ctypes.c_bool
        ok = MoveFileExW(temp_path, dest_path, MOVEFILE_REPLACE_EXISTING | MOVEFILE_DELAY_UNTIL_REBOOT)
        return bool(ok)
    except Exception:
        return False

def replace_file_atomic(dest: str, data: bytes) -> None:
    ensure_dir(os.path.dirname(dest))
    tmp = dest + ".updtmp"
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        if os.path.isfile(dest):
            try:
                os.remove(dest)
            except Exception:
                pass
        os.replace(tmp, dest)
    except PermissionError:
        _schedule_replace_on_reboot(tmp, dest)
    except Exception:
        pass

# ───────────────────────────── UTIL: zamykanie aplikacji ────────────────────
def try_shutdown(status_cb=None):
    try:
        if status_cb: status_cb("Zamykanie aplikacji…")
        requests.post(ADMIN_SHUTDOWN, json={"reason": "updater"}, timeout=1.5)
    except Exception:
        pass
    t0 = time.time()
    while time.time() - t0 < 15:
        try:
            requests.get(STATUS_URL, timeout=1.2)
            time.sleep(0.5)
        except Exception:
            break

# ───────────────────────────── POMOC: ZIP z katalogiem na wierzchu ─────────
def _norm(p: str) -> str:
    return p.replace("\\", "/")

def _strip_common_topdir(members):
    """
    Jeśli wszystkie pliki w zip mają wspólny katalog top-level (np. 'Piotrflix-Windows-x64/'),
    zwróć jego nazwę (z ukośnikiem). W przeciwnym wypadku zwróć ''.
    """
    files = [_norm(m.filename) for m in members if not m.is_dir()]
    if not files:
        return ""
    first_top = files[0].split("/")[0]
    if first_top and all(f.startswith(first_top + "/") for f in files):
        return first_top + "/"
    return ""

# ───────────────────────────── GUI: worker w tle ────────────────────────────
class UpdaterWorker(QtCore.QObject):
    progress = QtCore.Signal(int)        # 0..100
    status   = QtCore.Signal(str)        # komunikat
    logline  = QtCore.Signal(str)        # drobny log (opcjonalny)
    done     = QtCore.Signal(bool, str)  # success, msg

    # Wagi faz → łączny progress 100%
    W_CHECK = 3
    W_DL_SUMS = 2
    W_DL_ZIP = 60
    W_APPLY = 30
    W_FINISH = 5

    def __init__(self, parent=None):
        super().__init__(parent)
        self.install_dir = INSTALL_DIR
        self.app_exe = APP_EXE
        # pełna ścieżka do bieżącego procesu (updatera), żeby go NIE podmieniać
        try:
            self.self_path = os.path.abspath(sys.argv[0])
        except Exception:
            self.self_path = ""

    def _emitp(self, p):
        self.progress.emit(int(max(0, min(100, p))))

    def _download_with_progress(self, url, out_path, base, span):
        self.status.emit("Pobieranie…")
        r = requests.get(url, stream=True, timeout=60)
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if chunk:
                    f.write(chunk)
                    got += len(chunk)
                    pct = (got / total) if total else 0.0
                    self._emitp(base + pct * span)
        self._emitp(base + span)

    @QtCore.Slot()
    def run(self):
        try:
            self.status.emit("Sprawdzanie aktualizacji…")
            self._emitp(0)

            local_v = get_local_version()
            self.logline.emit(f"Lokalna wersja: {local_v or '(brak)'}")

            rel = get_latest_release()
            remote_v = rel.get("tag_name") or rel.get("name") or ""
            remote_v = remote_v.lstrip("v")
            self.logline.emit(f"Zdalna wersja: {remote_v or '(nieznana)'}")

            self._emitp(self.W_CHECK)

            if not remote_v:
                self.done.emit(False, "Nie udało się odczytać wersji zdalnej.")
                return

            if not compare_versions(local_v, remote_v):
                self.status.emit("Brak nowszej wersji. Uruchamianie aplikacji…")
                self._emitp(100)
                self.done.emit(True, "up_to_date")
                return

            # Pobierz linki do assetów
            zip_url  = find_asset(rel, ASSET_ZIP_NAME)
            sums_url = find_asset(rel, ASSET_SUMS_NAME)
            if not zip_url or not sums_url:
                self.done.emit(False, "Wydanie nie zawiera wymaganych plików (ZIP/SHA256SUMS).")
                return

            with tempfile.TemporaryDirectory() as td:
                zip_path  = os.path.join(td, ASSET_ZIP_NAME)
                sums_path = os.path.join(td, ASSET_SUMS_NAME)

                # 1) SUMS
                self.status.emit("Pobieranie sum kontrolnych…")
                try:
                    r = requests.get(sums_url, timeout=60)
                    r.raise_for_status()
                    with open(sums_path, "wb") as f:
                        f.write(r.content)
                except Exception as e:
                    self.done.emit(False, f"Nie udało się pobrać sum kontrolnych: {e}")
                    return
                self._emitp(self.W_CHECK + self.W_DL_SUMS)

                # 2) ZIP z progresem
                base_after_sums = self.W_CHECK + self.W_DL_SUMS
                self._download_with_progress(zip_url, zip_path, base_after_sums, self.W_DL_ZIP)

                # 3) Weryfikacja SHA256
                self.status.emit("Weryfikacja plików…")
                want = None
                try:
                    with open(sums_path, "r", encoding="utf-8") as f:
                        for line in f:
                            if ASSET_ZIP_NAME in line:
                                want = line.split()[0].strip()
                                break
                except Exception:
                    pass

                got = sha256_file(zip_path)
                self.logline.emit(f"ZIP sha256: {got}")
                if not want or want.lower() != got.lower():
                    self.done.emit(False, "Błąd weryfikacji: niezgodna suma SHA256.")
                    return

                # 4) Zamykanie aplikacji
                try_shutdown(lambda m: self.status.emit(m))
                self._emitp(self.W_CHECK + self.W_DL_SUMS + self.W_DL_ZIP)

                # 5) Podmiana plików w bieżącym folderze
                self.status.emit("Podmiana plików…")
                try:
                    with zipfile.ZipFile(zip_path, "r") as z:
                        members = [m for m in z.infolist() if not m.is_dir()]
                        top = _strip_common_topdir(members)
                        total = max(1, len(members))

                        for i, m in enumerate(members, 1):
                            relpath = _norm(m.filename)
                            if top and relpath.startswith(top):
                                relpath = relpath[len(top):]

                            if not relpath:
                                continue

                            dest = os.path.join(self.install_dir, relpath)
                            ensure_dir(os.path.dirname(dest))

                            # wczytaj dane z ZIP
                            with z.open(m, "r") as src:
                                data = src.read()

                            # Nie podmieniaj aktywnego updatera (ani tego samego pliku pod inną nazwą)
                            dest_abs = os.path.abspath(dest)
                            if self.self_path and os.path.samefile(dest_abs, self.self_path):
                                self.logline.emit(f"Pomijam plik aktualnie uruchomionego updatera: {dest_abs}")
                            else:
                                try:
                                    if os.path.isfile(dest):
                                        if hashlib.sha256(open(dest, "rb").read()).hexdigest() == hashlib.sha256(data).hexdigest():
                                            pass
                                        else:
                                            replace_file_atomic(dest, data)
                                    else:
                                        replace_file_atomic(dest, data)
                                except Exception:
                                    replace_file_atomic(dest, data)

                            # progres tej fazy
                            pct_apply = (i / total)
                            base = self.W_CHECK + self.W_DL_SUMS + self.W_DL_ZIP
                            self._emitp(base + pct_apply * self.W_APPLY)
                except Exception as e:
                    self.done.emit(False, f"Nie udało się podmienić plików: {e}")
                    return

                # 6) Zapisz nową wersję w tym folderze
                try:
                    with open(LOCAL_VERSION_FILE, "w", encoding="utf-8") as f:
                        json.dump({"version": remote_v}, f)
                except Exception:
                    pass

            self.status.emit("Finalizacja…")
            self._emitp(100)
            self.done.emit(True, "updated")

        except Exception as e:
            self.done.emit(False, f"Niespodziewany błąd: {e}")

# ───────────────────────────── GUI: okno ─────────────────────────────────────
def _find_static_dir():
    """
    Zwraca ścieżkę do katalogu 'static' obok updatera lub w trybie dev.
    Kolejność:
      1) <_MEIPASS>/static   (PyInstaller --onefile)
      2) <dir_exe>/static    (obok updater.exe / updater.py)
      3) <repo_root>/static  (gdy uruchamiasz python updater.py)
      4) <cwd>/static
    """
    candidates = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "static"))

    exe_dir = _exe_dir()
    candidates.append(os.path.join(exe_dir, "static"))

    try:
        here = os.path.abspath(os.path.dirname(__file__))
        candidates.append(os.path.join(here, "static"))
    except Exception:
        pass

    candidates.append(os.path.join(os.getcwd(), "static"))

    for p in candidates:
        if p and os.path.isdir(p):
            return p
    return None

class UpdaterWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__(None, Qt.WindowCloseButtonHint | Qt.MSWindowsFixedSizeDialogHint)
        self.setWindowTitle(f"{APP_NAME} – Aktualizator")
        self.setFixedSize(460, 360)

        static_dir = _find_static_dir()
        app_icon_path = os.path.join(static_dir or "", "icon.png") if static_dir else ""
        app_logo_path = os.path.join(static_dir or "", "logo.png") if static_dir else ""

        if os.path.isfile(app_icon_path):
            self.setWindowIcon(QtGui.QIcon(app_icon_path))

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # Logo u góry na środku
        self.logo = QtWidgets.QLabel(self)
        if os.path.isfile(app_logo_path):
            pix = QtGui.QPixmap(app_logo_path)
            self.logo.setPixmap(pix.scaledToWidth(180, Qt.SmoothTransformation))
        else:
            self.logo.setText(APP_NAME)
            self.logo.setStyleSheet("font-size:22px; font-weight:600; color:#ddd;")
        self.logo.setAlignment(Qt.AlignCenter)
        root.addWidget(self.logo)

        # Podpis z małą ikonką
        title_row = QtWidgets.QHBoxLayout()
        title_row.addStretch(1)
        self.icon = QtWidgets.QLabel(self)
        if os.path.isfile(app_icon_path):
            ipix = QtGui.QPixmap(app_icon_path).scaled(20, 20, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.icon.setPixmap(ipix)
        title_row.addWidget(self.icon)
        title_lbl = QtWidgets.QLabel("Aktualizacja Piotrflix", self)
        title_lbl.setStyleSheet("font-size:14px; color:#ddd; margin-left:6px;")
        title_row.addWidget(title_lbl)
        title_row.addStretch(1)
        root.addLayout(title_row)

        # Status
        self.status = QtWidgets.QLabel("Przygotowanie…", self)
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setStyleSheet("font-size:13px; color:#ddd;")
        root.addWidget(self.status)

        # Pasek postępu
        self.bar = QtWidgets.QProgressBar(self)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        root.addWidget(self.bar)

        # Mikro-log (scrollowalny, tylko do podglądu)
        self.log = QtWidgets.QPlainTextEdit(self)
        self.log.setReadOnly(True)
        self.log.setStyleSheet("background:#111; color:#9ad; font-size:12px;")
        self.log.setMaximumHeight(140)
        root.addWidget(self.log, 1)

        # Stopka
        foot = QtWidgets.QHBoxLayout()
        foot.addStretch(1)
        self.close_btn = QtWidgets.QPushButton("Zamknij", self)
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self._quit_now)
        foot.addWidget(self.close_btn)
        root.addLayout(foot)

        # Worker w tle
        self._thread = QtCore.QThread(self)
        self._worker = UpdaterWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.bar.setValue)
        self._worker.status.connect(self.status.setText)
        self._worker.logline.connect(self._log)
        self._worker.done.connect(self._on_done)

    def start(self):
        self._thread.start()

    def _log(self, line: str):
        self.log.appendPlainText(line)

    def _on_done(self, success: bool, msg: str):
        try:
            self._thread.quit()
            self._thread.wait(2000)
        except Exception:
            pass

        if success and msg == "up_to_date":
            self.status.setText("Masz najnowszą wersję. Uruchamiam aplikację…")
            QtCore.QTimer.singleShot(400, self._restart_app_and_exit)
            return

        if success and msg == "updated":
            self.status.setText("Aktualizacja zakończona. Uruchamiam aplikację…")
            QtCore.QTimer.singleShot(400, self._restart_app_and_exit)
            return

        # błąd
        self.status.setText(f"Błąd: {msg}")
        self.close_btn.setEnabled(True)

    def _restart_app_and_exit(self):
        # spróbuj uruchomić główną aplikację i zamknij updater
        try:
            exe = APP_EXE
            inst = INSTALL_DIR

            if not os.path.isfile(exe):
                msg = f"Nie znaleziono pliku wykonywalnego:\n{exe}"
                self._log(msg)
                QtWidgets.QMessageBox.warning(self, "Błąd uruchamiania", msg)
                return self._quit_now()

            QtCore.QThread.msleep(300)  # chwilka na zwolnienie uchwytów

            try:
                flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                subprocess.Popen([exe], cwd=inst, creationflags=flags)  # bez close_fds na Windows
            except Exception as e:
                try:
                    os.startfile(exe)  # fallback
                except Exception as e2:
                    msg = f"Nie udało się uruchomić aplikacji:\n{e}\n{e2}"
                    self._log(msg)
                    QtWidgets.QMessageBox.critical(self, "Błąd uruchamiania", msg)
        finally:
            self._quit_now()

    def _quit_now(self):
        QtWidgets.QApplication.instance().quit()

# ───────────────────────────── MAIN ──────────────────────────────────────────
def main():
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(f"{APP_NAME} Updater")
    app.setOrganizationName(APP_NAME)

    win = UpdaterWindow()
    win.show()
    QtCore.QTimer.singleShot(50, win.start)  # start pracy po pokazaniu okna
    return app.exec()

if __name__ == "__main__":
    sys.exit(main())

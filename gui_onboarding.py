# gui_onboarding.py — PySide6 onboarding dla Piotrflix
# 1 przycisk „Połącz z Plex”, logo, stały rozmiar, nowoczesny styling,
# walidacja pól + komunikaty krok po kroku, poprawiona obsługa ścieżek sieciowych
from __future__ import annotations
import os
import re
from urllib.parse import urlparse, unquote
import sys
import time
import uuid
import webbrowser
import socket
import requests
import warnings
from typing import Optional, List
from xml.etree import ElementTree as ET

from PySide6 import QtCore, QtGui, QtWidgets
from config_store import load_config, save_config

# toleruj self-signed przy /identity
warnings.filterwarnings("ignore", message="Unverified HTTPS request")
DEFAULT_TIMEOUT = 4  # s


# ───────────────────────── utils: ścieżki zasobów ─────────────────────────
def _resource_path(*parts: str) -> str:
    base = getattr(sys, "_MEIPASS", None) or os.path.abspath(os.path.dirname(__file__))
    return os.path.join(base, *parts)


# ───────────────────────── FS path helpers (UNC/SMB) ───────────────────────
_UNC_RE = re.compile(r"^\\\\[^\\\/]+\\[^\\\/]+(?:\\[^\\\/]+)*$")  # \\server\share\folder
# Uwaga: bardzo liberalne dopuszczenie względnych ścieżek odrzucamy – mają zaczynać się od litery dysku/UNC/smb/file.

def normalize_dir_path(p: str) -> str:
    """
    Normalizuje ścieżki:
      - file:///C:/Filmy  -> C:\Filmy   (Windows)
      - smb://server/share/f  -> \\server\share\f  (Windows)
      - usuwa trailing slashe (poza rootem udziału), porządkuje separatory
    Na Linuksie/mac pozostawia 'smb://…' bez zmian (to wskazanie SMB-URI).
    """
    if not p:
        return ""
    s = p.strip()

    # file:// URI
    if s.lower().startswith("file://"):
        url = QtCore.QUrl(s)
        lf = url.toLocalFile()
        if lf:
            s = lf

    # Windows: SMB uri -> UNC
    if os.name == "nt" and s.lower().startswith("smb://"):
        parts = s[6:]  # po 'smb://'
        parts = parts.strip("/")
        if parts:
            segs = [unquote(seg) for seg in parts.split("/")]
            if len(segs) >= 2:
                s = r"\\{0}\{1}".format(segs[0], segs[1])
                if len(segs) > 2:
                    s += "\\" + "\\".join(segs[2:])
            else:
                s = r"\\" + parts.replace("/", "\\")
        else:
            s = r"\\"

    # Porządkowanie separatorów/trailing slashy
    if os.name == "nt":
        s = s.replace("/", "\\")
        if len(s) > 2 and s.endswith("\\"):
            if not _UNC_RE.match(s[:-1]):
                s = s.rstrip("\\")
        s = os.path.normpath(s)
    else:
        if s.lower().startswith("smb://"):
            if len(s) > 6 and s.endswith("/"):
                s = s.rstrip("/")
            return s
        s = s.replace("\\", "/")
        if len(s) > 1 and s.endswith("/"):
            s = s.rstrip("/")
        s = os.path.normpath(s)

    return s


def looks_like_valid_dir(p: str) -> bool:
    """
    Akceptujemy:
      - istniejące katalogi (os.path.isdir)
      - UNC: \\server\share(\sub\sub)*
      - POSIX SMB-URI: smb://server/share(/sub)*
    """
    if not p:
        return False

    try:
        if os.path.isdir(p):
            return True
    except Exception:
        pass

    if os.name == "nt" and _UNC_RE.match(p):
        return True

    if p.lower().startswith("smb://"):
        rest = p[6:]
        if rest and "/" in rest:
            host, share_and_more = rest.split("/", 1)
            if host and share_and_more:
                return True
        return False

    if os.name == "nt" and re.match(r"^[a-zA-Z]:$", p):
        return False

    return False


# ───────────────────────── Plex helpers ─────────────────────────
def _plex_headers(client_id: str, token: Optional[str] = None):
    h = {
        "X-Plex-Product": "Piotrflix",
        "X-Plex-Version": "1.0",
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Platform": "Desktop",
        "Accept": "application/json",
    }
    if token:
        h["X-Plex-Token"] = token
    return h


def _probe_identity(base_url: str, token: Optional[str]) -> bool:
    try:
        url = base_url.rstrip("/") + "/identity"
        headers = {"Accept": "application/json"}
        if token:
            headers["X-Plex-Token"] = token
        r = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT, verify=False)
        return r.status_code == 200
    except Exception:
        return False


def _local_prefixes_guess() -> List[str]:
    guesses = ["192.168.0.", "192.168.1.", "10.0.0."]
    try:
        host = socket.gethostbyname(socket.gethostname())
        parts = host.split(".")
        if len(parts) == 4 and parts[0] in {"10", "172", "192"}:
            guesses.insert(0, ".".join(parts[:3]) + ".")
    except Exception:
        pass
    out, seen = [], set()
    for g in guesses:
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out


def auto_detect_via_account(token: str, prefer_https: bool = True) -> str:
    client_id = str(uuid.uuid4())
    try:
        resp = requests.get(
            "https://plex.tv/api/resources?includeHttps=1",
            headers=_plex_headers(client_id, token),
            timeout=DEFAULT_TIMEOUT + 2,
        )
        resp.raise_for_status()
        xml_root = ET.fromstring(resp.text)

        locals_, remotes_ = [], []
        for dev in xml_root.findall(".//Device"):
            if "server" not in (dev.attrib.get("provides") or ""):
                continue
            for conn in dev.findall("Connection"):
                uri = (conn.attrib.get("uri") or "").rstrip("/")
                if not uri:
                    continue
                is_local = (conn.attrib.get("local") == "1")
                is_https = uri.lower().startswith("https")
                weight = (0 if is_https else 1) if prefer_https else (0 if not is_https else 1)
                (locals_ if is_local else remotes_).append((uri, weight))

        locals_.sort(key=lambda t: t[1])
        remotes_.sort(key=lambda t: t[1])

        for uri, _ in locals_:
            if _probe_identity(uri, token):
                return uri
        for uri, _ in remotes_:
            if _probe_identity(uri, token):
                return uri
        return ""
    except Exception:
        return ""


def auto_detect_lan(token: Optional[str] = None, port: int = 32400, max_hosts_per_prefix: int = 50) -> str:
    for prefix in _local_prefixes_guess():
        typical = [1, 10, 50, 100, 200]
        checked = set()
        for last in typical:
            ip = f"{prefix}{last}"
            if ip in checked:
                continue
            checked.add(ip)
            url = f"http://{ip}:{port}"
            if _probe_identity(url, token):
                return url
        count = 0
        for last in range(2, 255):
            if count >= max_hosts_per_prefix:
                break
            if last in typical:
                continue
            ip = f"{prefix}{last}"
            if ip in checked:
                continue
            checked.add(ip)
            url = f"http://{ip}:{port}"
            if _probe_identity(url, token):
                return url
            count += 1
    return ""


def normalize_plex_base_url(uri: str, force_http: bool = True, default_port: int = 32400) -> str:
    """
    Np. https://192-168-1-224.xyz.plex.direct:32400 -> http://192.168.1.224:32400
    """
    if not uri:
        return ""
    try:
        p = urlparse(uri.strip())
        host = (p.hostname or "").strip()
        port = p.port or default_port

        ip = host
        m = re.match(r"^(\d{1,3}(?:-\d{1,3}){3})\.", host) if host.endswith(".plex.direct") else None
        if m:
            ip = m.group(1).replace("-", ".")
        scheme = "http" if force_http else (p.scheme or "http")
        return f"{scheme}://{ip}:{port}"
    except Exception:
        return uri


# ─────────────── Worker: PIN ➜ autodetect ───────────────
class PlexConnectWorker(QtCore.QObject):
    progress = QtCore.Signal(str)
    partialToken = QtCore.Signal(str)
    partialServer = QtCore.Signal(str)
    success = QtCore.Signal(str, str)  # (base_url, token)
    failed = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._abort = False

    @QtCore.Slot()
    def run(self):
        try:
            # 1) PIN login
            client_id = str(uuid.uuid4())
            self.progress.emit("Pobieram PIN…")
            r = requests.post(
                "https://plex.tv/api/v2/pins",
                headers=_plex_headers(client_id),
                params={"strong": "true"},
                timeout=10,
            )
            r.raise_for_status()
            pin = r.json()
            code = pin["code"]
            pin_id = pin["id"]
            auth_url = f"https://app.plex.tv/auth#?clientID={client_id}&code={code}&context%5Bdevice%5D%5Bproduct%5D=Piotrflix"
            self.progress.emit("Otwieram przeglądarkę do logowania…")
            webbrowser.open(auth_url, new=2, autoraise=True)

            token = ""
            t0 = time.time()
            while (time.time() - t0) < 120 and not self._abort:
                time.sleep(2)
                self.progress.emit("Czekam na autoryzację w Plex…")
                check = requests.get(
                    f"https://plex.tv/api/v2/pins/{pin_id}",
                    headers=_plex_headers(client_id),
                    timeout=10,
                )
                check.raise_for_status()
                data = check.json()
                token = data.get("authToken") or data.get("auth_token")
                if token:
                    self.partialToken.emit(token)
                    self.progress.emit("Token OK. Szukam serwera…")
                    break

            if not token:
                return self.failed.emit("Nie udało się uzyskać tokena (PIN).")
            if self._abort:
                return self.failed.emit("Przerwano.")

            # 2) autodetect
            base_url = auto_detect_via_account(token)
            if not base_url:
                self.progress.emit("Skanuję LAN (ograniczony)…")
                base_url = auto_detect_lan(token)
            if not base_url:
                return self.failed.emit("Nie znaleziono serwera Plex.")

            base_url = normalize_plex_base_url(base_url, force_http=True)
            self.partialServer.emit(base_url)

            # 3) test
            self.progress.emit("Testuję połączenie…")
            if not _probe_identity(base_url, token):
                return self.failed.emit("Serwer nie odpowiada na /identity.")

            self.success.emit(base_url, token)
        except Exception as e:
            self.failed.emit(f"Błąd połączenia: {e}")

    def abort(self):
        self._abort = True


# ─────────────── Główne okno ───────────────
class OnboardingWindow(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        # Ikona okna
        self.setWindowIcon(QtGui.QIcon(_resource_path("static", "icon.png")))

        # Tytuł i stały rozmiar (brak rozciągania/maksymalizacji)
        self.setWindowTitle("Piotrflix – Onboarding")
        self.setFixedSize(860, 600)
        self.setWindowFlag(QtCore.Qt.MSWindowsFixedSizeDialogHint, True)

        # Styl nowoczesny (QSS)
        self._apply_styles()

        self.cfg = load_config()

        # wątki
        self._cthread: QtCore.QThread | None = None
        self._cworker: PlexConnectWorker | None = None

        # layout główny
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 12)
        root.setSpacing(16)

        # ── LOGO (wyśrodkowane) ──
        logo_path = _resource_path("static", "logo.png")
        logo_lbl = QtWidgets.QLabel(objectName="Logo")
        logo_lbl.setAlignment(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter)
        pix = QtGui.QPixmap(logo_path)
        if not pix.isNull():
            scaled = pix.scaledToWidth(240, QtCore.Qt.SmoothTransformation)
            logo_lbl.setPixmap(scaled)
        else:
            logo_lbl.setText("Piotrflix")
            logo_lbl.setFont(QtGui.QFont("Segoe UI", 20, QtGui.QFont.Bold))
        root.addWidget(logo_lbl)

        # karta z formularzem
        card = QtWidgets.QFrame(objectName="Card")
        card.setLayout(QtWidgets.QVBoxLayout())
        card.layout().setContentsMargins(20, 16, 20, 12)
        card.layout().setSpacing(14)
        root.addWidget(card, 1)

        # FORM
        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignLeft)
        form.setFormAlignment(QtCore.Qt.AlignTop)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)
        card.layout().addLayout(form)

        # foldery z miejscem na etykiety błędów
        self.movies_edit = QtWidgets.QLineEdit(normalize_dir_path(self.cfg["paths"].get("movies", "")), objectName="LineEdit")
        self.movies_err = QtWidgets.QLabel("", objectName="ErrLabel")
        btn_movies = QtWidgets.QPushButton("Wybierz…", objectName="BtnSecondary")
        btn_movies.clicked.connect(self._pick_movies)
        form.addRow("Folder z filmami:", self._with_error(self.movies_edit, btn_movies, self.movies_err))

        self.series_edit = QtWidgets.QLineEdit(normalize_dir_path(self.cfg["paths"].get("series", "")), objectName="LineEdit")
        self.series_err = QtWidgets.QLabel("", objectName="ErrLabel")
        btn_series = QtWidgets.QPushButton("Wybierz…", objectName="BtnSecondary")
        btn_series.clicked.connect(self._pick_series)
        form.addRow("Folder z serialami:", self._with_error(self.series_edit, btn_series, self.series_err))

        # normalizacja po edycji ręcznej
        self.movies_edit.editingFinished.connect(lambda: self._normalize_and_validate(self.movies_edit, self.movies_err))
        self.series_edit.editingFinished.connect(lambda: self._normalize_and_validate(self.series_edit, self.series_err))

        # „Połącz z Plex”
        self.btn_connect = QtWidgets.QPushButton("Połącz z Plex", objectName="BtnPrimary")
        self.btn_connect.setMinimumHeight(40)
        self.btn_connect.clicked.connect(self._start_connect)
        form.addRow(" ", self.btn_connect)

        # statusy (badges)
        self.token_badge = QtWidgets.QLabel("Token: —", objectName="BadgeGray")
        self.server_badge = QtWidgets.QLabel("Serwer: —", objectName="BadgeGray")
        badges = QtWidgets.QHBoxLayout()
        badges.setSpacing(10)
        badges.addWidget(self.token_badge)
        badges.addWidget(self.server_badge)
        badges.addStretch(1)
        badges_w = QtWidgets.QWidget()
        badges_w.setLayout(badges)
        form.addRow("Status:", badges_w)

        # pola readonly
        self.plex_url_edit = QtWidgets.QLineEdit(self.cfg["plex"].get("base_url", ""), objectName="LineReadOnly")
        self.plex_url_edit.setReadOnly(True)
        form.addRow("Adres serwera Plex:", self.plex_url_edit)

        self.token_edit = QtWidgets.QLineEdit(self.cfg["plex"].get("token", ""), objectName="LineReadOnly")
        self.token_edit.setReadOnly(True)
        self.token_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        form.addRow("Plex Token:", self.token_edit)

        # status tekstowy – wyraźny komunikat krok po kroku
        self.status_label = QtWidgets.QLabel("", objectName="StatusText")
        card.layout().addWidget(self.status_label)

        # przyciski dół
        actions = QtWidgets.QHBoxLayout()
        actions.addStretch(1)
        self.btn_save = QtWidgets.QPushButton("Zapisz i uruchom", objectName="BtnPrimary")
        self.btn_save.setMinimumHeight(40)
        self.btn_save.setDefault(True)
        self.btn_save.clicked.connect(self._save)
        actions.addWidget(self.btn_save)
        btn_cancel = QtWidgets.QPushButton("Anuluj", objectName="BtnGhost")
        btn_cancel.clicked.connect(self._cancel)
        actions.addWidget(btn_cancel)
        card.layout().addLayout(actions)

        # stopka
        footer = QtWidgets.QLabel("© 2025 Piotr Kawa", objectName="Footer")
        footer.setAlignment(QtCore.Qt.AlignHCenter)
        root.addWidget(footer)

        # status wg configu
        if self.token_edit.text().strip():
            self._set_badge(self.token_badge, ok=True, text="Token: odebrano ✓")
        if self.plex_url_edit.text().strip():
            self._set_badge(self.server_badge, ok=True, text="Serwer: znaleziono ✓")

        # live-walidacja: czyszczenie błędów przy edycji
        self.movies_edit.textChanged.connect(lambda: self._clear_error(self.movies_edit, self.movies_err))
        self.series_edit.textChanged.connect(lambda: self._clear_error(self.series_edit, self.series_err))

        # pokaż, co jeszcze trzeba zrobić na start
        self._update_next_steps()

    # ── Styling (QSS) ─────────────────────────────────────────────
    def _apply_styles(self):
        self.setStyleSheet("""
            QWidget { color: #EAEAEA; font: 13px "Segoe UI", Arial, sans-serif; }
            #Card {
                background: #1f1f24;
                border: 1px solid #2b2b32;
                border-radius: 14px;
            }
            #Footer { color: #8b8b93; padding: 4px 0 0 0; font-size: 12px; }
            #StatusText { color: #d7dde3; padding: 8px 2px 2px 2px; font-size: 14px; }

            /* inputs */
            QLineEdit#LineEdit, QLineEdit#LineReadOnly {
                background: #141419;
                border: 1px solid #2a2a31;
                border-radius: 10px;
                padding: 8px 10px;
                selection-background-color: #4ca3e0;
            }
            QLineEdit#LineEdit:hover { border-color: #3a3a45; }
            QLineEdit#LineReadOnly { color: #b9c0c7; background: #121217; }

            /* błędy */
            QLineEdit[error="true"] {
                border: 1px solid #e74c3c;
                background: #181317;
            }
            QLabel#ErrLabel {
                color: #ff7d7d;
                font-size: 11px;
                padding: 3px 2px 0 2px;
            }

            /* buttons */
            QPushButton#BtnPrimary {
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #4aa3ff, stop:1 #2a7bd6);
                border: 0px; border-radius: 10px; color: white; padding: 9px 16px; font-weight: 600;
            }
            QPushButton#BtnPrimary:hover {
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #59adff, stop:1 #3187e6);
            }
            QPushButton#BtnPrimary:pressed { background: #256cc4; }

            QPushButton#BtnSecondary {
                background: #2a2a32; color: #eaeaea;
                border: 1px solid #3a3a44; border-radius: 8px; padding: 7px 12px;
            }
            QPushButton#BtnSecondary:hover { background: #32323a; }
            QPushButton#BtnSecondary:pressed { background: #2b2b33; }

            QPushButton#BtnGhost {
                background: transparent; color: #c7c7cf;
                border: 1px solid #3a3a44; border-radius: 10px; padding: 9px 16px;
            }
            QPushButton#BtnGhost:hover { background: #23232a; }
            QPushButton#BtnGhost:pressed { background: #1d1d23; }

            /* badges */
            QLabel#BadgeGray, QLabel#BadgeGreen, QLabel#BadgeRed {
                padding: 6px 10px; border-radius: 999px; font-weight: 700;
            }
            QLabel#BadgeGray { background: #2a2a32; color: #b9c0c7; border: 1px solid #34343d; }
            QLabel#BadgeGreen { background: #173d2a; color: #2ecc71; border: 1px solid #2a6e49; }
            QLabel#BadgeRed { background: #3d1b1b; color: #ff6b6b; border: 1px solid #6e2a2a; }
        """)

    # ── helpers UI ─────────────────────────────────────────────
    def _with_error(self, edit: QtWidgets.QLineEdit, btn: QtWidgets.QPushButton, err_label: QtWidgets.QLabel) -> QtWidgets.QWidget:
        row = QtWidgets.QVBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        h = QtWidgets.QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        h.addWidget(edit)
        h.addWidget(btn)
        row.addLayout(h)
        row.addWidget(err_label)
        w = QtWidgets.QWidget()
        w.setLayout(row)
        return w

    def _normalize_and_validate(self, edit: QtWidgets.QLineEdit, err_label: QtWidgets.QLabel):
        raw = edit.text()
        norm = normalize_dir_path(raw)
        if norm != raw:
            edit.setText(norm)
        if not norm or not looks_like_valid_dir(norm):
            self._mark_error(edit, err_label, "nieprawidłowa ścieżka (lokalna/UNC/SMB)")
        else:
            self._clear_error(edit, err_label)
        self._update_next_steps()

    def _pick_movies(self):
        start = self.movies_edit.text().strip() or QtCore.QDir.homePath()
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Wybierz folder z filmami", start,
            QtWidgets.QFileDialog.ShowDirsOnly
        )
        if path:
            path = normalize_dir_path(path)
            self.movies_edit.setText(path)
            self._clear_error(self.movies_edit, self.movies_err)
            self._update_next_steps()

    def _pick_series(self):
        start = self.series_edit.text().strip() or QtCore.QDir.homePath()
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Wybierz folder z serialami", start,
            QtWidgets.QFileDialog.ShowDirsOnly
        )
        if path:
            path = normalize_dir_path(path)
            self.series_edit.setText(path)
            self._clear_error(self.series_edit, self.series_err)
            self._update_next_steps()

    def _set_status(self, msg: str):
        self.status_label.setText(msg)
        self.status_label.repaint()

    def _set_badge(self, label: QtWidgets.QLabel, ok: bool, text: Optional[str] = None):
        if text is not None:
            label.setText(text)
        label.setObjectName("BadgeGreen" if ok else "BadgeRed")
        label.style().unpolish(label)
        label.style().polish(label)
        label.update()

    def _mark_error(self, edit: QtWidgets.QLineEdit, err_label: QtWidgets.QLabel, text: str):
        edit.setProperty("error", True)
        edit.style().unpolish(edit); edit.style().polish(edit); edit.update()
        err_label.setText(text)

    def _clear_error(self, edit: QtWidgets.QLineEdit, err_label: QtWidgets.QLabel):
        if edit.property("error"):
            edit.setProperty("error", False)
            edit.style().unpolish(edit); edit.style().polish(edit); edit.update()
        if err_label.text():
            err_label.setText("")

    def _validate_required(self) -> bool:
        ok = True

        m = self.movies_edit.text().strip()
        s = self.series_edit.text().strip()

        if not m or not looks_like_valid_dir(m):
            self._mark_error(self.movies_edit, self.movies_err, "nieprawidłowa ścieżka (lokalna/UNC/SMB)")
            ok = False
        if not s or not looks_like_valid_dir(s):
            self._mark_error(self.series_edit, self.series_err, "nieprawidłowa ścieżka (lokalna/UNC/SMB)")
            ok = False
        if not self.token_edit.text().strip():
            self._set_badge(self.token_badge, ok=False, text="Token: brak ✗")
            ok = False
        if not self.plex_url_edit.text().strip():
            self._set_badge(self.server_badge, ok=False, text="Serwer: nie znaleziono ✗")
            ok = False
        return ok

    def _update_next_steps(self):
        missing = []
        if not self.token_edit.text().strip() or not self.plex_url_edit.text().strip():
            missing.append("Połącz z Plex")
        if not self.movies_edit.text().strip():
            missing.append("Uzupełnij folder „Filmy”")
        if not self.series_edit.text().strip():
            missing.append("Uzupełnij folder „Seriale”")

        if not missing:
            self._set_status("Wszystko gotowe ✅ – możesz zapisać konfigurację.")
        else:
            self._set_status("Kroki do wykonania: " + " • ".join(missing))

    # ── połącz z Plex ──────────────────────────────────────────
    def _start_connect(self):
        if self._cthread is not None:
            return
        self.btn_connect.setEnabled(False)
        self._set_status("Uruchamiam połączenie z Plex…")
        # reset badge na szaro
        self.token_badge.setObjectName("BadgeGray"); self.token_badge.setText("Token: —")
        self.server_badge.setObjectName("BadgeGray"); self.server_badge.setText("Serwer: —")
        for b in (self.token_badge, self.server_badge):
            b.style().unpolish(b); b.style().polish(b); b.update()

        self._cthread = QtCore.QThread(self)
        self._cworker = PlexConnectWorker()
        self._cworker.moveToThread(self._cthread)

        self._cworker.progress.connect(self._set_status)
        self._cworker.partialToken.connect(self._on_token_partial)
        self._cworker.partialServer.connect(self._on_server_partial)
        self._cworker.success.connect(self._on_connect_success)
        self._cworker.failed.connect(self._on_connect_failed)

        self._cthread.started.connect(self._cworker.run)
        self._cthread.finished.connect(self._cleanup_connect_thread)
        self._cthread.start()

    def _on_token_partial(self, token: str):
        self.token_edit.setText(token)
        self._set_badge(self.token_badge, ok=True, text="Token: odebrano ✓")

    def _on_server_partial(self, base_url: str):
        self.plex_url_edit.setText(base_url)
        self._set_badge(self.server_badge, ok=True, text="Serwer: znaleziono ✓")

    def _on_connect_success(self, base_url: str, token: str):
        self.plex_url_edit.setText(base_url)
        self.token_edit.setText(token)
        self._set_badge(self.token_badge, ok=True, text="Token: odebrano ✓")
        self._set_badge(self.server_badge, ok=True, text="Serwer: znaleziono ✓")
        self._set_status("Połączono z Plex ✅. Uzupełnij foldery „Filmy” i „Seriale”, a następnie kliknij „Zapisz i uruchom”.")
        QtWidgets.QMessageBox.information(self, "Plex", "Połączono z Plex ✅")
        self._cthread.quit()
        self._update_next_steps()

    def _on_connect_failed(self, msg: str):
        if not self.token_edit.text().strip():
            self._set_badge(self.token_badge, ok=False, text="Token: brak ✗")
        if not self.plex_url_edit.text().strip():
            self._set_badge(self.server_badge, ok=False, text="Serwer: nie znaleziono ✗")
        self._set_status(msg)
        QtWidgets.QMessageBox.warning(self, "Plex", msg)
        self._cthread.quit()
        self._update_next_steps()

    def _cleanup_connect_thread(self):
        self.btn_connect.setEnabled(True)
        if self._cworker:
            self._cworker.deleteLater()
        self._cworker = None
        if self._cthread:
            self._cthread.deleteLater()
        self._cthread = None

    # ── zapis ──────────────────────────────────────────────────
    def _save(self):
        # normalizacja na wszelki wypadek
        self.movies_edit.setText(normalize_dir_path(self.movies_edit.text().strip()))
        self.series_edit.setText(normalize_dir_path(self.series_edit.text().strip()))

        if not self._validate_required():
            self._update_next_steps()
            return

        cfg = {
            "paths": {
                "movies": self.movies_edit.text().strip(),
                "series": self.series_edit.text().strip()
            },
            "plex": {
                "base_url": self.plex_url_edit.text().strip(),
                "token": self.token_edit.text().strip()
            }
        }
        try:
            save_config(cfg)
            QtWidgets.QMessageBox.information(self, "OK", "Zapisano konfigurację ✅")
            self.result = cfg
            self.close()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Błąd", f"Nie udało się zapisać config.json: {e}")

    def _cancel(self):
        self.result = {}
        self.close()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self._cworker and self._cthread:
            self._cworker.abort()
            self._cthread.quit()
            self._cthread.wait(1000)
        super().closeEvent(event)


# ─────────────── API dla app.py ───────────────
def run_onboarding() -> dict:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

    # Ciemny motyw (spójny z QSS)
    app.setStyle("Fusion")
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.Window, QtGui.QColor(26, 26, 31))
    palette.setColor(QtGui.QPalette.WindowText, QtCore.Qt.white)
    palette.setColor(QtGui.QPalette.Base, QtGui.QColor(20, 20, 25))
    palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(32, 32, 40))
    palette.setColor(QtGui.QPalette.Text, QtCore.Qt.white)
    palette.setColor(QtGui.QPalette.Button, QtGui.QColor(40, 40, 48))
    palette.setColor(QtGui.QPalette.ButtonText, QtCore.Qt.white)
    palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor(76, 163, 224))
    palette.setColor(QtGui.QPalette.HighlightedText, QtCore.Qt.black)
    app.setPalette(palette)

    win = OnboardingWindow()
    win.result = {}

    # wyśrodkuj
    screen = app.primaryScreen().availableGeometry()
    size = win.frameGeometry()
    size.moveCenter(screen.center())
    win.move(size.topLeft())
    win.show()

    app.exec()
    return getattr(win, "result", {})


if __name__ == "__main__":
    print(run_onboarding())

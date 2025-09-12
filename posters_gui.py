# posters_gui.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import os, hashlib, io, weakref, threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple
import requests
import sys
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from urllib.parse import urlparse


def _normalize_url(url: str) -> str:
    u = (url or "").strip().strip('"').strip("'")
    if u.startswith("//"):
        return "http:" + u
    return u


def _guess_referer(u: str) -> str | None:
    try:
        p = urlparse(u)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}/"
    except Exception:
        pass
    return None


def _default_cache_dir(app_name: str = "Piotrflix") -> str:
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    elif sys.platform == "darwin":
        root = os.path.expanduser("~/Library/Caches")
    else:
        root = os.path.expanduser("~/.cache")
    d = os.path.join(root, app_name, "posters")
    os.makedirs(d, exist_ok=True)
    return d


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()


def _rounded_pixmap(src: QtGui.QPixmap, radius: int) -> QtGui.QPixmap:
    if src.isNull():
        return src
    w, h = src.width(), src.height()
    target = QtGui.QPixmap(w, h)
    target.fill(Qt.transparent)
    p = QtGui.QPainter(target)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    path = QtGui.QPainterPath()
    path.addRoundedRect(0, 0, w, h, radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, src)
    p.end()
    return target


def _scale_for_label(pix: QtGui.QPixmap, label: QtWidgets.QLabel) -> QtGui.QPixmap:
    if pix.isNull():
        return pix
    size = label.size()
    return pix.scaled(size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)


def _placeholder(size: Tuple[int, int]) -> QtGui.QPixmap:
    w, h = size
    pm = QtGui.QPixmap(w, h)
    pm.fill(Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    # delikatny placeholder
    grad = QtGui.QLinearGradient(0, 0, 0, h)
    grad.setColorAt(0.0, QtGui.QColor(26, 28, 34))
    grad.setColorAt(1.0, QtGui.QColor(16, 18, 24))
    p.fillRect(0, 0, w, h, grad)
    pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 28))
    pen.setWidth(1)
    p.setPen(pen)
    p.drawRoundedRect(0, 0, w - 1, h - 1, 12, 12)
    # ikonka
    pen2 = QtGui.QPen(QtGui.QColor(170, 180, 210, 110))
    pen2.setWidth(2)
    p.setPen(pen2)
    p.drawRect(w * 0.25, h * 0.28, w * 0.5, h * 0.36)
    p.drawLine(w * 0.28, h * 0.52, w * 0.46, h * 0.38)
    p.drawLine(w * 0.46, h * 0.38, w * 0.70, h * 0.60)
    p.end()
    return pm




class PosterManager(QtCore.QObject):
    """Asynchroniczny loader okładek z cache’em (dysk + RAM).
    Użycie:
        label = posters.create_label(86, 129)
        posters.attach(label, url, radius=12)
    """
    # sygnał: label, gotowy QPixmap
    _sig_ready = QtCore.Signal(object, QtGui.QPixmap)

    def __init__(self, cache_dir: Optional[str] = None, max_workers: int = 8,
                 session: Optional[requests.Session] = None):
        super().__init__()
        self._cache_dir = cache_dir or _default_cache_dir()
        self._ram_cache: "weakref.WeakValueDictionary[str, QtGui.QPixmap]" = weakref.WeakValueDictionary()
        self._exec = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="poster")

        self._session = session or requests.Session()
        # Domyślne nagłówki – pomagają na 403/anti-bot
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        })

        # Retry na „miękkie” błędy i rate limiting
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.5,  # 0.5s, 1s, 2s...
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

        self._sig_ready.connect(self._on_ready_main)
        self._lock = threading.Lock()
        self._pending: set[str] = set()

    # ————— API —————
    def create_label(self, w: int = 86, h: int = 129, radius: int = 12) -> QtWidgets.QLabel:
        lab = QtWidgets.QLabel()
        lab.setFixedSize(w, h)
        lab.setScaledContents(False)
        lab.setAlignment(Qt.AlignCenter)
        lab.setProperty("_poster_radius", int(radius))
        lab.setPixmap(_placeholder((w, h)))
        return lab

    def attach(self, label: QtWidgets.QLabel, image_url: Optional[str], radius: Optional[int] = None):
        if not image_url:
            # zostaw placeholder
            return
        url = str(image_url).strip()
        if not url:
            return

        rad = radius if radius is not None else int(label.property("_poster_radius") or 12)
        key = _sha1(url) + f"_{label.width()}x{label.height()}_r{rad}"

        # RAM cache
        pm = self._ram_cache.get(key)
        if pm is not None and not pm.isNull():
            label.setPixmap(pm)
            return

        # Dysk cache (już przeskalowany wariant)
        disk_fp = os.path.join(self._cache_dir, f"{key}.qpm")
        if os.path.isfile(disk_fp):
            try:
                b = QtCore.QByteArray()
                with open(disk_fp, "rb") as f:
                    b.append(f.read())
                pm = QtGui.QPixmap()
                pm.loadFromData(b)
                if not pm.isNull():
                    self._ram_cache[key] = pm
                    label.setPixmap(pm)
                    return
            except Exception:
                pass

        # W tle pobierz/skaluj/zapamiętaj
        with self._lock:
            if key in self._pending:
                return
            self._pending.add(key)

        ph = _placeholder((label.width(), label.height()))
        label.setPixmap(ph)

        def _task():
            try:
                # 1) pobierz bytes (albo z dysku surowe?)
                raw = self._download_raw(url)
                if raw is None:
                    return None
                # 2) pixmap → skaluj → zaokrąglij
                pix = QtGui.QPixmap()
                pix.loadFromData(raw)
                if pix.isNull():
                    return None
                pix2 = _scale_for_label(pix, label)
                pix2 = _rounded_pixmap(pix2, rad)
                # 3) do RAM cache
                self._ram_cache[key] = pix2
                # 4) na dysk (zapisz QPixmap do PNG w kontenerze)
                try:
                    ba = QtCore.QByteArray()
                    buf = QtCore.QBuffer(ba); buf.open(QtCore.QIODevice.WriteOnly)
                    pix2.save(buf, "PNG", quality=85)
                    with open(disk_fp, "wb") as f:
                        f.write(bytes(ba))
                except Exception:
                    pass
                return (label, pix2)
            finally:
                with self._lock:
                    self._pending.discard(key)

        fut = self._exec.submit(_task)
        fut.add_done_callback(lambda f: self._emit_if_ready(f))

    # ————— wewnętrzne —————
    def _emit_if_ready(self, future):
        try:
            res = future.result()
            if not res:
                return
            label, pix = res
            self._sig_ready.emit(label, pix)
        except Exception:
            pass

    @QtCore.Slot(object, QtGui.QPixmap)
    def _on_ready_main(self, label: QtWidgets.QLabel, pix: QtGui.QPixmap):
        if label is None or pix is None:
            return
        if not isinstance(label, QtWidgets.QLabel):
            return
        label.setPixmap(pix)

    def _download_raw(self, url: str) -> Optional[bytes]:
        try:
            u = _normalize_url(url)

            # 0) Lokalny plik (ścieżka absolutna lub file://)
            path = None
            if isinstance(u, str) and u.startswith("file://"):
                path = u[7:]
            elif isinstance(u, str) and (
                    os.path.isabs(u) or os.path.sep in u or (os.name == "nt" and ":" in u[:3])
            ):
                path = u

            if path:
                path = os.path.expanduser(path)
                if os.path.isfile(path):
                    try:
                        with open(path, "rb") as f:
                            return f.read()
                    except Exception:
                        pass  # spróbujemy niżej jako URL

            # 1) Cache surowych bajtów
            raw_fp = os.path.join(self._cache_dir, _sha1("raw_" + u) + ".bin")
            if os.path.isfile(raw_fp):
                try:
                    with open(raw_fp, "rb") as f:
                        return f.read()
                except Exception:
                    pass

            # 2) HTTP(S) z większym timeoutem + Referer (jeśli możliwy)
            headers = {}
            ref = _guess_referer(u)
            if ref:
                headers["Referer"] = ref

            # oddzielny connect/read timeout pomaga przy wolnych serwerach
            r = self._session.get(u, timeout=(5, 15), headers=headers, allow_redirects=True)
            if r.status_code == 200 and r.content:
                try:
                    with open(raw_fp, "wb") as f:
                        f.write(r.content)
                except Exception:
                    pass
                return r.content

            # nieudane – zostaw None
        except Exception:
            pass
        return None


# ——— singleton wygodny do importu ———
# (możesz też samemu stworzyć instancję i przekazać do stron)
poster = PosterManager()

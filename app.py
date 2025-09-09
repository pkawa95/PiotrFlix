# -*- coding: utf-8 -*-
from __future__ import annotations
from bs4 import BeautifulSoup
import atexit
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Optional, Dict, List, Set
from selenium.webdriver.common.by import By
import requests
from flask import Flask, render_template, request, redirect, url_for, jsonify
from plexapi.server import PlexServer
from selenium.webdriver.chrome.options import Options
from selenium import webdriver
# Twój klient libtorrent + historia
from torrent_client import TorrentClient, HistoryStore
from flask import Flask, request, jsonify
from flask import send_from_directory
from config_store import load_config, save_config, config_exists

DEFER_INIT = os.environ.get("PFLIX_DEFER_INIT") == "1"

# ─────────────────────────────────────────────────────────────────────────────
# Ścieżki i podstawy
# ─────────────────────────────────────────────────────────────────────────────
# ── Trwały katalog danych użytkownika (działa też w PyInstaller) ────────────
APP_NAME = "Piotrflix"

def _user_state_dir(app_name: str = APP_NAME) -> str:
    if sys.platform.startswith("win"):
        root = os.environ.get("APPDATA") or os.path.expanduser(r"~\\AppData\\Roaming")
    elif sys.platform == "darwin":
        root = os.path.expanduser("~/Library/Application Support")
    else:
        root = os.path.expanduser("~/.local/share")
    return os.path.join(root, app_name, "state")

STATE_DIR = _user_state_dir()
os.makedirs(STATE_DIR, exist_ok=True)

# ── LOGGING (progress cache) ────────────────────────────────────────────────
import logging
from logging.handlers import RotatingFileHandler

LOG_FILE = os.path.join(STATE_DIR, "progress_cache.log")

progress_log = logging.getLogger("progress-cache")
progress_log.setLevel(logging.DEBUG)

# uniknij podwójnych handlerów przy restarcie modułu
if not progress_log.handlers:
    _h = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    progress_log.addHandler(_h)
    _c = logging.StreamHandler()
    _c.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    progress_log.addHandler(_c)

progress_log.info("Logger ready. Log file: %s", LOG_FILE)


# Plik z postępami w trwałej lokalizacji
PROGRESS_CACHE_FILE = os.path.join(STATE_DIR, "progress_cache.json")

# (opcjonalna migracja) – jeśli kiedyś był obok exe/źródeł, przenieś go 1x
_old_base = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.abspath(".")
_OLD_PROGRESS = os.path.join(_old_base, "progress_cache.json")
if os.path.exists(_OLD_PROGRESS) and not os.path.exists(PROGRESS_CACHE_FILE):
    try:
        os.replace(_OLD_PROGRESS, PROGRESS_CACHE_FILE)
    except Exception:
        pass

# Utwórz pusty cache, jeśli brak
if not os.path.exists(PROGRESS_CACHE_FILE):
    with open(PROGRESS_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f, ensure_ascii=False)


if getattr(sys, "frozen", False):
    base_path = sys._MEIPASS
else:
    base_path = os.path.abspath(".")


tclient = TorrentClient.get()
POSTER_DIR = os.path.join(STATE_DIR, "posters")
POSTER_CACHE_FILE = os.path.join(STATE_DIR, "poster_cache.json")
os.makedirs(POSTER_DIR, exist_ok=True)
AVAILABLE_CACHE_FILE = os.path.join(base_path, "available_cache.json")
HISTORY_FILE = os.path.join(base_path, "torrent_history.json")
GC_MIN_AGE = 24 * 3600  # 24h

os.makedirs(POSTER_DIR, exist_ok=True)
if not os.path.exists(POSTER_CACHE_FILE):
    with open(POSTER_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f)

app = Flask(
    __name__,
    template_folder=os.path.join(base_path, "templates"),
    static_folder=os.path.join(base_path, "static"),
)


# --- PROGRESS CACHE: helpers -------------------------------------------------
PROGRESS_LOCK = threading.Lock()
# ── Koordynacja zamykania ───────────────────────────────────────────────────
SHUTDOWN_EVENT = threading.Event()
_SHUTDOWN_ONCE = threading.Event()
_BG_THREAD_NAMES = {
    "complete-watchdog",
    "available-watchdog",
    "progress-cache-watchdog",
    "cleanup-loop",
    "poster-sweeper",
    "post-finish-preload",  # prefiks – patrz helper ze startem
}

# pojedynczość graceful
_GRACEFUL_LOCK = threading.Lock()
_GRACEFUL_DONE = False

def _start_all_backgrounds():
    start_completion_watchdog(3)
    _start_available_watchdog()
    start_progress_cache_watchdog(10)
    run_cleanup_loop(10)
    start_poster_sweeper(every_minutes=120, rebuild_before=False, force=False)  # ⬅️ TU

def start_flask_blocking(host="127.0.0.1", port=5000):
    """Uruchamia serwer Flask w trybie blokującym (do odpalenia w wątku przez GUI)."""
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)

def init_backend_after_splash():
    """
    Wołane przez GUI PO wyświetleniu splash’a:
    - robi preload available_cache (plakaty itd.),
    - startuje wszystkie watchdogy i pętle w tle.
    """
    _do_available_bootstrap()
    _start_all_backgrounds()

def _do_available_bootstrap():
    print("🚀 Preload plakatów i cache dostępnych tytułów…")
    try:
        available_cache.rebuild_now()
        print("✅ Preload OK")
    except Exception as e:
        print("⚠️ Preload nie w pełni się udał:", e)

    try:
        progress_log.info("Available watchdog uruchomiony w tle")
    except Exception as e:
        try:
            progress_log.warning("Available watchdog init error: %s", e)
        except Exception:
            print("⚠️ Available watchdog init error:", e)


def _progress_load() -> dict:
    try:
        with open(PROGRESS_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _progress_save(data: dict):
    try:
        tmp = PROGRESS_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, PROGRESS_CACHE_FILE)
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# Konfiguracja zewnętrzna
# ─────────────────────────────────────────────────────────────────────────────
TMDB_API_KEY = "5471a26860fd4401b09ebf325ab6b4fb"
TMDB_IMG = "https://image.tmdb.org/t/p"
POSTER_SIZE = "w342"

CONFIG = load_config()
PLEX_URL = CONFIG["plex"]["base_url"]
TOKEN = CONFIG["plex"]["token"]

MOVIES_DIR = CONFIG["paths"]["movies"]
SERIES_DIR = CONFIG["paths"]["series"]

# ─────────────────────────────────────────────────────────────────────────────
# Historia + klient torrentów + watchdog ukończeń
# ─────────────────────────────────────────────────────────────────────────────
history_store = HistoryStore(HISTORY_FILE)
tclient.set_history_store(history_store)

_completed_ids_lock = threading.Lock()
_completed_finished_ids = set(e["id"] for e in history_store.get() if e.get("event") == "finished")


@app.route('/static/posters/<path:filename>')
def serve_posters(filename):
    return send_from_directory(POSTER_DIR, filename)

# --- Bezpieczne pobranie PlexServer albo None -------------------------------
def get_plex_or_none():
    """
    Zwraca obiekt PlexServer jeśli URL/token są poprawne i serwer odpowiada na /identity.
    W przeciwnym razie None (bez rzucania wyjątku).
    """
    try:
        if not PLEX_URL or not TOKEN:
            return None
        plex = PlexServer(PLEX_URL, TOKEN)
        # szybki ping – jeśli nie odpowie, traktuj jako offline
        try:
            plex.query("/identity", timeout=5)
        except Exception:
            return None
        return plex
    except Exception:
        return None


def _mark_finished_once(tid: str, name: str, path: str) -> bool:
    with _completed_ids_lock:
        if tid in _completed_finished_ids:
            return False
        history_store.add(
            {"ts": int(time.time()), "id": tid, "name": name, "path": path, "event": "finished"}
        )
        _completed_finished_ids.add(tid)
        return True


def start_completion_watchdog(interval_seconds: int = 3):
    def loop():
        print("🚀 [COMPLETE-WATCHDOG] start")
        while not SHUTDOWN_EVENT.is_set():
            try:
                torrents = tclient.get_torrents()
                for tid, t in torrents.items():
                    if float(t.progress) >= 100.0:
                        if t.state != "Paused":
                            try:
                                tclient.pause(tid)
                                print(f"⏸️  [WATCHDOG] auto-pauza: {t.name} ({tid})")
                            except Exception as e:
                                print(f"⚠️  [WATCHDOG] pauza nieudana {tid}: {e}")
                        if _mark_finished_once(tid, t.name, t.download_location):
                            trigger_postfinish_preload(t.name, t.download_location)
            except Exception as e:
                print(f"⚠️  [WATCHDOG] błąd: {e}")
            SHUTDOWN_EVENT.wait(interval_seconds)

    threading.Thread(target=loop, daemon=True, name="complete-watchdog").start()





# ─────────────────────────────────────────────────────────────────────────────
# Narzędzia ścieżek
# ─────────────────────────────────────────────────────────────────────────────
def fix_windows_path(path: str) -> str:
    if path.startswith(("/mch/", "/mnt/", "/media/")):
        fixed = path.replace("/", "\\")
        fixed = fixed.replace("\\mch\\", "Z:\\").replace("\\mnt\\", "Z:\\").replace("\\media\\", "Z:\\")
        return fixed
    return path


def map_network_drive():
    if os.name != "nt":
        return
    try:
        # nie używamy text=True -> dostaniemy bytes
        result = subprocess.run(
            ["net", "use", "Z:", r"\\MYCLOUD-00A2RY\kawjorek", "/persistent:no"],
            shell=True,
            capture_output=True
        )
        # net.exe zwykle używa OEM (cp852); użyj 'ignore' żeby nie wysypywać się na rzadkich znakach
        out = (result.stdout or b"").decode("cp852", errors="ignore").strip()
        err = (result.stderr or b"").decode("cp852", errors="ignore").strip()
        msg = out or err or f"exit={result.returncode}"
        print(f"🔌 Mapowanie dysku Z: – {msg}")
    except Exception as e:
        print(f"❌ Błąd mapowania dysku: {e}")




# ─────────────────────────────────────────────────────────────────────────────
# Plakaty – spójny system (PosterManager)
# ─────────────────────────────────────────────────────────────────────────────
def _normalize(title: str) -> str:
    return " ".join((title or "").strip().lower().split())


def _sha_name(type_: str, title: str) -> str:
    return hashlib.sha1(f"{type_.lower()}::{_normalize(title)}".encode("utf-8")).hexdigest() + ".jpg"


def _tmdb_find_poster_url(title: str, type_: str) -> Optional[str]:
    try:
        endpoint = "movie" if type_ == "movie" else "tv"
        r = requests.get(
            f"https://api.themoviedb.org/3/search/{endpoint}",
            params={"api_key": TMDB_API_KEY, "query": title, "language": "pl-PL"},
            timeout=6,
        )
        data = r.json()
        if data.get("results"):
            poster_path = data["results"][0].get("poster_path")
            if poster_path:
                return f"{TMDB_IMG}/{POSTER_SIZE}{poster_path}"
    except Exception:
        pass
    return None


class PosterManager:
    def __init__(self, poster_dir: str, cache_file: str):
        self.dir = poster_dir
        self.cache_file = cache_file
        self._lock = threading.Lock()
        os.makedirs(self.dir, exist_ok=True)
        self.cache: Dict[str, str] = self._load()

    def _load(self) -> Dict[str, str]:
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _save(self):
        try:
            tmp = self.cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.cache_file)
        except Exception:
            pass

    def ensure_local(self, title: str, type_: str) -> str:
        if not title:
            return ""
        key = f"{type_.lower()}:{_normalize(title)}"
        with self._lock:
            cached = self.cache.get(key)
            if cached:
                file_path = os.path.join(POSTER_DIR, os.path.basename(cached))
                if os.path.exists(file_path):
                    return cached
                else:
                    self.cache.pop(key, None)

        url = _tmdb_find_poster_url(title, type_)
        if not url:
            return ""

        fname = _sha_name(type_, title)
        fpath = os.path.join(POSTER_DIR, fname)
        try:
            r = requests.get(url, timeout=8)
            if r.status_code == 200:
                with open(fpath, "wb") as f:
                    f.write(r.content)
                rel = f"/static/posters/{fname}"
                with self._lock:
                    self.cache[key] = rel
                    self._save()
                return rel
        except Exception:
            pass
        return ""



    def cleanup_unused(self, used_rel_paths: Set[str]):
        try:
            if not used_rel_paths:
                return
            now = time.time()
            existing = set()
            if os.path.isdir(POSTER_DIR):
                for fname in os.listdir(POSTER_DIR):
                    if fname.lower().endswith(".jpg"):
                        existing.add(f"/static/posters/{fname}")

            candidates = existing - set(used_rel_paths)
            for rel in candidates:
                fpath = os.path.join(POSTER_DIR, os.path.basename(rel))
                try:
                    if (now - os.path.getmtime(fpath)) < GC_MIN_AGE:
                        continue
                    os.remove(fpath)
                except Exception:
                    pass

            with self._lock:
                for k, v in list(self.cache.items()):
                    if v not in used_rel_paths and v in candidates:
                        self.cache.pop(k, None)
                self._save()
        except Exception:
            pass

    def remove_for_title(self, type_: str, title: str, force: bool = True) -> bool:
        """
        Usuwa plakat powiązany z (type_, title) NATYCHMIAST:
        - kasuje plik z POSTER_DIR
        - czyści wpis z poster_cache.json
        Zwraca True, jeśli coś usunięto.
        """
        if not title:
            return False
        key = f"{type_.lower()}:{_normalize(title)}"
        with self._lock:
            rel = self.cache.pop(key, None)
            self._save()
        if not rel:
            return False

        try:
            fpath = os.path.join(POSTER_DIR, os.path.basename(rel))
            if os.path.isfile(fpath):
                os.remove(fpath)
                return True
        except Exception:
            pass
        return False

def _collect_used_poster_rel_paths() -> Set[str]:
    """
    Zbiera relatywne ścieżki /static/posters/*.jpg, które są *aktualnie używane*
    w AvailableCache (filmy + seriale).
    """
    used: Set[str] = set()
    try:
        # snapshot (bez blokad – get_* już kopiuje)
        films = available_cache.get_films()
        series = available_cache.get_series()
        for it in (films + series):
            rel = it.get("thumb")
            if isinstance(rel, str) and rel.startswith("/static/posters/"):
                used.add(rel)
    except Exception:
        pass
    return used


# ─────────────────────────────────────────────────────────────────────────────
# Cache „Dostępne” – preload przy starcie + watchdog
# ─────────────────────────────────────────────────────────────────────────────
class AvailableCache:
    def __init__(self, poster_mgr: PosterManager):
        self.poster_mgr = poster_mgr
        self._lock = threading.Lock()
        self.data = {"films": [], "series": []}
        try:
            if os.path.exists(AVAILABLE_CACHE_FILE):
                with open(AVAILABLE_CACHE_FILE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    self.data = d
        except Exception:
            pass

    def _save(self):
        try:
            tmp = AVAILABLE_CACHE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, AVAILABLE_CACHE_FILE)
        except Exception:
            pass

    def _build_films(self, plex) -> List[dict]:
        out: List[dict] = []
        for video in plex.library.section("Filmy").all():
            try:
                key = str(video.ratingKey)
                title = video.title
                local_thumb = self.poster_mgr.ensure_local(title, "movie")
                progress = (
                    100
                    if getattr(video, "isWatched", False)
                    else (round(((video.viewOffset or 0) / (video.duration or 1)) * 100, 1) if video.viewOffset else 0)
                )
                watched_at = int(video.lastViewedAt.timestamp() * 1000) if video.lastViewedAt else None
                try:
                    path = video.media[0].parts[0].file
                except Exception:
                    path = ""
                delete_at = watched_at + 7 * 86400000 if progress >= 100 and watched_at else None

                out.append(
                    {
                        "id": key,
                        "title": title,
                        "thumb": local_thumb or (plex.url(video.thumb) if video.thumb else ""),
                        "progress": progress,
                        "watchedAt": watched_at,
                        "deleteAt": delete_at,
                        "path": path,
                        "type": "film",
                    }
                )
            except Exception:
                continue
        return out

    # ─────────────────────────────────────────────────────────────────────────
    # NOWE: liczenie progresu odcinków/serialu (bez "unresolved reference")
    # ─────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _episode_progress_percent(ep, finished_threshold: float = 0.98) -> int:
        """
        Zwraca procent obejrzenia odcinka:
        - jeśli Plex oznaczył jako obejrzany (isWatched/viewCount) → 100
        - inaczej z viewOffset/duration; >= finished_threshold => 100
        Zwraca int 0..100.
        """
        try:
            dur = int(getattr(ep, "duration", 0) or 0)
            # "twardy" watched z Plexa
            if getattr(ep, "isWatched", False) or int(getattr(ep, "viewCount", 0) or 0) > 0:
                return 100
            off = int(getattr(ep, "viewOffset", 0) or 0)
            if dur <= 0:
                return 0
            off = max(0, min(off, dur))  # clamp
            frac = off / dur
            if frac >= finished_threshold:
                return 100
            return min(100, max(0, round(frac * 100)))
        except Exception:
            return 0

    @classmethod
    def _series_progress_weighted(cls, episodes: List[object]) -> int:
        """
        Ważony procent serialu po czasie: suma(pct_i * dur_i) / suma(dur_i).
        Jeśli brak duration, fallback: zwykła średnia z pct_i. Zwraca 0..100.
        """
        weighted_sum = 0
        dur_sum = 0
        any_duration = False
        for ep in (episodes or []):
            dur = int(getattr(ep, "duration", 0) or 0)
            pct = cls._episode_progress_percent(ep)  # 0..100
            if dur > 0:
                any_duration = True
                weighted_sum += pct * dur
                dur_sum += dur
        if any_duration and dur_sum > 0:
            return round(weighted_sum / dur_sum)
        # fallback: średnia arytmetyczna
        percents = [cls._episode_progress_percent(ep) for ep in (episodes or [])]
        return round(sum(percents) / len(percents)) if percents else 0

    def _build_series(self, plex) -> List[dict]:
        out: List[dict] = []
        for show in plex.library.section("Seriale").all():
            try:
                key = str(show.ratingKey)
                title = show.title
                local_thumb = self.poster_mgr.ensure_local(title, "tv")

                plex_eps = show.episodes()

                # —— katalogi sezonów (każdy odcinek → dirname pliku)
                season_dirs: Set[str] = set()
                for ep in plex_eps:
                    try:
                        p = ep.media[0].parts[0].file
                        if p:
                            season_dirs.add(os.path.dirname(p))
                    except Exception:
                        pass
                season_dirs_list = sorted(season_dirs)

                # —— progres ważony i kompletność
                series_progress = self._series_progress_weighted(plex_eps)
                ep_list: List[dict] = []
                all_finished = True
                last_viewed_ms = 0

                for ep in plex_eps:
                    dur = int(getattr(ep, "duration", 0) or 0)
                    off = int(getattr(ep, "viewOffset", 0) or 0)
                    prog = self._episode_progress_percent(ep)
                    ep_watched_ms = int(ep.lastViewedAt.timestamp() * 1000) if getattr(ep, "lastViewedAt",
                                                                                       None) else None
                    ep_delete_ms = (ep_watched_ms + 7 * 86400000) if (prog >= 100 and ep_watched_ms) else None

                    if ep_watched_ms:
                        last_viewed_ms = max(last_viewed_ms, ep_watched_ms)
                    if prog < 100:
                        all_finished = False

                    ep_list.append({
                        "season": ep.seasonNumber,
                        "episode": ep.index,
                        "title": ep.title,
                        "progress": prog,
                        "durationMs": dur,
                        "viewOffsetMs": off,
                        "watchedAt": ep_watched_ms,
                        "deleteAt": ep_delete_ms,
                        "id": str(ep.ratingKey),
                        "parentId": key,
                    })

                # uwaga: timer serialu uruchamiamy TYLKO jeśli wszystkie odcinki = 100%
                series_delete_at = (last_viewed_ms + 7 * 86400000) if (all_finished and last_viewed_ms) else None

                # path (legacy) bywa pusty – prawdziwa lista jest w "paths"
                try:
                    path = show.media[0].parts[0].file
                except Exception:
                    path = ""

                out.append({
                    "id": key,
                    "title": title,
                    "thumb": local_thumb or (plex.url(show.thumb) if show.thumb else ""),
                    "progress": series_progress,
                    "watchedAt": last_viewed_ms,
                    "deleteAt": series_delete_at,
                    "episodes": ep_list,
                    "path": path,
                    "paths": season_dirs_list,  # ⬅️ TU: katalogi sezonów
                    "type": "series",
                })
            except Exception:
                continue
        return out

    def rebuild_now(self):
        plex = get_plex_or_none()
        if plex is None:
            try:
                progress_log.info("AvailableCache: Plex offline – pomijam rebuild_now")
            except Exception:
                print("ℹ️ AvailableCache: Plex offline – pomijam rebuild_now")
            return

        try:
            films = self._build_films(plex)
            series = self._build_series(plex)

            used = {
                i["thumb"]
                for i in (films + series)
                if isinstance(i.get("thumb"), str) and i["thumb"].startswith("/static/posters/")
            }
            self.poster_mgr.cleanup_unused(used)

            payload = {"films": films, "series": series}
            payload = self._apply_overrides(payload)

            with self._lock:
                self.data = payload
                self._save()

            try:
                progress_log.info("AvailableCache: przebudowano cache (%d filmów, %d seriali)",
                                  len(films), len(series))
            except Exception:
                pass
        except Exception as e:
            try:
                progress_log.warning("AvailableCache.rebuild_now błąd: %s", e)
            except Exception:
                print(f"⚠️ AvailableCache.rebuild_now: {e}")

    def _load_progress_overrides(self) -> Dict[str, int]:
        try:
            with open(PROGRESS_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            out = {}
            for k, v in data.items():
                try:
                    da = v.get("delete_at")
                    if isinstance(da, int):
                        out[str(k)] = da
                except Exception:
                    pass
            return out
        except Exception:
            return {}

    def _apply_overrides(self, payload: dict) -> dict:
        """Podmień deleteAt w films/series oraz ODCINKACH jeśli override w progress_cache jest większy."""
        overrides = self._load_progress_overrides()

        def _pick(cur, ov):
            if not isinstance(ov, int):
                return cur
            if not isinstance(cur, int):
                return ov
            return max(cur, ov)

        # filmy
        for it in payload.get("films", []):
            _id = str(it.get("id") or "")
            if not _id:
                continue
            ov = overrides.get(_id)
            if isinstance(ov, int):
                it["deleteAt"] = _pick(it.get("deleteAt"), ov)

        # seriale + odcinki
        for it in payload.get("series", []):
            sid = str(it.get("id") or "")
            if sid:
                ov = overrides.get(sid)
                if isinstance(ov, int):
                    it["deleteAt"] = _pick(it.get("deleteAt"), ov)
            # epizody
            eps = it.get("episodes") or []
            for ep in eps:
                eid = str(ep.get("id") or "")
                if not eid:
                    continue
                ov = overrides.get(eid)
                if isinstance(ov, int):
                    ep["deleteAt"] = _pick(ep.get("deleteAt"), ov)

        return payload

    def apply_overrides_from_progress(self):
        """Zastosuj override’y na bieżącej pamięci i zapisz cache na dysk."""
        with self._lock:
            current = {
                "films": list(self.data.get("films", [])),
                "series": list(self.data.get("series", [])),
            }
        patched = self._apply_overrides(current)
        with self._lock:
            self.data = patched
            self._save()

    def refresh_in_background(self, every_minutes: int = 30):
        def _loop():
            while not SHUTDOWN_EVENT.is_set():
                try:
                    SHUTDOWN_EVENT.wait(max(1, every_minutes) * 60)
                    if SHUTDOWN_EVENT.is_set():
                        break
                    self.rebuild_now()
                except Exception:
                    try:
                        progress_log.warning("Available watchdog error", exc_info=True)
                    except Exception:
                        print("⚠️ Available watchdog error:", sys.exc_info()[1])

        threading.Thread(target=_loop, daemon=True, name="available-watchdog").start()

    def get_films(self) -> List[dict]:
        with self._lock:
            return list(self.data.get("films", []))

    def get_series(self) -> List[dict]:
        with self._lock:
            return list(self.data.get("series", []))


poster_mgr = PosterManager(POSTER_DIR, POSTER_CACHE_FILE)
available_cache = AvailableCache(poster_mgr)





def trigger_postfinish_preload(title: str, path: str):
    def _job():
        if SHUTDOWN_EVENT.is_set():
            return
        plex = get_plex_or_none()
        if plex is None:
            print("ℹ️ [PostFinish] Plex offline – pomijam preload")
            return
        try:
            lp = (path or "").lower()
            section_name = None
            media_type = "movie"
            if "plex\\seriale" in lp or "/plex/seriale" in lp:
                section_name = "Seriale"; media_type = "tv"
            elif "plex\\filmy" in lp or "/plex/filmy" in lp:
                section_name = "Filmy"; media_type = "movie"
            try:
                poster_mgr.ensure_local(title, media_type)
            except Exception as e:
                print(f"⚠️ [PostFinish] ensure_local error: {e}")
            if section_name:
                try: plex.library.section(section_name).update()
                except Exception as e: print(f"⚠️ [PostFinish] Plex section update error: {e}")
            for _ in range(15):
                if SHUTDOWN_EVENT.is_set(): return
                time.sleep(1)
            try:
                if not SHUTDOWN_EVENT.is_set():
                    available_cache.rebuild_now()
                    print("✅ [PostFinish] Cache przebudowany, plakaty gotowe.")
            except Exception as e:
                print(f"⚠️ [PostFinish] rebuild_now error: {e}")
        except Exception as e:
            print(f"⚠️ [PostFinish] preload error: {e}")
    threading.Thread(
        target=_job, daemon=True,
        name=f"post-finish-preload-{hashlib.md5((title or '').encode()).hexdigest()[:6]}"
    ).start()

def stop_backgrounds(join_timeout: float = 5.0):
    """Zatrzymaj pętle w tle i poczekaj aż wątki się zakończą."""
    SHUTDOWN_EVENT.set()
    deadln = time.time() + join_timeout
    for th in list(threading.enumerate()):
        if th is threading.current_thread():
            continue
        name = (th.name or "").lower()
        if any(name.startswith(n) for n in _BG_THREAD_NAMES):
            try:
                remaining = max(0.0, deadln - time.time())
                th.join(timeout=remaining)
            except Exception:
                pass

_SHUTDOWN_ONCE = False
def graceful_shutdown(reason: str = "unknown", hard: bool = False):
    """
    Zatrzymuje pętle w tle, finalizuje cache i bezpiecznie gasi klienta torrentów.
    Wywoływalne z sygnałów, /admin/shutdown, atexit i launchera.
    """
    if _SHUTDOWN_ONCE.is_set():
        return
    _SHUTDOWN_ONCE.set()

    try:
        progress_log.info("graceful_shutdown start (reason=%s)", reason)
    except Exception:
        print(f"graceful_shutdown start (reason={reason})")

    # zatrzymaj pętle
    try:
        SHUTDOWN_EVENT.set()
    except Exception:
        pass

    # daj wątkom wyjść z pętli (czeka krótko, żeby nie blokować)
    for _ in range(10):
        time.sleep(0.1)

    # ostatnia synchronizacja cache → dysk
    try:
        sync_progress_cache_from_available()
        try:
            available_cache.apply_overrides_from_progress()
        except Exception:
            pass
    except Exception:
        pass

    # ostrożnie wyhamuj torrenty i zamknij sesję
    try:
        if hasattr(tclient, "pause_all"):
            tclient.pause_all()
    except Exception:
        pass
    try:
        tclient.shutdown()
    except Exception:
        pass

    try:
        progress_log.info("graceful_shutdown done")
    except Exception:
        pass

    if hard:
        # TYLKO przy zamknięciu z sygnału/launchera; atexit nie używa hard.
        os._exit(0)



def sync_progress_cache_from_available():
    films = available_cache.get_films()
    series = available_cache.get_series()

    def pick_delete_series(prev_delete, new_delete):
        # jeśli nowy stan nie ma timera (nie 100%), to go kasujemy
        nd = new_delete if isinstance(new_delete, int) else None
        if nd is None:
            return None
        od = prev_delete if isinstance(prev_delete, int) else None
        return nd if od is None else max(od, nd)

    with PROGRESS_LOCK:
        store = _progress_load()

        # — filmy (bez zmian)
        for f in films:
            _id = str(f.get("id") or "")
            if not _id:
                continue
            prev = store.get(_id, {})
            keep_delete = max(prev.get("delete_at", 0), f.get("deleteAt", 0)) if isinstance(prev.get("delete_at"), int) and isinstance(f.get("deleteAt"), int) else (f.get("deleteAt") if isinstance(f.get("deleteAt"), int) else prev.get("delete_at"))
            store[_id] = {
                "id": _id,
                "title": f.get("title") or "",
                "type": "film",
                "path": f.get("path") or "",
                "watched_at": f.get("watchedAt"),
                "delete_at": keep_delete,
            }

        # — seriale (paths + logiczne anulowanie timera)
        for s in series:
            sid = str(s.get("id") or "")
            if not sid:
                continue
            prev = store.get(sid, {})
            keep_delete = pick_delete_series(prev.get("delete_at"), s.get("deleteAt"))

            # lista katalogów sezonów (Windows fix)
            paths = []
            for p in (s.get("paths") or []):
                try:
                    paths.append(fix_windows_path(p))
                except Exception:
                    paths.append(p)

            store[sid] = {
                "id": sid,
                "title": s.get("title") or "",
                "type": "series",
                "path": s.get("path") or "",   # legacy
                "paths": paths,                # ⬅️ NOWE: lista katalogów
                "watched_at": s.get("watchedAt"),
                "delete_at": keep_delete,
            }

            # — odcinki jako osobne wpisy (bez zmian)
            for ep in (s.get("episodes") or []):
                eid = str(ep.get("id") or "")
                if not eid:
                    continue
                prev_ep = store.get(eid, {})
                # dla epizodu – jeśli nie 100% już, to delete_at == None
                nd = ep.get("deleteAt") if isinstance(ep.get("deleteAt"), int) else None
                od = prev_ep.get("delete_at") if isinstance(prev_ep.get("delete_at"), int) else None
                keep_ep = (None if nd is None else (nd if od is None else max(od, nd)))

                # ładna nazwa
                try:
                    s_no = int(ep.get("season") or 0)
                    e_no = int(ep.get("episode") or 0)
                    nice = f"S{s_no:02d}E{e_no:02d} {ep.get('title') or ''}".strip()
                except Exception:
                    nice = ep.get("title") or ""

                store[eid] = {
                    "id": eid,
                    "parent_id": sid,
                    "title": f"{s.get('title') or ''} – {nice}".strip(),
                    "type": "episode",
                    "path": "",
                    "watched_at": ep.get("watchedAt"),
                    "delete_at": keep_ep,
                }

        _progress_save(store)

    try:
        available_cache.apply_overrides_from_progress()
    except Exception:
        pass

# --- start background refresh dla AvailableCache (bez NameError) ------------
def _start_available_watchdog():
    try:
        available_cache.refresh_in_background(every_minutes=30)
    except Exception:
        # Zaloguj pełny traceback, bez używania zmiennej 'e'
        try:
            progress_log.warning("Available watchdog init error", exc_info=True)
        except Exception:
            print("⚠️ Available watchdog init error (fallback print):", sys.exc_info()[1])





def start_progress_cache_watchdog(every_minutes: int = 10):
    def loop():
        try:
            progress_log.info("progress-cache-watchdog START (interval=%s min)", every_minutes)
        except Exception:
            pass

        while not SHUTDOWN_EVENT.is_set():
            t0 = time.time()
            try:
                sync_progress_cache_from_available()
                try:
                    available_cache.apply_overrides_from_progress()
                except Exception:
                    pass
                try:
                    dt = time.time() - t0
                    progress_log.debug("watchdog tick OK in %.3fs", dt)
                except Exception:
                    pass
            except Exception:
                try:
                    progress_log.exception("watchdog tick FAILED")
                except Exception:
                    print("⚠️ progress-cache sync:", sys.exc_info()[1])

            SHUTDOWN_EVENT.wait(max(1, every_minutes) * 60)

    try:
        if not SHUTDOWN_EVENT.is_set():
            sync_progress_cache_from_available()
            try:
                available_cache.apply_overrides_from_progress()
            except Exception:
                pass
            try:
                progress_log.info("initial progress-cache sync OK")
            except Exception:
                pass
    except Exception:
        try:
            progress_log.exception("initial progress-cache sync FAILED")
        except Exception:
            print("⚠️ initial progress-cache sync:", sys.exc_info()[1])

    threading.Thread(target=loop, daemon=True, name="progress-cache-watchdog").start()






# ─────────────────────────────────────────────────────────────────────────────
# Automatyczny cleanup wg PROGRESS_CACHE_FILE
# ─────────────────────────────────────────────────────────────────────────────
@app.after_request
def add_no_store(resp):
    try:
        if request.path.startswith("/plex/") or request.path in ("/search-local", "/status"):
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
    except Exception:
        pass
    return resp

@app.route("/debug/progress/<item_id>")
def debug_progress(item_id):
    with PROGRESS_LOCK:
        store = _progress_load()
        return jsonify(store.get(item_id) or {"error": "not found"})


def log_cleanup_entry(title, media_type, path):
    log_line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ✅ Usunięto: {title} ({media_type}) – {path}\n"
    with open("cleanup.log", "a", encoding="utf-8") as log_file:
        log_file.write(log_line)


def cleanup_old_media():
    try:
        now = int(time.time() * 1000)
        removed = []

        # Mapuj dysk sieciowy (Windows) – bez paniki jeśli już jest
        try:
            map_network_drive()
        except Exception:
            pass

        # Plex może być offline – wtedy grzecznie pomijamy cleanup
        plex = get_plex_or_none()
        if plex is None:
            try:
                progress_log.info("cleanup: Plex offline – pomijam sprzątanie")
            except Exception:
                pass
            return jsonify({"removed": [], "skipped": True, "reason": "plex_unavailable"})

        # helper: czy serial w 100% obejrzany (ostateczna weryfikacja po stronie Plex)
        def _is_series_fully_watched(series_id: str, finished_threshold: float = 0.98) -> bool:
            try:
                show = plex.fetchItem(int(series_id))
                episodes = show.episodes()
            except Exception:
                return False

            try:
                for ep in episodes:
                    pct = AvailableCache._episode_progress_percent(ep, finished_threshold)
                    if pct < 100:
                        return False
                return True
            except Exception:
                for ep in episodes:
                    try:
                        dur = int(getattr(ep, "duration", 0) or 0)
                        if int(getattr(ep, "viewCount", 0) or 0) > 0:
                            continue
                        off = int(getattr(ep, "viewOffset", 0) or 0)
                        if dur <= 0 or (off / max(1, dur)) < finished_threshold:
                            return False
                    except Exception:
                        return False
                return True

        # snapshot kluczy
        with PROGRESS_LOCK:
            store = _progress_load()
            keys = list(store.keys())

        for key in keys:
            try:
                # świeży odczyt wpisu
                with PROGRESS_LOCK:
                    entry = _progress_load().get(key)
                if not entry:
                    continue

                item_id = str(entry.get("id") or key)
                media_type = (entry.get("type") or "").lower()
                title = entry.get("title") or ""
                delete_at = entry.get("delete_at")

                # 1) Odcinków NIE kasujemy automatycznie – jedynie czyścimy przeterminowany timer
                if media_type == "episode":
                    if isinstance(delete_at, int) and delete_at < now:
                        with PROGRESS_LOCK:
                            s2 = _progress_load()
                            if item_id in s2 and isinstance(s2[item_id], dict):
                                s2[item_id]["delete_at"] = None
                                _progress_save(s2)
                    continue

                # 2) Timer nieaktywny → nic do roboty
                if not (isinstance(delete_at, int) and delete_at < now):
                    continue

                # 3) Filmy kasujemy normalnie; Seriale tylko jeśli CAŁOŚĆ = 100%
                if media_type == "series":
                    if not _is_series_fully_watched(item_id):
                        with PROGRESS_LOCK:
                            s2 = _progress_load()
                            if item_id in s2 and isinstance(s2[item_id], dict):
                                s2[item_id]["delete_at"] = None
                                _progress_save(s2)
                        try:
                            progress_log.info("cleanup: skipped series (not 100%%) id=%s title=%s", item_id, title)
                        except Exception:
                            pass
                        continue
                elif media_type not in ("film", "movie", "series"):
                    continue

                # 4) Zbierz ścieżki do usunięcia
                paths_to_remove: List[str] = []
                entry_paths = entry.get("paths") if isinstance(entry.get("paths"), list) else []
                if entry_paths:
                    paths_to_remove.extend(entry_paths)
                elif entry.get("path"):
                    paths_to_remove.append(entry.get("path"))

                # Fallback: jeśli brak ścieżek w cache → spróbuj z Plex
                plex_item = None
                try:
                    try:
                        plex_item = plex.fetchItem(int(item_id))
                    except Exception:
                        plex_item = None

                    if plex_item is not None:
                        if media_type == "series":
                            season_dirs = set()
                            for ep in plex_item.episodes():
                                try:
                                    fp = ep.media[0].parts[0].file
                                    if fp:
                                        season_dirs.add(os.path.dirname(fp))
                                except Exception:
                                    pass
                            paths_to_remove.extend(list(season_dirs))
                        else:  # film
                            try:
                                fp = plex_item.media[0].parts[0].file
                                if fp:
                                    paths_to_remove.append(fp)
                            except Exception:
                                pass
                except Exception as e:
                    try:
                        progress_log.warning("cleanup: fetchItem fallback failed (%s): %s", item_id, e)
                    except Exception:
                        pass

                # deduplikacja + normalizacja ścieżek (Windows/net share fix)
                norm_paths = []
                seen = set()
                for p in paths_to_remove:
                    p2 = fix_windows_path(p or "")
                    if p2 and p2 not in seen:
                        norm_paths.append(p2)
                        seen.add(p2)

                fs_status = []

                # 5) Fizyczne usuwanie z dysku (rekurencyjnie dla katalogów)
                import shutil
                for p in norm_paths:
                    try:
                        if os.path.isfile(p):
                            os.remove(p)
                            fs_status.append({"path": p, "status": "file_removed"})
                            log_cleanup_entry(title, media_type, p)
                        elif os.path.isdir(p):
                            try:
                                shutil.rmtree(p)
                                fs_status.append({"path": p, "status": "dir_removed_recursive"})
                                log_cleanup_entry(title, media_type, p)
                            except Exception as e:
                                fs_status.append({"path": p, "status": f"dir_remove_error: {e}"})
                        else:
                            fs_status.append({"path": p, "status": "path_not_found"})
                    except Exception as e:
                        fs_status.append({"path": p, "status": f"remove_error: {e}"})

                # spróbuj usunąć puste katalogi nadrzędne
                try:
                    parents = {os.path.dirname(p) for p in norm_paths}
                    for parent in parents:
                        try:
                            if parent and os.path.isdir(parent) and not os.listdir(parent):
                                os.rmdir(parent)
                                try:
                                    progress_log.info("FS removed empty parent dir: %s", parent)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                except Exception:
                    pass

                # 6) Usuń z Plex (preferuj ID)
                plex_removed = False
                try:
                    if 'plex_item' not in locals() or plex_item is None:
                        try:
                            plex_item = plex.fetchItem(int(item_id))
                        except Exception:
                            plex_item = None
                    if plex_item is not None:
                        plex_item.delete()
                        plex_removed = True
                    else:
                        try:
                            if media_type in ("film", "movie"):
                                plex.library.section("Filmy").get(title).delete()
                                plex_removed = True
                            elif media_type == "series":
                                plex.library.section("Seriale").get(title).delete()
                                plex_removed = True
                        except Exception:
                            pass
                except Exception as e:
                    try:
                        progress_log.warning(
                            "cleanup: plex remove failed id=%s title=%s type=%s err=%s",
                            item_id, title, media_type, e
                        )
                    except Exception:
                        pass

                # 7) Usuń wpis(y) z progress_cache.json
                with PROGRESS_LOCK:
                    store2 = _progress_load()
                    if item_id in store2:
                        store2.pop(item_id, None)
                    if media_type == "series":
                        to_del = [k for k, v in store2.items()
                                  if isinstance(v, dict) and v.get("parent_id") == item_id]
                        for k in to_del:
                            store2.pop(k, None)
                    _progress_save(store2)

                # 7.5) Usuń plakat NATYCHMIAST (filmy/seriale – odcinków nie tykamy)
                try:
                    if media_type in ("film", "movie"):
                        poster_mgr.remove_for_title("movie", title, force=True)
                    elif media_type == "series":
                        poster_mgr.remove_for_title("tv", title, force=True)
                except Exception:
                    pass

                removed.append({
                    "id": item_id,
                    "title": title,
                    "type": media_type,
                    "fs": fs_status,
                    "plexRemoved": plex_removed
                })

            except Exception:
                try:
                    progress_log.exception("cleanup loop error for key=%s", key)
                except Exception:
                    pass
                continue

        # (opcjonalnie) szybkie odświeżenie dostępnych po większym sprzątaniu
        try:
            available_cache.rebuild_now()
        except Exception:
            pass

        return jsonify({"removed": removed, "skipped": False})
    except Exception as e:
        try:
            progress_log.exception("cleanup_old_media fatal: %s", e)
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500

def run_cleanup_loop(interval_minutes=10):
    def loop():
        while not SHUTDOWN_EVENT.is_set():
            try:
                print("🔁 Automatyczny cleanup…")
                with app.app_context():
                    cleanup_old_media()
            except Exception as e:
                print(f"❌ Błąd automatycznego cleanupu: {e}")
            SHUTDOWN_EVENT.wait(interval_minutes * 60)
    threading.Thread(target=loop, daemon=True, name="cleanup-loop").start()




@app.route("/maintenance/sweep-posters", methods=["POST", "GET"])
def sweep_posters():
    """
    Sprawdzarka aktualności plakatów. Działa cyklicznie i na żądanie (GET/POST).
    Parametry (opcjonalnie, jako query lub JSON):
      - force=true  -> ignoruje GC_MIN_AGE i usuwa sieroty natychmiast
      - dry_run=true -> tylko raport (nic nie usuwa)
      - rebuild=true -> najpierw available_cache.rebuild_now()
    Zwraca JSON z podsumowaniem.
    """
    try:
        # --- paramy
        try:
            payload = request.get_json(silent=True) or {}
        except Exception:
            payload = {}
        q = request.args
        force = str(payload.get("force", q.get("force", "false"))).lower() in {"1", "true", "yes", "y", "on"}
        dry_run = str(payload.get("dry_run", q.get("dry_run", "false"))).lower() in {"1", "true", "yes", "y", "on"}
        do_rebuild = str(payload.get("rebuild", q.get("rebuild", "false"))).lower() in {"1", "true", "yes", "y", "on"}

        # (opcjonalnie) odśwież najpierw available
        if do_rebuild:
            try:
                available_cache.rebuild_now()
            except Exception:
                pass

        used = _collect_used_poster_rel_paths()

        # zczytaj aktualne pliki i cache
        existing_files = set()
        for fname in os.listdir(POSTER_DIR):
            if fname.lower().endswith(".jpg"):
                existing_files.add(f"/static/posters/{fname}")

        # sieroty względem available
        orphans = sorted(existing_files - used)

        # oraz wpisy w poster_cache, które wskazują na nieistniejący plik
        broken_cache_keys = []
        with poster_mgr._lock:
            cache_copy = dict(poster_mgr.cache)
        for k, rel in cache_copy.items():
            fpath = os.path.join(POSTER_DIR, os.path.basename(rel or ""))
            if not (rel and os.path.isfile(fpath)):
                broken_cache_keys.append(k)

        removed_files = []
        removed_cache_keys = []
        skipped_young = []

        # wiek (sięga po GC_MIN_AGE, ale można wymusić force)
        now = time.time()

        # USUWANIE SIEROT Z DYSKU
        for rel in orphans:
            fpath = os.path.join(POSTER_DIR, os.path.basename(rel))
            try:
                if not os.path.isfile(fpath):
                    continue
                if not force:
                    # zachowaj ten sam próg co reszta systemu
                    mtime = os.path.getmtime(fpath)
                    if (now - mtime) < GC_MIN_AGE:
                        skipped_young.append(rel)
                        continue
                if not dry_run:
                    os.remove(fpath)
                removed_files.append(rel)
            except Exception as e:
                try:
                    progress_log.warning("Poster sweep: remove file failed %s: %s", rel, e)
                except Exception:
                    pass

        # CZYSZCZENIE „ZWICHROWANYCH” WPISÓW W CACHE
        if broken_cache_keys:
            with poster_mgr._lock:
                for k in broken_cache_keys:
                    if not dry_run:
                        poster_mgr.cache.pop(k, None)
                if not dry_run:
                    poster_mgr._save()
                removed_cache_keys.extend(broken_cache_keys)

        # DODATKOWO: usuń z cache wpisy dla kluczy, których rel nie jest już w 'used'
        # (ochrona na wypadek, gdyby rebuild użył innego plakatu)
        with poster_mgr._lock:
            for k, rel in list(poster_mgr.cache.items()):
                if rel and rel not in used:
                    # jeśli plik istnieje i nie był usunięty – pozostaw decyzję GC (chyba że force)
                    if force and not dry_run:
                        poster_mgr.cache.pop(k, None)
                        removed_cache_keys.append(k)
            if force and not dry_run:
                poster_mgr._save()

        # raport
        try:
            progress_log.info(
                "Poster sweep: removed_files=%d, removed_cache=%d, skipped_young=%d, dry_run=%s, force=%s",
                len(removed_files), len(removed_cache_keys), len(skipped_young), dry_run, force
            )
        except Exception:
            pass

        return jsonify({
            "used_count": len(used),
            "existing_files_count": len(existing_files),
            "orphans_count": len(orphans),
            "removed_files": removed_files,
            "removed_cache_keys": removed_cache_keys,
            "skipped_young": skipped_young,
            "dry_run": dry_run,
            "force": force
        })
    except Exception as e:
        try:
            progress_log.exception("sweep_posters failed")
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500



def start_poster_sweeper(every_minutes: int = 60, rebuild_before: bool = False, force: bool = False):
    def loop():
        while not SHUTDOWN_EVENT.is_set():
            try:
                with app.app_context():
                    if rebuild_before:
                        try:
                            available_cache.rebuild_now()
                        except Exception:
                            pass
                    used = _collect_used_poster_rel_paths()
                    now = time.time()
                    existing_files = set(
                        f"/static/posters/{fn}" for fn in os.listdir(POSTER_DIR)
                        if fn.lower().endswith(".jpg")
                    )
                    orphans = existing_files - used
                    removed_files = []
                    for rel in sorted(orphans):
                        fpath = os.path.join(POSTER_DIR, os.path.basename(rel))
                        try:
                            if not os.path.isfile(fpath):
                                continue
                            if not force and (now - os.path.getmtime(fpath)) < GC_MIN_AGE:
                                continue
                            os.remove(fpath)
                            removed_files.append(rel)
                        except Exception:
                            pass
                    with poster_mgr._lock:
                        changed = False
                        for k, rel in list(poster_mgr.cache.items()):
                            fp = os.path.join(POSTER_DIR, os.path.basename(rel or ""))
                            if not (rel and os.path.isfile(fp)):
                                poster_mgr.cache.pop(k, None)
                                changed = True
                        if changed:
                            poster_mgr._save()
                    try:
                        progress_log.info("Poster sweeper tick: removed=%d, force=%s", len(removed_files), force)
                    except Exception:
                        pass
            except Exception:
                try:
                    progress_log.warning("Poster sweeper error", exc_info=True)
                except Exception:
                    pass
            SHUTDOWN_EVENT.wait(max(1, every_minutes) * 60)
    threading.Thread(target=loop, daemon=True, name="poster-sweeper").start()



# ─────────────────────────────────────────────────────────────────────────────
# Torrenty
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        magnet = request.form.get("magnet")
        source = request.form.get("source", "default")
        if magnet:
            try:
                if source == "series":
                    download_path = SERIES_DIR
                else:
                    download_path = MOVIES_DIR
                os.makedirs(download_path, exist_ok=True)
                torrent_id = tclient.add_magnet(magnet, download_path)
                if torrent_id:
                    print(f"✅ Dodano torrent (ID: {torrent_id}) → {download_path}")
                else:
                    print("⚠️ Dodano torrent, ale info_hash jeszcze nie dostępny.")
            except Exception as e:
                print(f"❌ Błąd dodawania torrenta (libtorrent): {e}")
        return redirect(url_for("index"))
    return render_template("index.html")


@app.route("/status")
def status():
    try:
        torrents = tclient.get_torrents()
        payload = {
            tid: {
                "name": t.name,
                "progress": float(t.progress),
                "state": t.state,
                "download_payload_rate": int(t.download_payload_rate),
                "eta": int(t.eta),
                "download_location": t.download_location,
            }
            for tid, t in torrents.items()
        }
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/toggle/<torrent_id>", methods=["POST"])
def toggle_torrent(torrent_id):
    try:
        torrents = tclient.get_torrents()
        tor = torrents.get(torrent_id)
        if not tor:
            return jsonify({"error": "Torrent not found"}), 404
        if (tor["state"] if isinstance(tor, dict) else tor.state) == "Paused":
            ok = tclient.resume(torrent_id)
        else:
            ok = tclient.pause(torrent_id)
        if not ok:
            return jsonify({"error": "Operation failed"}), 400
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/remove/<torrent_id>", methods=["POST"])
def remove_torrent(torrent_id):
    remove_data = request.args.get("data") == "true"
    try:
        ok = tclient.remove(torrent_id, remove_data=remove_data)
        if not ok:
            return jsonify({"error": "Torrent not found"}), 404
        return jsonify({"status": "removed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/debug/torrents")
def debug_torrents():
    torrents = tclient.get_torrents()
    response = {tid: t.name if hasattr(t, "name") else t["name"] for tid, t in torrents.items()}
    return jsonify(response)


@app.route("/set-global-limit", methods=["POST"])
def set_global_limit():
    try:
        data = request.get_json(force=True) or {}
        # oczekujemy MB/s (np. 5 -> ok. 5 MB/s)
        limit_mbs = float(data.get("limit", 0))

        # KiB/s dla naszej metody (0 lub <0 => brak limitu)
        limit_kib = -1 if limit_mbs <= 0 else int(limit_mbs * 1024)

        print(f"🌐 Ustawiam globalny limit na: {limit_kib} KiB/s (z {limit_mbs} MB/s)")
        tclient.set_global_download_limit(limit_kib)

        return jsonify({
            "status": "ok",
            "limit_input_unit": "MB/s",
            "limit_mbs": limit_mbs,
            "limit_kib_per_s": (0 if limit_mbs <= 0 else limit_kib),
            "note": "0 lub mniej = bez limitu"
        })
    except Exception as e:
        print(f"❌ Błąd ustawiania limitu: {e}")
        return jsonify({"error": str(e)}), 500



@app.route("/history", methods=["GET"])
def history():
    try:
        items = history_store.get()
        items = sorted(items, key=lambda x: x.get("ts", 0), reverse=True)
        return jsonify({"history": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# Plex + cache „Dostępne”
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/plex/films")
def plex_films():
    try:
        return jsonify(available_cache.get_films())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/plex/series")
def plex_series():
    try:
        return jsonify(available_cache.get_series())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Wyszukiwanie lokalne w Plex (szybkie, legalne)
@app.route("/search-local", methods=["GET"])
def search_local():
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify({"films": [], "series": []})
    films = [i for i in available_cache.get_films() if q in i["title"].lower()]
    series = [i for i in available_cache.get_series() if q in i["title"].lower()]
    return jsonify({"films": films, "series": series})

# ─────────────────────────────────────────────────────────────────────────────
# Pozostałe narzędzia Plex (reset timera, kasowanie)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/plex/reset-delete-timer", methods=["POST"])
def reset_delete_timer():
    try:
        payload = request.get_json(force=True) or {}
        item_id = str(payload.get("id") or "")
        if not item_id:
            return jsonify({"error": "Brak ID"}), 400

        with PROGRESS_LOCK:
            store = _progress_load()
            entry = store.get(item_id)
            if not entry:
                return jsonify({"error": "Nie znaleziono ID"}), 404

            old_time = entry.get("delete_at")
            new_time = int(time.time() * 1000) + 7 * 86400000  # +7 dni
            entry["delete_at"] = new_time
            store[item_id] = entry

            # NATYCHMIASTOWY, ATOMOWY ZAPIS DO PLIKU
            _progress_save(store)

        # log (jeśli dodałeś logger z poprzedniej wiadomości)
        try:
            progress_log.info("Reset delete timer: id=%s old=%s new=%s", item_id, old_time, new_time)
        except Exception:
            pass

        return jsonify({"success": True, "newDeleteAt": new_time})
    except Exception as e:
        try:
            progress_log.exception("reset_delete_timer failed")
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500



# ── ZAMIANA ISTNIEJĄCEGO ENDPOINTU /plex/delete NA TEN ──────────────────────
@app.route("/plex/delete", methods=["DELETE", "POST"])
def delete_plex_item():
    try:
        item_id = _take_id_from_request(request)
        if not item_id:
            return jsonify({"error": "Brak ID"}), 400
        force = _take_force_from_request(request)

        try:
            map_network_drive()
        except Exception:
            pass

        entry = _fetch_entry_from_anywhere(item_id)
        if not entry:
            return jsonify({"error": "Nie znaleziono wpisu (cache/available)"}), 404

        title = entry.get("title") or ""
        media_type = (entry.get("type") or "").lower()

        plex = get_plex_or_none()
        fs_status = []

        # 1) FS
        paths_to_remove = _gather_fs_paths_for_entry(entry, plex)
        import shutil
        for p in paths_to_remove:
            try:
                if os.path.isfile(p):
                    os.remove(p)
                    fs_status.append({"path": p, "status": "file_removed"})
                    progress_log.info("FS removed file: %s", p)
                elif os.path.isdir(p):
                    try:
                        if force:
                            shutil.rmtree(p)
                            fs_status.append({"path": p, "status": "dir_removed_recursive"})
                            progress_log.info("FS removed directory recursively: %s", p)
                        else:
                            os.rmdir(p)
                            fs_status.append({"path": p, "status": "empty_dir_removed"})
                            progress_log.info("FS removed empty directory: %s", p)
                    except OSError:
                        fs_status.append({"path": p, "status": "dir_not_empty"})
                    except Exception as e:
                        fs_status.append({"path": p, "status": f"dir_remove_error: {e}"})
                else:
                    fs_status.append({"path": p, "status": "path_not_found"})
            except Exception as e:
                fs_status.append({"path": p, "status": f"remove_error: {e}"})

        # sprzątanie pustych katalogów nadrzędnych
        try:
            parents = {os.path.dirname(p) for p in (paths_to_remove or [])}
            for parent in parents:
                try:
                    if parent and os.path.isdir(parent) and not os.listdir(parent):
                        os.rmdir(parent)
                        progress_log.info("FS removed empty parent dir: %s", parent)
                except Exception:
                    pass
        except Exception:
            pass

        # 2) Plex
        plex_removed = False
        if plex is not None:
            try:
                plex_item = plex.fetchItem(int(item_id))
                plex_item.delete()
                plex_removed = True
            except Exception as e:
                try:
                    if media_type in ("film", "movie"):
                        plex.library.section("Filmy").get(title).delete()
                        plex_removed = True
                    elif media_type == "series":
                        plex.library.section("Seriale").get(title).delete()
                        plex_removed = True
                except Exception:
                    progress_log.warning("Plex remove failed for id=%s (%s): %s", item_id, media_type, e)

        # 3) progress_cache.json
        with PROGRESS_LOCK:
            store = _progress_load()
            if item_id in store:
                store.pop(item_id, None)
            if media_type == "series":
                to_del = [k for k, v in store.items() if isinstance(v, dict) and v.get("parent_id") == item_id]
                for k in to_del:
                    store.pop(k, None)
            _progress_save(store)

        # 4) Odśwież dostępne
        threading.Thread(target=lambda: available_cache.rebuild_now(), daemon=True).start()

        # 4.5) USUŃ plakat TERAZ (to było po return – przeniesione!)
        try:
            if media_type in ("film", "movie"):
                if poster_mgr.remove_for_title("movie", title, force=True):
                    progress_log.info("Poster removed (movie): %s", title)
            elif media_type == "series":
                if poster_mgr.remove_for_title("tv", title, force=True):
                    progress_log.info("Poster removed (tv): %s", title)
        except Exception as e:
            progress_log.warning("Poster remove failed for %s (%s): %s", title, media_type, e)

        return jsonify({"success": True, "fs": fs_status, "plexRemoved": plex_removed})

    except Exception as e:
        try:
            progress_log.exception("delete_plex_item failed")
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500



# ── Helpers do kasowania (wklej w okolicy innych helperów) ───────────────────
def _take_id_from_request(req) -> str:
    try:
        payload = req.get_json(silent=True) or {}
    except Exception:
        payload = {}
    # priorytet: JSON → form → query
    item_id = str(payload.get("id") or req.form.get("id") or req.args.get("id") or "").strip()
    return item_id

def _take_force_from_request(req) -> bool:
    try:
        payload = req.get_json(silent=True) or {}
    except Exception:
        payload = {}
    force = payload.get("force", req.form.get("force", req.args.get("force", "false")))
    if isinstance(force, bool):
        return force
    return str(force).lower() in {"1", "true", "yes", "y", "on"}

def _fetch_entry_from_anywhere(item_id: str) -> dict | None:
    """
    Najpierw progress_cache.json, potem z AvailableCache (films/series/episodes).
    Zwraca ujednolicony dict: {id, title, type, path?, paths?}
    """
    with PROGRESS_LOCK:
        store = _progress_load()
        e = store.get(item_id)
    if isinstance(e, dict):
        return e

    # Spróbuj w AvailableCache
    def _by_id(lst):
        for it in lst:
            if str(it.get("id")) == item_id:
                return it
        return None

    it = _by_id(available_cache.get_films())
    if it:
        return {
            "id": str(it.get("id")),
            "title": it.get("title") or "",
            "type": "film",
            "path": it.get("path") or "",
        }
    it = _by_id(available_cache.get_series())
    if it:
        return {
            "id": str(it.get("id")),
            "title": it.get("title") or "",
            "type": "series",
            "path": it.get("path") or "",
            "paths": it.get("paths") or [],
        }

    # ostatnia próba: może to odcinek
    for s in available_cache.get_series():
        for ep in (s.get("episodes") or []):
            if str(ep.get("id")) == item_id:
                return {
                    "id": item_id,
                    "title": f"{s.get('title','')} – S{int(ep.get('season') or 0):02d}E{int(ep.get('episode') or 0):02d} {ep.get('title') or ''}",
                    "type": "episode",
                    "path": "",  # uzupełnimy z Plexa przy kasowaniu
                    "parent_id": str(s.get("id") or "")
                }
    return None

def _gather_fs_paths_for_entry(entry: dict, plex) -> list[str]:
    """
    Zwraca listę ścieżek do usunięcia na podstawie entry + (opcjonalnie) Plex.
    """
    media_type = (entry.get("type") or "").lower()
    paths_to_remove: list[str] = []

    entry_paths = entry.get("paths") if isinstance(entry.get("paths"), list) else []
    if entry_paths:
        paths_to_remove.extend(entry_paths)
    elif entry.get("path"):
        paths_to_remove.append(entry.get("path"))

    # Jeśli nic nie mamy w cache, spróbuj z Plex
    try:
        if plex is not None and not paths_to_remove:
            plex_item = plex.fetchItem(int(entry["id"]))
            if media_type == "series":
                season_dirs = set()
                for ep in plex_item.episodes():
                    try:
                        fp = ep.media[0].parts[0].file
                        if fp:
                            season_dirs.add(os.path.dirname(fp))
                    except Exception:
                        pass
                paths_to_remove.extend(sorted(season_dirs))
            elif media_type in ("film", "movie"):
                try:
                    fp = plex_item.media[0].parts[0].file
                    if fp:
                        paths_to_remove.append(fp)
                except Exception:
                    pass
            elif media_type == "episode":
                try:
                    fp = plex_item.media[0].parts[0].file
                    if fp:
                        paths_to_remove.append(fp)
                except Exception:
                    pass
    except Exception:
        pass

    # normalizacja (Windows fix)
    out = []
    seen = set()
    for p in paths_to_remove:
        p2 = fix_windows_path(p or "")
        if p2 and p2 not in seen:
            out.append(p2); seen.add(p2)
    return out




# ─────────────────────────────────────────────────────────────────────────────
# TPB, YTS
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/search", methods=["POST"])
def search():
    query = request.form.get("query")
    site = "https://yts.mx"
    url = f"{site}/browse-movies/{query.replace(' ', '%20')}"
    headers = {"User-Agent": "Mozilla/5.0"}
    results = []
    seen_titles = set()

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        movies = soup.select(".browse-movie-wrap")

        for m in movies:
            title_el = m.select_one(".browse-movie-title")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if title in seen_titles:
                continue
            seen_titles.add(title)

            link = title_el["href"]
            img_tag = m.select_one("img")
            img = img_tag.get("data-src") or img_tag.get("src")

            # TMDb
            display_title = title
            description = ""
            rating = ""
            try:
                tmdb = requests.get(
                    "https://api.themoviedb.org/3/search/movie",
                    params={"api_key": TMDB_API_KEY, "query": title, "language": "pl-PL"},
                    timeout=5
                ).json()
                if tmdb.get("results"):
                    movie = tmdb["results"][0]
                    display_title = movie.get("title", title)
                    description = movie.get("overview", "")
                    rating = movie.get("vote_average", "")
            except:
                pass

            short_description = (description[:200] + "...") if len(description) > 200 else description

            results.append({
                "title": display_title,
                "url": link,
                "image": img,
                "description": short_description,
                "rating": rating
            })

    except Exception as e:
        print("Search error:", e)
        return jsonify({"error": "Search failed"}), 500

    return jsonify({"results": results})
@app.route("/yts", methods=["POST"])
def get_magnet():
    yts_url = request.form.get("yts_url")
    quality = request.form.get("quality", "1080p")
    try:
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        driver = webdriver.Chrome(options=options)
        driver.get(yts_url)
        time.sleep(2)
        torrents = driver.find_elements(By.CLASS_NAME, "modal-torrent")
        for t in torrents:
            try:
                quality_div = t.find_element(By.CLASS_NAME, "modal-quality")
                if quality in quality_div.get_attribute("id"):
                    magnet_links = t.find_elements(By.CSS_SELECTOR, "a.magnet-download")
                    for link in magnet_links:
                        href = link.get_attribute("href")
                        if href.startswith("magnet:"):
                            driver.quit()
                            return jsonify({"magnet": href})
            except Exception:
                continue
        driver.quit()
        return jsonify({"error": "No magnet link found for the selected quality."})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/search-series", methods=["POST"])
def search_series():
    import re
    query = request.form.get("query", "").strip()
    if not query:
        return jsonify({"results": [], "source": "tpb"})
    try:
        tmdb = requests.get(
            "https://api.themoviedb.org/3/search/tv",
            params={"api_key": TMDB_API_KEY, "query": query, "language": "pl-PL"},
            timeout=5
        ).json()
        if tmdb.get("results"):
            query = tmdb["results"][0].get("original_name", query)
            print(f"🌍 TMDb przetłumaczył na: {query}")
    except Exception as e:
        print("⚠️ Błąd tłumaczenia TMDb:", e)

    search_url = f"https://tpb.party/search/{query}/1/7/208"
    print(f"➡️ Szukam serialu (HDTV): {query}")
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(search_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return jsonify({"results": [], "source": "tpb"})

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table#searchResult tr")
        results = []
        for row in rows:
            try:
                title_el = row.find("a", href=lambda x: x and "/torrent/" in x)
                magnet_el = row.find("a", href=lambda x: x and x.startswith("magnet:"))
                columns = row.find_all("td", align="right")
                if not title_el or not magnet_el:
                    continue
                title = title_el.text.strip()
                magnet = magnet_el["href"]
                size = "Nieznany rozmiar"; seeds = leeches = "?"
                if len(columns) >= 3:
                    size = columns[0].text.strip()
                    seeds = columns[1].text.strip()
                    leeches = columns[2].text.strip()

                clean_title = re.sub(
                    r"(S\d+E\d+)|(\d{3,4}p)|x26[45]|BluRay|WEB[- ]?DL|AMZN|NF|DDP\d|COMPLETE|Season \d+|H\.?264|HEVC|BRRip|HDRip|AVS|PSA|\[.*?\]|\.|-|_",
                    " ", title, flags=re.IGNORECASE
                )
                clean_title = re.sub(r"\s+", " ", clean_title).strip()
                title_for_tvmaze = " ".join(clean_title.split()[:3])

                image = "/static/logo.png"; description = ""; rating = ""
                try:
                    tvmaze_resp = requests.get("https://api.tvmaze.com/search/shows",
                                               params={"q": title_for_tvmaze}, timeout=5)
                    tvmaze_data = tvmaze_resp.json()
                    if not tvmaze_data:
                        tvmaze_resp = requests.get("https://api.tvmaze.com/search/shows",
                                                   params={"q": query}, timeout=5)
                        tvmaze_data = tvmaze_resp.json()
                    if tvmaze_data:
                        show = tvmaze_data[0].get("show", {})
                        image_data = show.get("image")
                        if image_data and image_data.get("medium"):
                            image = image_data["medium"]
                        if show.get("summary"):
                            description = BeautifulSoup(show["summary"], "html.parser").get_text()
                            if len(description) > 200:
                                description = description[:200] + "..."
                        if show.get("rating", {}).get("average"):
                            rating = show["rating"]["average"]
                except Exception as e:
                    print("⚠️ TVmaze:", e)

                results.append({
                    "title": title, "magnet": magnet, "image": image,
                    "description": description, "rating": rating,
                    "size": f"Size {size}", "seeds": seeds, "leeches": leeches
                })
            except Exception as e:
                print(f"⛔️ Wiersz:", e)
                continue
        return jsonify({"results": results, "source": "tpb"})
    except Exception as e:
        print(f"❌ Błąd połączenia: {e}")
        return jsonify({"results": [], "source": "tpb"})

@app.route("/search-premium", methods=["POST"])
def search_premium():
    import re
    query = request.form.get("query", "").strip()
    if not query:
        return jsonify({"results": [], "source": "tpb"})
    try:
        tmdb = requests.get(
            "https://api.themoviedb.org/3/search/movie",
            params={"api_key": TMDB_API_KEY, "query": query, "language": "pl-PL"},
            timeout=5
        ).json()
        if tmdb.get("results"):
            query = tmdb["results"][0].get("original_title", query)
            print(f"🌍 TMDb przetłumaczył na: {query}")
    except Exception as e:
        print("⚠️ Błąd tłumaczenia TMDb:", e)

    search_url = f"https://tpb.party/search/{query}/1/7/207"
    print(f"➡️ Szukam premium (207): {query}")
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(search_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return jsonify({"results": [], "source": "tpb"})
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table#searchResult tr")
        results = []
        for row in rows:
            try:
                title_el = row.find("a", href=lambda x: x and "/torrent/" in x)
                magnet_el = row.find("a", href=lambda x: x and x.startswith("magnet:"))
                columns = row.find_all("td", align="right")
                if not title_el or not magnet_el:
                    continue
                title = title_el.text.strip()
                magnet = magnet_el["href"]
                size = "Nieznany rozmiar"; seeds = leeches = "?"
                if len(columns) >= 3:
                    size = columns[0].text.strip()
                    seeds = columns[1].text.strip()
                    leeches = columns[2].text.strip()

                clean_title = re.sub(
                    r"(S\d+E\d+)|(\d{3,4}p)|x26[45]|BluRay|WEB[- ]?DL|AMZN|NF|DDP\d|COMPLETE|Season \d+|H\.?264|HEVC|BRRip|HDRip|AVS|PSA|\[.*?\]|\.|-|_",
                    " ", title, flags=re.IGNORECASE
                )
                clean_title = re.sub(r"\s+", " ", clean_title).strip()
                title_for_tmdb = " ".join(clean_title.split()[:3])

                image = "/static/logo.png"; description = f"Rozmiar: {size}"; rating = ""
                try:
                    tmdb_resp = requests.get(
                        "https://api.themoviedb.org/3/search/movie",
                        params={"api_key": TMDB_API_KEY, "query": title_for_tmdb, "language": "pl-PL"},
                        timeout=5
                    ).json()
                    if not tmdb_resp.get("results"):
                        tmdb_resp = requests.get(
                            "https://api.themoviedb.org/3/search/movie",
                            params={"api_key": TMDB_API_KEY, "query": query, "language": "pl-PL"},
                            timeout=5
                        ).json()
                    if tmdb_resp.get("results"):
                        movie = tmdb_resp["results"][0]
                        if movie.get("overview"):
                            description = movie["overview"]
                        if movie.get("vote_average"):
                            rating = movie["vote_average"]
                        if movie.get("poster_path"):
                            image = f"https://image.tmdb.org/t/p/w300{movie['poster_path']}"
                except Exception as e:
                    print("⚠️ TMDb:", e)

                if len(description) > 200:
                    description = description[:200] + "..."
                results.append({
                    "title": title, "magnet": magnet, "image": image,
                    "description": description, "rating": rating,
                    "size": f"Size {size}", "seeds": seeds, "leeches": leeches
                })
            except Exception as e:
                print(f"⛔️ Wiersz:", e)
                continue
        return jsonify({"results": results, "source": "tpb"})
    except Exception as e:
        print(f"❌ Błąd połączenia: {e}")
        return jsonify({"results": [], "source": "tpb"})

@app.route("/browse", methods=["GET"])
def browse():
    def param(key, default="0"):
        val = request.args.get(key, "").strip()
        return val if val else default

    query = request.args.get("query", "").strip()
    rating = param("rating"); quality = param("quality")
    genre = param("genre").lower(); order_by = param("order").lower()
    year = param("year"); sort_by = param("sort_by"); page = param("page")

    quality = "all" if quality == "0" else quality
    sort_by = "all" if sort_by == "0" else sort_by
    language = "0"
    first_segment = query if query else rating
    path = f"{first_segment}/{quality}/{genre}/{language}/{order_by}/{year}/{sort_by}"
    url = f"https://yts.mx/browse-movies/{path}"
    if page != "1":
        url += f"?page={page}"
    print("✅ Final YTS URL:", url)

    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        movie_wraps = soup.select(".browse-movie-wrap")
        if not movie_wraps:
            return jsonify({"results": [], "message": "Brak wyników."})
        results = []
        for wrap in movie_wraps:
            try:
                title_el = wrap.select_one(".browse-movie-title")
                title = title_el.get_text(strip=True)
                link = title_el["href"]
                img_tag = wrap.select_one("img")
                img = img_tag.get("data-src") or img_tag.get("src", "")
                if img.startswith("/"):
                    img = "https://yts.mx" + img
                img = img.replace("http://", "https://")
                year_text = wrap.select_one(".browse-movie-year").get_text(strip=True)

                display_title = title; description = ""; rating_value = ""
                try:
                    tmdb_resp = requests.get(
                        "https://api.themoviedb.org/3/search/movie",
                        params={"api_key": TMDB_API_KEY, "query": title, "language": "pl-PL"},
                        timeout=5
                    )
                    tmdb_data = tmdb_resp.json()
                    if tmdb_data.get("results"):
                        movie = tmdb_data["results"][0]
                        display_title = movie.get("title", title)
                        description = movie.get("overview", "")
                        rating_value = movie.get("vote_average", "")
                except Exception:
                    pass
                short_description = (description[:200] + "...") if len(description) > 200 else description
                results.append({
                    "title": display_title, "url": link, "image": img,
                    "rating": rating_value, "year": year_text, "description": short_description
                })
            except Exception:
                continue
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500





# ─────────────────────────────────────────────────────────────────────────────
# graceful shutdown
# ─────────────────────────────────────────────────────────────────────────────
def _atexit_hook():
    graceful_shutdown(reason="atexit", hard=False)
atexit.register(_atexit_hook)



# ─────────────────────────────────────────────────────────────────────────────
# graceful shutdown – publiczne API dla launchera i endpoint fallback
# ─────────────────────────────────────────────────────────────────────────────
def graceful_shutdown(reason: str = "unknown", hard: bool = True):
    global _SHUTDOWN_ONCE
    if _SHUTDOWN_ONCE:
        return
    _SHUTDOWN_ONCE = True

    try:
        progress_log.info("graceful_shutdown start (reason=%s)", reason)
    except Exception:
        pass

    # 1) Zatrzymaj poboczne rzeczy (jeśli masz swoje stopery w kodzie, wywołaj je tutaj)

    # 2) Ostatnia synchronizacja i zapisy JSON
    try:
        sync_progress_cache_from_available()          # -> progress_cache.json
    except Exception as e:
        try: progress_log.warning("final sync_progress failed: %s", e)
        except: pass

    try:
        available_cache.apply_overrides_from_progress()  # -> available_cache.json
    except Exception as e:
        try: progress_log.warning("apply_overrides failed: %s", e)
        except: pass

    # (opcjonalnie) dobij zapisu poster_cache.json — zwykle zbędne, ale nie szkodzi
    try:
        with poster_mgr._lock:
            poster_mgr._save()
    except Exception:
        pass

    # Historia (jeśli ma save/flush)
    try:
        if hasattr(history_store, "save"):
            history_store.save()
        elif hasattr(history_store, "flush"):
            history_store.flush()
    except Exception as e:
        try: progress_log.warning("history save failed: %s", e)
        except: pass

    # 3) Klient torrentów – zapis resume/DHT
    try:
        tclient.shutdown()
    except Exception as e:
        try: progress_log.warning("tclient.shutdown failed: %s", e)
        except: pass

    # 4) Wypchnij bufory logów
    try:
        for h in list(progress_log.handlers):
            try: h.flush()
            except: pass
    except Exception:
        pass

    try:
        progress_log.info("graceful_shutdown done")
    except Exception:
        pass

    if hard:
        os._exit(0)


@app.route("/admin/shutdown", methods=["POST"])
def admin_shutdown():
    """
    Fallback dla launchera: HTTP POST /admin/shutdown
    Zwraca od razu 200, a właściwe zamknięcie robi wątkiem, by nie zrywać odpowiedzi.
    """
    threading.Thread(
        target=lambda: (graceful_shutdown("api"), os._exit(0)),
        daemon=True,
        name="shutdown-thread"
    ).start()
    return jsonify({"ok": True, "message": "shutting down"}), 200

if "admin_shutdown" not in app.view_functions:
    @app.route("/admin/shutdown", methods=["POST"])
    def admin_shutdown():
        payload = request.get_json(silent=True) or {}
        reason = str(payload.get("reason") or "http")
        graceful_shutdown(reason=reason, hard=True)
        return jsonify({"ok": True, "reason": reason})
@app.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json', mimetype='application/manifest+json')

@app.route('/sw.js')
def service_worker():
    # UWAGA: SW musi być w scope '/' (czyli bez /static/ w URL)
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')

@app.route('/offline.html')
def offline_page():
    return send_from_directory('static', 'offline.html', mimetype='text/html')

# --- /admin/shutdown (idempotent) ---
def _admin_shutdown_view():
    threading.Thread(
        target=lambda: (graceful_shutdown("http"), time.sleep(0.2), os._exit(0)),
        daemon=True
    ).start()
    return jsonify({"ok": True})

if "admin_shutdown" not in app.view_functions:
    app.add_url_rule("/admin/shutdown", endpoint="admin_shutdown",
                     view_func=_admin_shutdown_view, methods=["POST"])


# zarejestruj endpoint tylko jeśli nie ma go jeszcze w app
if "admin_shutdown" not in app.view_functions:
    app.add_url_rule(
        "/admin/shutdown",
        endpoint="admin_shutdown",
        view_func=_admin_shutdown_view,
        methods=["POST"]
    )


if __name__ == "__main__":
    def _sig_handler(*_):
        graceful_shutdown(reason="signal", hard=True)


    signal.signal(signal.SIGINT, _sig_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _sig_handler)
    if hasattr(signal, "SIGBREAK"):  # Windows
        signal.signal(signal.SIGBREAK, _sig_handler)

    # Onboarding tylko w trybie standalone
    if not config_exists():
        try:
            from gui_onboarding import run_onboarding
            new_cfg = run_onboarding()
            if not new_cfg:
                print("❌ Konfiguracja przerwana – zamykam.")
                sys.exit(1)
            CONFIG.update(new_cfg)
        except Exception as e:
            print(f"❌ Błąd onboardingu: {e}")
            sys.exit(1)

    # Jeśli to standalone i nie ma defer – uruchom wątki w tle normalnie
    if not DEFER_INIT:
        _do_available_bootstrap()
        _start_all_backgrounds()

    # Serwer Flask blokująco (standalone)
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False, threaded=True)






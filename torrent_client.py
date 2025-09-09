import os
import time
import atexit
import threading
from dataclasses import dataclass, asdict
from typing import Dict, Optional, List, Set
import sys, shutil
import libtorrent as lt

# ─────────────────────────────────────────────────────────────────────────────
# ŚCIEŻKI I STAŁE – trwałe w profilu użytkownika (działa w PyInstaller onefile)
# ─────────────────────────────────────────────────────────────────────────────
APP_NAME = "Piotrflix"

def _user_state_root(app_name: str = APP_NAME) -> str:
    if sys.platform.startswith("win"):
        root = os.environ.get("APPDATA") or os.path.expanduser(r"~\\AppData\\Roaming")
    elif sys.platform == "darwin":
        root = os.path.expanduser("~/Library/Application Support")
    else:
        root = os.path.expanduser("~/.local/share")
    return os.path.join(root, app_name)

STATE_DIR = os.path.join(_user_state_root(), "lt")   # katalog na dane libtorrent
RESUME_DIR = os.path.join(STATE_DIR, "resume")
DHT_STATE_FILE = os.path.join(STATE_DIR, "dht_state.dat")

os.makedirs(RESUME_DIR, exist_ok=True)

# (migracja 1x) – jeśli wcześniej było obok exe/źródeł, przenieś pliki
_OLD_BASE = os.path.abspath(getattr(sys, "_MEIPASS", "."))
_OLD_STATE = os.path.join(_OLD_BASE, "state")
_OLD_RESUME = os.path.join(_OLD_STATE, "resume")
_OLD_DHT = os.path.join(_OLD_STATE, "dht_state.dat")

def _migrate_if_needed():
    try:
        # przenieś *.fastresume
        if os.path.isdir(_OLD_RESUME):
            for fname in os.listdir(_OLD_RESUME):
                if not fname.endswith(".fastresume"):
                    continue
                src = os.path.join(_OLD_RESUME, fname)
                dst = os.path.join(RESUME_DIR, fname)
                if not os.path.exists(dst):
                    try:
                        os.replace(src, dst)
                    except Exception:
                        shutil.copy2(src, dst)
        # przenieś dht_state.dat
        if os.path.isfile(_OLD_DHT) and not os.path.exists(DHT_STATE_FILE):
            try:
                os.replace(_OLD_DHT, DHT_STATE_FILE)
            except Exception:
                shutil.copy2(_OLD_DHT, DHT_STATE_FILE)
    except Exception:
        pass

_migrate_if_needed()

RESUME_FLUSH_INTERVAL = 30  # co ile sekund robimy checkpoint resume
LISTEN_PORTS = (6881, 6891)

# ─────────────────────────────────────────────────────────────────────────────
# USTAWIENIA SESJI
# ─────────────────────────────────────────────────────────────────────────────
_SETTINGS = {
    "enable_dht": True,
    "enable_lsd": True,
    "enable_upnp": True,
    "enable_natpmp": True,
    "rate_limit_ip_overhead": True,
    "aio_threads": 8,
    "checking_mem_usage": 128,
    "allow_multiple_connections_per_ip": True,
    "out_enc_policy": lt.enc_policy.forced,
    "in_enc_policy": lt.enc_policy.forced,
    "prefer_rc4": False,
}


@dataclass
class TorrentInfo:
    id: str
    name: str
    progress: float
    state: str
    download_payload_rate: int
    eta: int
    download_location: str

    def as_json(self):
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Historia – prosty plikowy store
# ─────────────────────────────────────────────────────────────────────────────
class HistoryStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        if not os.path.exists(self.path):
            self._write([])

    def _read(self) -> List[dict]:
        import json
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return []

        if isinstance(raw, dict):
            raw = [raw]
        elif isinstance(raw, str):
            return []

        out: List[dict] = []
        if isinstance(raw, list):
            import json as _json
            for item in raw:
                if isinstance(item, dict):
                    out.append(item)
                elif isinstance(item, str):
                    try:
                        obj = _json.loads(item)
                        if isinstance(obj, dict):
                            out.append(obj)
                    except Exception:
                        pass
        return out

    def _write(self, data: List[dict]):
        import json
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)

    def add(self, entry: dict):
        data = self._read()
        data.append(entry)
        self._write(data)

    def get(self) -> List[dict]:
        return self._read()

    def clear(self):
        self._write([])


# ─────────────────────────────────────────────────────────────────────────────
# Główny klient
# ─────────────────────────────────────────────────────────────────────────────
class TorrentClient:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self.ses = lt.session()
        self.ses.listen_on(*LISTEN_PORTS)

        # ustawienia
        sett = self.ses.get_settings()
        if isinstance(sett, dict):
            new_sett = dict(sett)
            new_sett.update(_SETTINGS)
            self.ses.apply_settings(new_sett)
        else:
            for k, v in _SETTINGS.items():
                if hasattr(sett, k):
                    setattr(sett, k, v)
            self.ses.apply_settings(sett)

        # DHT
        try:
            self.ses.add_dht_router("router.bittorrent.com", 6881)
            self.ses.add_dht_router("router.utorrent.com", 6881)
            self.ses.add_dht_router("dht.transmissionbt.com", 6881)
            self._load_dht_state()
            self.ses.start_dht()
        except Exception:
            pass

        self.history: Optional[HistoryStore] = None
        self._finished_ids: Set[str] = set()   # ID zakończonych (aby nie dublować historii)
        self._name_cache: Dict[str, str] = {}  # stabilna nazwa zanim metadata wróci

        # wątki
        self._alerts_thread = threading.Thread(target=self._alerts_loop, daemon=True, name="lt-alerts")
        self._alerts_thread.start()

        self._checkpoint_thread = threading.Thread(target=self._periodic_resume_checkpoint, daemon=True, name="lt-checkpoint")
        self._checkpoint_thread.start()

        # wznów wszystkie znane torrenty
        self._load_all_resume()

        # elegancki shutdown
        atexit.register(self.shutdown)

    @classmethod
    def get(cls) -> "TorrentClient":
        with cls._lock:
            if cls._instance is None:
                cls._instance = TorrentClient()
            return cls._instance

    # ─────────────────────────────────────────────────────────────────────
    # API publiczne
    # ─────────────────────────────────────────────────────────────────────
    def set_history_store(self, store: HistoryStore):
        """Podpinamy plik historii i ładujemy listę zakończonych ID."""
        self.history = store
        try:
            for e in self.history.get():
                if isinstance(e, dict) and e.get("event") == "finished" and e.get("id"):
                    self._finished_ids.add(e["id"])
        except Exception:
            pass

    def get_history(self) -> List[dict]:
        return self.history.get() if self.history else []

    def add_magnet(self, magnet: str, download_path: str) -> Optional[str]:
        os.makedirs(download_path, exist_ok=True)
        params = lt.add_torrent_params()
        params.save_path = download_path
        params.url = magnet
        params.flags |= lt.torrent_flags.auto_managed  # start od razu

        h = self.ses.add_torrent(params)
        try:
            h.resume()
        except Exception:
            pass

        ih = _handle_info_hash_hex(h)
        for _ in range(50):  # do ~5s na nadanie hash
            if ih:
                break
            time.sleep(0.1)
            ih = _handle_info_hash_hex(h)

        # cache wstępnej nazwy (jeśli libtorrent cokolwiek zwraca)
        try:
            nm = (h.name() if hasattr(h, "name") else "") or ""
            if nm and ih:
                self._name_cache[ih] = nm
        except Exception:
            pass

        # pierwszy zapis fastresume
        try:
            h.save_resume_data(lt.torrent_handle.save_info_dict)
        except Exception:
            pass

        return ih

    def get_torrents(self) -> Dict[str, TorrentInfo]:
        result: Dict[str, TorrentInfo] = {}

        for h in self.ses.get_torrents():
            ih = _handle_info_hash_hex(h)
            if not ih:
                continue

            st = h.status()

            # nazwa – zawsze zwróć niepustą
            name = (getattr(st, "name", "") or "").strip()
            if not name:
                try:
                    name = getattr(h, "name", lambda: "")() or ""
                except Exception:
                    name = ""
            if not name:
                name = self._name_cache.get(ih, "")
            if not name:
                name = ih  # ostateczny fallback – nigdy nie zwrócimy ""

            # zapamiętaj gdy już się pojawi
            if name and ih not in self._name_cache and name != ih:
                self._name_cache[ih] = name

            progress = round(float(getattr(st, "progress", 0.0) or 0.0) * 100, 1)
            rate = int(getattr(st, "download_payload_rate", 0) or 0)
            eta = _calc_eta(st)
            state = _map_state(st) or "Unknown"
            save_path = getattr(st, "save_path", "") or ""

            # Auto-pauza + historia (awaryjnie także tutaj)
            if progress >= 100.0:
                if not _status_is_paused(st):
                    try:
                        h.pause()
                        state = "Paused"
                        try:
                            h.save_resume_data(lt.torrent_handle.save_info_dict)
                        except Exception:
                            pass
                    except Exception:
                        pass
                self._maybe_log_finished(ih, name, save_path)

            result[ih] = TorrentInfo(
                id=ih,
                name=name,
                progress=progress,
                state=state,
                download_payload_rate=rate,
                eta=eta,
                download_location=save_path,
            )

        return result

    def get_torrent(self, torrent_id: str) -> Optional[lt.torrent_handle]:
        for h in self.ses.get_torrents():
            if _handle_info_hash_hex(h) == torrent_id:
                return h
        return None

    def pause(self, torrent_id: str) -> bool:
        h = self.get_torrent(torrent_id)
        if not h:
            return False
        h.pause()
        try:
            h.save_resume_data(lt.torrent_handle.save_info_dict)
        except Exception:
            pass
        return True

    def resume(self, torrent_id: str) -> bool:
        h = self.get_torrent(torrent_id)
        if not h:
            return False
        h.resume()
        return True

    def remove(self, torrent_id: str, remove_data: bool = False) -> bool:
        h = self.get_torrent(torrent_id)
        if not h:
            return False
        flags = lt.options_t.delete_files if remove_data else lt.options_t.none
        self._delete_resume_file(torrent_id)
        self.ses.remove_torrent(h, flags)
        return True

    def set_global_download_limit(self, kib_per_sec: int):
        # KiB/s -> B/s, <=0 oznacza brak limitu
        bps = 0 if (kib_per_sec is None or kib_per_sec <= 0) else int(kib_per_sec * 1024)

        # 1) jeśli sesja ma prostą metodę - użyj jej (działa w wielu wersjach)
        if hasattr(self.ses, "set_download_rate_limit"):
            try:
                self.ses.set_download_rate_limit(bps)
            except Exception:
                pass
        else:
            # 2) w przeciwnym razie aktualizuj settings (obsługa dict i settings_pack)
            try:
                sett = self.ses.get_settings()
                if isinstance(sett, dict):
                    sett["download_rate_limit"] = bps
                    self.ses.apply_settings(sett)
                else:
                    try:
                        # część bindingów pozwala na atrybut
                        setattr(sett, "download_rate_limit", bps)
                        self.ses.apply_settings(sett)
                    except Exception:
                        # a część wymaga nowego settings_pack i set_int()
                        try:
                            sp = lt.settings_pack()
                            # jeśli dostępne set_int + stała:
                            if hasattr(sp, "set_int") and hasattr(lt.settings_pack, "download_rate_limit"):
                                sp.set_int(lt.settings_pack.download_rate_limit, bps)
                            else:
                                # fallback: przypisanie atrybutu
                                sp.download_rate_limit = bps
                            self.ses.apply_settings(sp)
                        except Exception:
                            pass
            except Exception:
                pass

        # 3) opcjonalnie per-torrent (żeby natychmiast zadziałało na istniejących handle’ach)
        for h in self.ses.get_torrents():
            try:
                h.set_download_limit(bps)  # 0 == bez limitu
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────────────
    # Zamknięcie – flush wszystkiego
    # ─────────────────────────────────────────────────────────────────────
    def shutdown(self):
        try:
            self._save_dht_state()
        except Exception:
            pass
        try:
            self._save_all_resume_blocking(timeout=8.0)
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────
    # Resume / DHT
    # ─────────────────────────────────────────────────────────────────────
    def _resume_path_for(self, info_hash_hex: str) -> str:
        return os.path.join(RESUME_DIR, f"{info_hash_hex}.fastresume")

    def _write_resume_file(self, ih_hex: str, buf: bytes):
        path = self._resume_path_for(ih_hex)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(buf)
        os.replace(tmp, path)

    def _delete_resume_file(self, ih_hex: str):
        try:
            path = self._resume_path_for(ih_hex)
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def _load_all_resume(self):
        files = [f for f in os.listdir(RESUME_DIR) if f.endswith(".fastresume")]
        loaded = 0
        for fname in files:
            fpath = os.path.join(RESUME_DIR, fname)
            try:
                with open(fpath, "rb") as f:
                    data = f.read()

                params = None
                try:
                    params = lt.read_resume_data(data)
                except Exception:
                    try:
                        decoded = lt.bdecode(data)
                        params = lt.read_resume_data(lt.bencode(decoded))
                    except Exception as e:
                        print(f"⚠️ resume decode error for {fname}: {e}")
                        continue

                if not getattr(params, "save_path", None):
                    continue

                params.flags |= lt.torrent_flags.auto_managed
                h = self.ses.add_torrent(params)
                try:
                    h.resume()
                except Exception:
                    pass
                loaded += 1
            except Exception as e:
                print(f"⚠️ resume load error for {fname}: {e}")
        if loaded:
            print(f"🔁 Przywrócono {loaded} torrentów z resume")

    def _save_all_resume_blocking(self, timeout: float = 5.0):
        # poproś o zapis
        handles = list(self.ses.get_torrents())
        for h in handles:
            try:
                h.save_resume_data(lt.torrent_handle.save_info_dict)
            except Exception:
                pass

        # czekamy aż przyjdą alerty zapisu
        end = time.time() + timeout
        while time.time() < end:
            wrote = self._consume_resume_alerts_once()
            if wrote == 0:
                time.sleep(0.1)
            else:
                time.sleep(0.05)

    def _save_dht_state(self):
        try:
            st = self.ses.save_state(lt.save_state_flags_t.save_dht_state)
            buf = lt.bencode(st)
            with open(DHT_STATE_FILE, "wb") as f:
                f.write(buf)
        except Exception as e:
            print(f"⚠️ DHT save error: {e}")

    def _load_dht_state(self):
        try:
            if not os.path.exists(DHT_STATE_FILE):
                return
            with open(DHT_STATE_FILE, "rb") as f:
                data = f.read()
            st = lt.bdecode(data)
            self.ses.load_state(st)
        except Exception as e:
            print(f"⚠️ DHT load error: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # Alerty / checkpoint
    # ─────────────────────────────────────────────────────────────────────
    def _periodic_resume_checkpoint(self):
        while True:
            try:
                self._save_all_resume_blocking(timeout=2.5)
            except Exception:
                pass
            time.sleep(RESUME_FLUSH_INTERVAL)

    def _maybe_log_finished(self, ih: str, name: str, path: str):
        """Dodaj wpis 'finished' tylko raz dla danego torrenta."""
        if ih and ih not in self._finished_ids and self.history is not None:
            try:
                self.history.add({
                    "ts": int(time.time()),
                    "id": ih,
                    "name": name or ih,
                    "path": path or "",
                    "event": "finished"
                })
                self._finished_ids.add(ih)
            except Exception:
                pass

    def _consume_resume_alerts_once(self) -> int:
        """
        Zbiera z kolejki tylko alerty związane z resume + zakończeniem + metadanymi.
        Zwraca liczbę zapisanych plików resume.
        """
        saved = 0
        for alert in self.ses.pop_alerts():
            # zapis resume
            if isinstance(alert, lt.save_resume_data_alert):
                h = alert.handle
                ih = _handle_info_hash_hex(h)
                if not ih:
                    continue
                try:
                    buf = _resume_to_bytes(getattr(alert, "params", {}))
                    if buf:
                        self._write_resume_file(ih, buf)
                        saved += 1
                except Exception as e:
                    print(f"⚠️ resume write error {ih}: {e}")

            # metadata -> uzupełnij nazwę
            elif hasattr(lt, "metadata_received_alert") and isinstance(alert, lt.metadata_received_alert):
                try:
                    ih = _handle_info_hash_hex(alert.handle)
                    if ih:
                        st = alert.handle.status()
                        nm = (getattr(st, "name", "") or "") or (alert.handle.name() if hasattr(alert.handle, "name") else "")
                        if nm:
                            self._name_cache[ih] = nm
                except Exception:
                    pass

            # nowy torrent -> spróbuj odczytać nazwę
            elif hasattr(lt, "add_torrent_alert") and isinstance(alert, lt.add_torrent_alert):
                try:
                    ih = _handle_info_hash_hex(alert.handle)
                    if ih:
                        nm = (alert.handle.name() if hasattr(alert.handle, "name") else "") or ""
                        if nm:
                            self._name_cache[ih] = nm
                except Exception:
                    pass

            # ukończony torrent → auto-pauza, historia, resume
            elif isinstance(alert, lt.torrent_finished_alert):
                try:
                    h = alert.handle
                    ih = _handle_info_hash_hex(h) or ""
                    st = h.status()
                    name = (getattr(st, "name", "") or "") or (h.name() if hasattr(h, "name") else "") or ih
                    path = getattr(st, "save_path", "") or ""
                    if not _status_is_paused(st):
                        try:
                            h.pause()
                        except Exception:
                            pass
                    self._maybe_log_finished(ih, name, path)
                    try:
                        h.save_resume_data(lt.torrent_handle.save_info_dict)
                    except Exception:
                        pass
                except Exception:
                    pass

            # po pauzie – doraźny zapis resume
            elif isinstance(alert, lt.torrent_paused_alert):
                try:
                    alert.handle.save_resume_data(lt.torrent_handle.save_info_dict)
                except Exception:
                    pass

            # log błędów do historii
            elif isinstance(alert, lt.torrent_error_alert):
                try:
                    h = alert.handle
                    ih = _handle_info_hash_hex(h) or ""
                    name = (h.status().name or "") or ih
                    if self.history:
                        self.history.add({
                            "ts": int(time.time()),
                            "id": ih,
                            "name": name,
                            "path": h.status().save_path,
                            "event": "error",
                            "message": alert.message()
                        })
                except Exception:
                    pass

        return saved

    def _alerts_loop(self):
        while True:
            try:
                self._consume_resume_alerts_once()
            except Exception:
                pass
            time.sleep(0.2)


# ─────────────────────────────────────────────────────────────────────────────
# POMOCNICZE
# ─────────────────────────────────────────────────────────────────────────────
def _settings_from_dict(current, overrides: dict) -> dict:
    valid = {}
    for k, v in overrides.items():
        if hasattr(current, k):
            valid[k] = v
    return valid


def _handle_info_hash_hex(h: lt.torrent_handle) -> Optional[str]:
    try:
        if hasattr(h, "info_hashes"):
            ihs = h.info_hashes()
            for attr in ("v1", "v2"):
                ih = getattr(ihs, attr, None)
                if ih:
                    if hasattr(ih, "to_bytes"):
                        return ih.to_bytes().hex()
                    if hasattr(ih, "to_string"):
                        return ih.to_string().hex()
        if hasattr(h, "info_hash"):
            ih = h.info_hash()
            if hasattr(ih, "to_bytes"):
                return ih.to_bytes().hex()
            if hasattr(ih, "to_string"):
                return ih.to_string().hex()
    except Exception:
        pass
    return None


def _status_is_paused(st: lt.torrent_status) -> bool:
    """Bezpieczne sprawdzenie pauzy dla różnych wersji libtorrent."""
    # 1) niektóre buildy mają .is_paused
    if hasattr(st, "is_paused"):
        try:
            return bool(st.is_paused)
        except Exception:
            pass
    # 2) inne mają .paused
    if hasattr(st, "paused"):
        try:
            return bool(st.paused)
        except Exception:
            pass
    # 3) flaga w st.flags
    flags = getattr(st, "flags", 0)
    try:
        return bool(flags & lt.torrent_flags.paused)
    except Exception:
        return False


def _map_state(st: lt.torrent_status) -> str:
    if getattr(st, "errc", None) and st.errc.value() != 0:
        return "Error"
    if _status_is_paused(st):
        return "Paused"
    mapping = {
        lt.torrent_status.checking_files: "Checking",
        lt.torrent_status.downloading_metadata: "Fetching metadata",
        lt.torrent_status.downloading: "Downloading",
        lt.torrent_status.finished: "Seeding",
        lt.torrent_status.seeding: "Seeding",
        lt.torrent_status.allocating: "Allocating",
        lt.torrent_status.checking_resume_data: "Checking",
    }
    return mapping.get(getattr(st, "state", None), "Unknown")


def _calc_eta(st: lt.torrent_status) -> int:
    rate = int(getattr(st, "download_payload_rate", 0) or 0)
    total = int(getattr(st, "total_wanted", 0) or 0)
    done = int(getattr(st, "total_wanted_done", 0) or 0)
    if rate > 0 and total > 0 and total > done:
        return int((total - done) / rate)
    return -1


def _resume_to_bytes(obj) -> bytes:
    """
    Zwraca zawsze bytes:
    - jeśli obj jest już bytes/bytearray -> zwraca
    - jeśli obj jest dict -> bencode(dict)
    - jeśli to add_torrent_params -> spróbuj write_resume_data(), a jak da dict to bencode
    - fallback: bencode(obj) jeśli się da
    """
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj)

    try:
        buf = lt.write_resume_data(obj)  # może zwrócić bytes albo dict
        if isinstance(buf, (bytes, bytearray)):
            return bytes(buf)
        if isinstance(buf, dict):
            return lt.bencode(buf)
    except Exception:
        pass

    if isinstance(obj, dict):
        return lt.bencode(obj)

    try:
        return lt.bencode(obj)
    except Exception:
        return b""



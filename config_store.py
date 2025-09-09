# config_store.py
import os, sys, json, threading

APP_NAME = "Piotrflix"

def _user_appdata_dir(app_name: str = APP_NAME) -> str:
    if sys.platform.startswith("win"):
        root = os.environ.get("APPDATA") or os.path.expanduser(r"~\\AppData\\Roaming")
    elif sys.platform == "darwin":
        root = os.path.expanduser("~/Library/Application Support")
    else:
        root = os.path.expanduser("~/.local/share")
    p = os.path.join(root, app_name)
    os.makedirs(p, exist_ok=True)
    return p

CONFIG_PATH = os.path.join(_user_appdata_dir(), "config.json")
_LOCK = threading.Lock()

DEFAULTS = {
    "paths": {
        "movies": "",   # wybrane przez usera
        "series": ""    # wybrane przez usera
    },
    "plex": {
        "base_url": "",  # np. http://192.168.1.224:32400
        "token": ""      # pobrany z OAuth PIN
    }
}

def load_config() -> dict:
    try:
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # defensywnie uzupełnij brakujące klucze
            out = DEFAULTS.copy()
            out["paths"] = {**DEFAULTS["paths"], **(data.get("paths") or {})}
            out["plex"]  = {**DEFAULTS["plex"],  **(data.get("plex")  or {})}
            return out
    except Exception:
        pass
    return DEFAULTS.copy()

def save_config(cfg: dict):
    data = {
        "paths": {
            "movies": (cfg.get("paths") or {}).get("movies", ""),
            "series": (cfg.get("paths") or {}).get("series", ""),
        },
        "plex": {
            "base_url": (cfg.get("plex") or {}).get("base_url", ""),
            "token": (cfg.get("plex") or {}).get("token", ""),
        },
    }
    tmp = CONFIG_PATH + ".tmp"
    with _LOCK:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_PATH)

def config_exists() -> bool:
    return os.path.isfile(CONFIG_PATH)

from flask import Flask, render_template, request, redirect, url_for, jsonify
from deluge_client import DelugeRPCClient
from bs4 import BeautifulSoup
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
import re
import hashlib
from flask import Flask, render_template
import json
import sys
import threading
import subprocess
import os
from datetime import datetime
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
import time
from xml.etree import ElementTree
from plexapi.server import PlexServer
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc

if getattr(sys, 'frozen', False):
    base_path = sys._MEIPASS
else:
    base_path = os.path.abspath(".")

# 📁 Ścieżki plików i katalogów
PROGRESS_CACHE_FILE = os.path.join(base_path, "progress_cache.json")
POSTER_DIR = os.path.join(base_path, "static", "posters")
POSTER_CACHE_FILE = os.path.join(base_path, "poster_cache.json")
PROGRESS_REFRESH_INTERVAL = 60 * 30  # 30 minut

# 🚀 Flask app
app = Flask(
    __name__,
    template_folder=os.path.join(base_path, 'templates'),
    static_folder=os.path.join(base_path, 'static')
)

# 🛡️ Upewnij się, że folder i plik cache istnieją
os.makedirs(POSTER_DIR, exist_ok=True)

if not os.path.exists(POSTER_CACHE_FILE):
    with open(POSTER_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f)

# 🔧 Deluge config
DELUGE_HOST = "127.0.0.1"
DELUGE_PORT = 58846
DELUGE_USER = "magnetman"
DELUGE_PASS = "qwerty123"

# 🔑 TMDb + Plex config
TMDB_API_KEY = "5471a26860fd4401b09ebf325ab6b4fb"
PLEX_URL = "http://192.168.1.224:32400"
TOKEN = "s7f2-x71kLuXF5xikzBd"


def cleanup_unused_posters():
    print("🧹 Czyszczenie nieużywanych plakatów...")

    if not os.path.exists(POSTER_CACHE_FILE):
        print("⚠️ Brak pliku poster_cache.json")
        return

    with open(POSTER_CACHE_FILE, "r", encoding="utf-8") as f:
        used_posters = set(json.load(f).values())

    if not os.path.isdir(POSTER_DIR):
        print("⚠️ Folder plakatów nie istnieje")
        return

    deleted = 0
    for fname in os.listdir(POSTER_DIR):
        fpath = os.path.join(POSTER_DIR, fname)
        relative_url = f"/static/posters/{fname}"

        if relative_url not in used_posters:
            try:
                os.remove(fpath)
                deleted += 1
                print(f"🗑️ Usunięto: {fname}")
            except Exception as e:
                print(f"❌ Błąd usuwania {fname}: {e}")

    print(f"✅ Usunięto {deleted} nieużywanych plakatów.")

def load_poster_cache():
    if os.path.exists(POSTER_CACHE_FILE):
        with open(POSTER_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_poster_cache(cache):
    with open(POSTER_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)

def get_cached_poster(title, url):
    filename = f"{title.lower().replace(' ', '_')}.jpg"
    path = os.path.join(POSTER_DIR, filename)

    if os.path.exists(path):
        return f"/static/posters/{filename}"

    try:
        response = requests.get(url)
        if response.status_code == 200:
            with open(path, "wb") as f:
                f.write(response.content)
            return f"/static/posters/{filename}"
    except Exception as e:
        print(f"⚠️ Nie udało się pobrać plakatu: {e}")

    return ""  # lub placeholder

def fix_windows_path(path):
    # Jeśli ścieżka zaczyna się od /mch/ lub coś podobnego — zamień na dysk Z
    if path.startswith("/mch/") or path.startswith("/mnt/") or path.startswith("/media/"):
        fixed = path.replace("/", "\\")
        fixed = fixed.replace("\\mch\\", "Z:\\")
        fixed = fixed.replace("\\mnt\\", "Z:\\")
        fixed = fixed.replace("\\media\\", "Z:\\")
        return fixed
    return path


def map_network_drive():
    if os.name == "nt":  # tylko na Windowsie
        try:
            result = subprocess.run(
                ['net', 'use', 'Z:', r'\\MYCLOUD-00A2RY\kawjorek', '/persistent:no'],
                shell=True,
                capture_output=True,
                text=True
            )
            print(f"🔌 Mapowanie dysku Z: – {result.stdout or result.stderr}")
        except Exception as e:
            print(f"❌ Błąd mapowania dysku: {e}")

def log_cleanup_entry(title, media_type, path):
    log_line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ✅ Usunięto: {title} ({media_type}) – {path}\n"
    with open("cleanup.log", "a", encoding="utf-8") as log_file:
        log_file.write(log_line)

def run_cleanup_loop(interval_minutes=60):
    def loop():
        while True:
            try:
                print("🔁 Automatyczny cleanup...")
                with app.app_context():
                    cleanup_old_media()
            except Exception as e:
                print(f"❌ Błąd automatycznego cleanupu: {e}")
            time.sleep(interval_minutes * 60)
    threading.Thread(target=loop, daemon=True).start()

# 🚀 Start pętli cleanup po załadowaniu definicji
run_cleanup_loop(60)

def load_progress_cache():
    if os.path.exists(PROGRESS_CACHE_FILE):
        with open(PROGRESS_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_progress_cache(data):
    with open(PROGRESS_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_client():
    client = DelugeRPCClient(DELUGE_HOST, DELUGE_PORT, DELUGE_USER, DELUGE_PASS)
    client.connect()
    return client

def get_tmdb_poster(title, type_="movie"):
    try:
        # 📁 Upewnij się, że folder istnieje
        os.makedirs(POSTER_DIR, exist_ok=True)

        # 🧠 Generuj nazwę pliku z hasha (unikalność i brak problemów z polskimi znakami)
        safe_filename = hashlib.sha1(title.lower().encode('utf-8')).hexdigest() + ".jpg"
        local_path = os.path.join(POSTER_DIR, safe_filename)

        # 🧃 Jeśli już istnieje – zwróć lokalny link
        if os.path.exists(local_path):
            return f"/static/posters/{safe_filename}"

        # 🌐 Zapytanie do TMDb
        resp = requests.get(
            f"https://api.themoviedb.org/3/search/{type_}",
            params={"api_key": TMDB_API_KEY, "query": title, "language": "pl-PL"},
            timeout=5
        )
        data = resp.json()

        # 📥 Pobieranie plakatu
        if data.get("results"):
            poster_path = data["results"][0].get("poster_path")
            if poster_path:
                poster_url = f"https://image.tmdb.org/t/p/w342{poster_path}"
                img_data = requests.get(poster_url).content
                with open(local_path, "wb") as f:
                    f.write(img_data)
                return f"/static/posters/{safe_filename}"
    except Exception as e:
        print(f"❌ Błąd TMDb dla: {title} –", e)

    return ""


@app.route("/search-series", methods=["POST"])
def search_series():
    import re

    query = request.form.get("query", "").strip()
    if not query:
        return jsonify({"results": [], "source": "tpb"})

    # 🔤 Tłumaczenie tytułu z PL na EN (TMDb, tak samo jak w /search)
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
    print(f"➡️ URL: {search_url}")

    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(search_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return jsonify({"results": [], "source": "tpb"})

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table#searchResult tr")
        print(f"📦 Znaleziono wierszy: {len(rows)}")

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

                size = "Nieznany rozmiar"
                seeds = leeches = "?"
                if len(columns) >= 3:
                    size = columns[0].text.strip()
                    seeds = columns[1].text.strip()
                    leeches = columns[2].text.strip()

                # 🔍 Przygotuj tytuł do TVmaze
                clean_title = re.sub(
                    r"(S\d+E\d+)|(\d{3,4}p)|x26[45]|BluRay|WEB[- ]?DL|AMZN|NF|DDP\d|COMPLETE|Season \d+|H\.?264|HEVC|BRRip|HDRip|AVS|PSA|\[.*?\]|\.|-|_",
                    " ",
                    title, flags=re.IGNORECASE
                )
                clean_title = re.sub(r"\s+", " ", clean_title).strip()

                # Użyj tylko pierwszych 3 słów
                cleaned_words = clean_title.split()
                title_for_tvmaze = " ".join(cleaned_words[:3])

                print("📡 Zapytanie do TVmaze:", title_for_tvmaze)

                image = "/static/logo.png"
                description = ""
                rating = ""

                # 🧠 Fallback: najpierw z oczyszczonym tytułem
                try:
                    tvmaze_resp = requests.get("https://api.tvmaze.com/search/shows", params={"q": title_for_tvmaze}, timeout=5)
                    tvmaze_data = tvmaze_resp.json()
                except Exception as e:
                    print("⚠️ Błąd połączenia z TVmaze:", e)
                    tvmaze_data = []

                # Fallback jeśli nie znaleziono
                if not tvmaze_data:
                    print("🔁 Fallback do query:", query)
                    tvmaze_resp = requests.get("https://api.tvmaze.com/search/shows", params={"q": query}, timeout=5)
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

                results.append({
                    "title": title,
                    "magnet": magnet,
                    "image": image,
                    "description": description,
                    "rating": rating,
                    "size": f"Size {size}",
                    "seeds": seeds,
                    "leeches": leeches
                })

                print(f"✅ Dodano: {title} [{size}] – Seeds: {seeds}, Leeches: {leeches}")

            except Exception as e:
                print(f"⛔️ Błąd wiersza: {e}")
                continue

        return jsonify({"results": results, "source": "tpb"})

    except Exception as e:
        print(f"❌ Błąd połączenia: {e}")
        return jsonify({"results": [], "source": "tpb"})


""
""
@app.route("/search-premium", methods=["POST"])
def search_premium():
    import re

    query = request.form.get("query", "").strip()
    if not query:
        return jsonify({"results": [], "source": "tpb"})

    # 🔤 Tłumaczenie tytułu z PL na EN (TMDb, jak w search-series)
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
    print(f"➡️ URL: {search_url}")

    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(search_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return jsonify({"results": [], "source": "tpb"})

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table#searchResult tr")
        print(f"📦 Znaleziono wierszy: {len(rows)}")

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

                size = "Nieznany rozmiar"
                seeds = leeches = "?"
                if len(columns) >= 3:
                    size = columns[0].text.strip()
                    seeds = columns[1].text.strip()
                    leeches = columns[2].text.strip()

                clean_title = re.sub(
                    r"(S\d+E\d+)|(\d{3,4}p)|x26[45]|BluRay|WEB[- ]?DL|AMZN|NF|DDP\d|COMPLETE|Season \d+|H\.?264|HEVC|BRRip|HDRip|AVS|PSA|\[.*?\]|\.|-|_",
                    " ",
                    title, flags=re.IGNORECASE
                )
                clean_title = re.sub(r"\s+", " ", clean_title).strip()

                # Użyj tylko pierwszych 3 słów
                cleaned_words = clean_title.split()
                title_for_tmdb = " ".join(cleaned_words[:3])

                print("📡 Zapytanie do TMDb:", title_for_tmdb)

                image = "/static/logo.png"
                description = f"Rozmiar: {size}"
                rating = ""

                try:
                    tmdb_resp = requests.get(
                        "https://api.themoviedb.org/3/search/movie",
                        params={"api_key": TMDB_API_KEY, "query": title_for_tmdb, "language": "pl-PL"},
                        timeout=5
                    ).json()
                    if not tmdb_resp.get("results"):
                        print("🔁 Fallback do query:", query)
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
                    print("⚠️ Błąd pobierania danych TMDb:", e)

                # Skracanie opisu do 200 znaków
                if len(description) > 200:
                    description = description[:200] + "..."

                results.append({
                    "title": title,
                    "magnet": magnet,
                    "image": image,
                    "description": description,
                    "rating": rating,
                    "size": f"Size {size}",
                    "seeds": seeds,
                    "leeches": leeches
                })

                print(f"✅ Dodano: {title} [{size}] – Seeds: {seeds}, Leeches: {leeches}")

            except Exception as e:
                print(f"⛔️ Błąd wiersza: {e}")
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

    # Fraza do przeszukania tytułu
    query = request.args.get("query", "").strip()
    rating = param("rating")
    quality = param("quality")
    genre = param("genre").lower()
    order_by = param("order").lower()
    year = param("year")
    sort_by = param("sort_by")
    page = param("page")

    # Zamiana "0" na "all" tam gdzie YTS wymaga
    quality = "all" if quality == "0" else quality
    sort_by = "all" if sort_by == "0" else sort_by

    # Język zawsze "0"
    language = "0"

    # Jeśli użytkownik wpisał frazę (query), to ona zastępuje 'rating' w URL
    # (tak jak robi to YTS)
    first_segment = query if query else rating

    # Budowanie ścieżki URL
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

                # TMDb (opcjonalnie)
                display_title = title
                description = ""
                rating_value = ""
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
                    "title": display_title,
                    "url": link,
                    "image": img,
                    "rating": rating_value,
                    "year": year_text,
                    "description": short_description
                })
            except Exception:
                continue

        return jsonify({"results": results})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        magnet = request.form.get("magnet")
        source = request.form.get("source", "default")

        if magnet:
            try:
                client = get_client()

                # Ścieżka zapisu
                if source == "series":
                    download_path = r"\\MYCLOUD-00A2RY\kawjorek\Plex\Seriale"
                else:
                    download_path = r"\\MYCLOUD-00A2RY\kawjorek\Plex\Filmy"

                # 📌 Lista ID przed dodaniem
                before = set(client.call("core.get_torrents_status", {}, ["name"]).keys())

                # ➕ Dodaj magnet
                client.call("core.add_torrent_magnet", magnet, {
                    b"download_location": download_path.encode("utf-8")
                })
                print(f"📥 Dodano torrent do: {download_path}")

                # ⏱️ Poczekaj chwilę aż Deluge zarejestruje nowy torrent
                time.sleep(3)

                # 📌 Lista ID po dodaniu
                after = client.call("core.get_torrents_status", {}, ["name"])
                new_ids = set(after.keys()) - before

                if new_ids:
                    new_id = list(new_ids)[0].decode()
                    new_name = after[list(new_ids)[0]][b"name"].decode()
                    print(f"✅ Nowy torrent: {new_name} (ID: {new_id})")
                else:
                    print("⚠️ Nie udało się znaleźć nowego ID torrenta.")

            except Exception as e:
                print("❌ Błąd dodawania torrenta:", e)

        return redirect(url_for("index"))

    return render_template("index.html")
@app.route("/status")
def status():
    try:
        client = get_client()
        fields = ["name", "progress", "state", "download_payload_rate", "eta", "download_location"]
        torrents_raw = client.call("core.get_torrents_status", {}, fields)
        torrents = {}

        for tid, data in torrents_raw.items():
            decoded = {
                k.decode(): v.decode() if isinstance(v, bytes) else v
                for k, v in data.items()
            }

            tid_decoded = tid.decode() if isinstance(tid, bytes) else tid

            is_series = "Seriale" in decoded["download_location"]

            if decoded.get("progress", 0) >= 100 and decoded.get("state") not in ["Paused", "Error"]:
                client.call("core.pause_torrent", [tid])
                decoded["state"] = "Paused"
            torrents[tid_decoded] = decoded

        return jsonify(torrents)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/toggle/<torrent_id>", methods=["POST"])
def toggle_torrent(torrent_id):
    try:
        client = get_client()
        status = client.call("core.get_torrent_status", torrent_id.encode(), ["state"])
        current_state = status[b"state"].decode()
        if current_state == "Paused":
            client.call("core.resume_torrent", [torrent_id])
        else:
            client.call("core.pause_torrent", [torrent_id])
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/remove/<torrent_id>", methods=["POST"])
def remove_torrent(torrent_id):
    remove_data = request.args.get("data") == "true"
    try:
        client = get_client()
        client.call("core.remove_torrent", torrent_id.encode(), remove_data)
        return jsonify({"status": "removed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

    def get_active_sessions():
        url = f"{PLEX_URL}/status/sessions?X-Plex-Token={TOKEN}"
        res = requests.get(url)
        if res.status_code == 200:
            tree = ElementTree.fromstring(res.content)
            for video in tree.findall("Video"):
                title = video.attrib.get("title")
                view_offset = int(video.attrib.get("viewOffset", 0))
                duration = int(video.attrib.get("duration", 1))
                percent = round((view_offset / duration) * 100, 1)
                print(f"🎬 {title} – obejrzano {percent}%")
        else:
            print("❌ Błąd połączenia:", res.status_code)

    get_active_sessions()

    plex = PlexServer(PLEX_URL, TOKEN)

    # Mapowanie torrent_id -> ścieżka katalogu pobierania
    torrent_locations = {
        "abc123": "/downloads/Breaking.Bad/Season.01",
        "xyz789": "/downloads/Chernobyl"
    }

@app.route("/debug/torrents")
def debug_torrents():
    client = get_client()
    torrents_raw = client.call("core.get_torrents_status", {}, ["name"])
    response = {
        tid.decode(): data[b"name"].decode() for tid, data in torrents_raw.items()
    }
    return jsonify(response)

@app.route("/set-global-limit", methods=["POST"])
def set_global_limit():
    try:
        data = request.get_json(force=True)
        limit_mbps = float(data.get("limit", 0))

        if limit_mbps <= 0:
            # -1 oznacza unlimited w Deluge
            limit_kib = -1
        else:
            # Konwersja MB/s → KiB/s (1 MB = 1024 KiB)
            limit_kib = int(limit_mbps * 1024)

        print(f"🌐 Ustawiam limit globalny i per-torrent na: {limit_kib} KiB/s (z {limit_mbps} MB/s)")

        client = get_client()
        client.call("core.set_config", {
            "max_download_speed": limit_kib,
            "max_download_speed_per_torrent": limit_kib,
            "rate_limit_ip_overhead": True,
            "ignore_limits_on_local_network": False
        })

        return jsonify({
            "status": "ok",
            "limit_mbps": limit_mbps,
            "limit_kib": limit_kib
        })

    except Exception as e:
        print(f"❌ Błąd ustawiania limitu: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/plex/films")
def plex_films():
    def load_progress_cache():
        if os.path.exists(PROGRESS_CACHE_FILE):
            with open(PROGRESS_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def save_progress_cache(data):
        with open(PROGRESS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    try:
        plex = PlexServer(PLEX_URL, TOKEN)
        results = []
        cache = load_progress_cache()
        updated = False

        for video in plex.library.section('Filmy').all():
            key = str(video.ratingKey)
            title = video.title
            poster = get_tmdb_poster(title, type_="movie")
            percent = 100 if video.isWatched else round((video.viewOffset or 0) / video.duration * 100, 1) if video.viewOffset else 0
            watched_at = int(video.lastViewedAt.timestamp() * 1000) if video.lastViewedAt else None

            try:
                path = video.media[0].parts[0].file
            except Exception:
                path = ""

            delete_at = None

            cached_entry = cache.get(key, {})
            if percent >= 100 and watched_at:
                delete_at = cached_entry.get("delete_at") or (watched_at + 7 * 86400000)
            elif percent < 100:
                delete_at = None

            cache_entry = {
                "type": "film",
                "title": title,
                "path": path,
                "progress": percent,
                "delete_at": delete_at
            }

            if cache.get(key) != cache_entry:
                cache[key] = cache_entry
                updated = True

            delete_at_formatted = datetime.fromtimestamp(delete_at / 1000).strftime("%Y-%m-%d %H:%M:%S") if delete_at else None

            film_entry = {
                "id": key,
                "title": title,
                "thumb": poster or (plex.url(video.thumb) if video.thumb else ""),
                "progress": percent,
                "watchedAt": watched_at,
                "deleteAt": delete_at,
                "deleteAtFormatted": delete_at_formatted
            }

            if percent < 100:
                film_entry["canDeleteNow"] = True

            results.append(film_entry)

        if updated:
            save_progress_cache(cache)
            cleanup_unused_posters()

        return jsonify(results)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/plex/series")
def plex_series():
    def load_progress_cache():
        if os.path.exists(PROGRESS_CACHE_FILE):
            with open(PROGRESS_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def save_progress_cache(data):
        with open(PROGRESS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    try:
        plex = PlexServer(PLEX_URL, TOKEN)
        shows = plex.library.section("Seriale").all()

        results = []
        cache = load_progress_cache()
        updated = False

        for show in shows:
            episodes = show.episodes()
            episode_data = []
            total_progress = 0
            last_viewed = 0

            for ep in episodes:
                offset = ep.viewOffset or 0
                duration = ep.duration or 1
                progress = min(100, round(offset / duration * 100))
                total_progress += progress
                episode_data.append({
                    "season": ep.seasonNumber,
                    "episode": ep.index,
                    "title": ep.title,
                    "progress": progress,
                    "id": str(ep.ratingKey)
                })
                if ep.lastViewedAt:
                    last_viewed = max(last_viewed, int(ep.lastViewedAt.timestamp() * 1000))

            average = round(total_progress / len(episodes)) if episodes else 0
            poster = get_tmdb_poster(show.title, type_="tv")
            key = str(show.ratingKey)
            delete_at = None

            # 📁 Pobierz ścieżkę pliku
            try:
                path = show.media[0].parts[0].file
            except Exception:
                path = ""

            if average >= 100 and last_viewed:
                cached = cache.get(key)
                expected_delete = last_viewed + 7 * 86400000
                if not cached or cached.get("delete_at", 0) < expected_delete:
                    cache[key] = {
                        "type": "series",
                        "title": show.title,
                        "delete_at": expected_delete,
                        "path": path
                    }
                    updated = True
                delete_at = cache[key]["delete_at"]

            results.append({
                "id": key,
                "title": show.title,
                "thumb": poster or (plex.url(show.thumb) if show.thumb else ""),
                "progress": average,
                "watchedAt": last_viewed,
                "deleteAt": delete_at,
                "episodes": episode_data
            })

        if updated:
            save_progress_cache(cache)
            cleanup_unused_posters()

        return jsonify(results)

    except Exception as e:
        import traceback
        traceback.print_exc()

        return jsonify({"error": str(e)}), 500

@app.route("/plex/reset-delete-timer", methods=["POST"])
def reset_delete_timer():
    try:
        payload = request.get_json()
        item_id = str(payload.get("id"))

        if not os.path.exists(PROGRESS_CACHE_FILE):
            return jsonify({"error": "Brak pliku cache"}), 404

        with open(PROGRESS_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if item_id not in data:
            return jsonify({"error": "Nie znaleziono ID"}), 404

        new_time = int(time.time() * 1000) + 7 * 86400000
        data[item_id]["delete_at"] = new_time  # ✅ poprawka tutaj

        with open(PROGRESS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        print(f"✅ Zresetowano timer dla ID {item_id} -> {new_time}")
        return jsonify({"success": True, "newDeleteAt": new_time})  # ✅ frontend używa camelCase

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/plex/delete", methods=["DELETE"])
def delete_plex_item():
    try:
        payload = request.get_json()
        item_id = str(payload.get("id"))  # Upewniamy się, że ID jest stringiem
        print(f"📩 Otrzymano żądanie usunięcia ID: {item_id}")

        if not item_id:
            return jsonify({"error": "Brak ID"}), 400

        # Mapowanie dysku (Windows)
        map_network_drive()

        # Wczytanie cache
        print(f"📁 Ładowanie cache z pliku: {PROGRESS_CACHE_FILE}")
        if not os.path.exists(PROGRESS_CACHE_FILE):
            return jsonify({"error": "Brak pliku cache"}), 404

        with open(PROGRESS_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        print("🔍 Klucze w cache:", list(data.keys()))

        entry = data.get(item_id)
        if not entry:
            return jsonify({"error": "Nie znaleziono wpisu w cache"}), 404

        path = entry.get("path")
        path = fix_windows_path(path)
        if not path or not os.path.exists(path):
            return jsonify({"error": f"Nie znaleziono ścieżki: {path}"}), 404

        # 🧹 Usuwanie z dysku
        if os.path.isfile(path):
            os.remove(path)
            print(f"🗑️ Usunięto plik: {path}")
        elif os.path.isdir(path):
            try:
                os.rmdir(path)
                print(f"🗑️ Usunięto folder: {path}")
            except OSError:
                return jsonify({"error": "Folder nie jest pusty"}), 400

        # 🧹 Usuwanie z Plex
        try:
            plex = PlexServer(PLEX_URL, TOKEN)
            media_type = entry.get("type")
            title = entry.get("title")

            if media_type == "film":
                plex.library.section("Filmy").get(title).delete()
                print(f"🎬 Usunięto film z Plexa: {title}")
            elif media_type == "series":
                plex.library.section("Seriale").get(title).delete()
                print(f"📺 Usunięto serial z Plexa: {title}")
        except Exception as e:
            print(f"⚠️ Błąd przy usuwaniu z Plexa: {e}")

        # 🧽 Usuwanie z cache
        del data[item_id]
        with open(PROGRESS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        return jsonify({"success": True})

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"❌ Błąd usuwania: {e}")
        return jsonify({"error": str(e)}), 500




def cleanup_old_media():
    try:
        now = int(time.time() * 1000)
        removed = []
        changed = False

        if not os.path.exists(PROGRESS_CACHE_FILE):
            return jsonify({"removed": [], "status": "no-cache"})

        with open(PROGRESS_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        plex = PlexServer(PLEX_URL, TOKEN)

        for key in list(data.keys()):
            entry = data[key]
            delete_at = entry.get("delete_at")
            path = entry.get("path")
            media_type = entry.get("type")

            if delete_at and delete_at < now:
                if path and os.path.exists(path):
                    try:
                        if os.path.isfile(path):
                            os.remove(path)
                        elif os.path.isdir(path):
                            os.rmdir(path)
                    except Exception as e:
                        print(f"⚠️ Błąd przy usuwaniu pliku/folderu: {e}")

                try:
                    if media_type == "film":
                        item = plex.library.section("Filmy").get(entry["title"])
                        item.delete()
                    elif media_type == "series":
                        item = plex.library.section("Seriale").get(entry["title"])
                        item.delete()
                except Exception as e:
                    print(f"⚠️ Błąd przy usuwaniu z Plexa: {e}")

                removed.append(entry)
                del data[key]
                changed = True

        if changed:
            with open(PROGRESS_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

        return jsonify({"removed": removed})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    def refresh_progress_loop(interval=60):
        def loop():
            while True:
                try:
                    print("🔄 Odświeżanie postępu Plex...")
                    plex = PlexServer(PLEX_URL, TOKEN)
                    cache = load_progress_cache()
                    updated = False

                    for video in plex.library.section('Filmy').all():
                        key = str(video.ratingKey)
                        title = video.title
                        percent = 100 if video.isWatched else round((video.viewOffset or 0) / video.duration * 100,
                                                                    1) if video.viewOffset else 0
                        watched_at = int(video.lastViewedAt.timestamp() * 1000) if video.lastViewedAt else None

                        delete_at = cache.get(key, {}).get("delete_at")

                        if percent >= 100 and watched_at:
                            new_delete_at = watched_at + 7 * 86400000
                            if not delete_at or delete_at < new_delete_at:
                                delete_at = new_delete_at
                                print(f"⏳ Timer ustawiony dla {title} – za 7 dni")

                        # Ścieżka pliku
                        try:
                            path = video.media[0].parts[0].file
                        except:
                            path = ""

                        cache_entry = {
                            "type": "film",
                            "title": title,
                            "path": path,
                            "progress": percent,
                            "delete_at": delete_at
                        }
                        if cache.get(key) != cache_entry:
                            cache[key] = cache_entry
                            updated = True

                    if updated:
                        save_progress_cache(cache)
                        print("💾 Zapisano postęp do cache")

                except Exception as e:
                    print(f"❌ Błąd pętli odświeżania: {e}")

                time.sleep(interval)

        threading.Thread(target=loop, daemon=True).start()

        refresh_progress_loop(interval=60)  # co minutę

def start_film_watchdog(interval_seconds=30):
    def loop():
        print("🚀 [WATCHDOG] Uruchamianie watchdog'a dla filmów...")
        while True:
            try:
                print("🔄 [WATCHDOG] Sprawdzanie postępu filmów...")
                plex = PlexServer(PLEX_URL, TOKEN)
                cache = load_progress_cache()
                updated = False

                videos = plex.library.section('Filmy').all()
                print(f"📺 [WATCHDOG] Znaleziono {len(videos)} filmów")

                now = int(time.time() * 1000)

                for video in videos:
                    key = str(video.ratingKey)
                    title = video.title
                    percent = 100 if video.isWatched else round((video.viewOffset or 0) / video.duration * 100, 1) if video.viewOffset else 0
                    watched_at = int(video.lastViewedAt.timestamp() * 1000) if video.lastViewedAt else None

                    try:
                        path = video.media[0].parts[0].file
                    except Exception:
                        path = ""

                    cached = cache.get(key, {})
                    previous_progress = cached.get("progress")
                    previous_timer = cached.get("delete_at")

                    delete_at = previous_timer  # domyślnie nie zmieniamy timera

                    # 👉 Jeśli progres wzrósł do 100%
                    if percent >= 100 and watched_at:
                        proposed_delete_at = watched_at + 7 * 86400000
                        # tylko jeśli poprzedni timer był pusty albo mniejszy niż nowy
                        if not previous_timer or previous_timer < proposed_delete_at:
                            delete_at = proposed_delete_at
                            print(f"✅ [WATCHDOG] Timer dla '{title}' ustawiony na {datetime.fromtimestamp(delete_at / 1000)}")

                    # 👈 Jeśli progres spadł poniżej 100%, kasujemy timer
                    elif percent < 100:
                        if previous_timer:
                            print(f"❌ [WATCHDOG] Cofnięto postęp '{title}' (<100%) – usuwam timer")
                            delete_at = None

                    # Zapisujemy tylko jeśli cokolwiek się zmieniło
                    if percent != previous_progress or delete_at != previous_timer or path != cached.get("path"):
                        cache[key] = {
                            "type": "film",
                            "title": title,
                            "path": path,
                            "progress": percent,
                            "delete_at": delete_at
                        }
                        updated = True
                        print(f"💾 [WATCHDOG] Zaktualizowano cache dla '{title}' – {percent}%")

                if updated:
                    save_progress_cache(cache)
                    print("📁 [WATCHDOG] Cache zapisany do pliku")

            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"❌ [WATCHDOG] Błąd: {e}")

            time.sleep(interval_seconds)

    threading.Thread(target=loop, daemon=True).start()

start_film_watchdog(interval_seconds=30)

@app.route("/watchtime", methods=["GET"])
def get_watchtime():
    try:
        plex = PlexServer(PLEX_URL, TOKEN)
        results = []

        for video in plex.library.section('Filmy').all():
            title = video.title
            percent = 100 if video.isWatched else round((video.viewOffset or 0) / video.duration * 100, 1) if video.viewOffset else 0
            watched_at = int(video.lastViewedAt.timestamp() * 1000) if video.lastViewedAt else None

            results.append({
                "title": title,
                "progress": percent,
                "watchedAt": watched_at
            })

        return jsonify(results)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

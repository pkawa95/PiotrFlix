# -*- mode: python ; coding: utf-8 -*-

import os
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

APP_NAME = "PiotrFlix"
ENTRY    = "gui_main.py"
ICON     = "static/icon.ico" if os.path.isfile("static/icon.ico") else None

# Wyklucz inne bindingi Qt (żeby nie biły się z PySide6)
EXCLUDES = ["PyQt5", "PyQt5.*", "PyQt6", "PyQt6.*", "tkinter"]

# ——— HIDDEN IMPORTS ———
# - 'app' bo ładujesz backend dynamicznie (importlib)
# - nowe moduły GUI: posters_gui, gui_main_apperance
# - bs4/soupsieve, plexapi, selenium, libtorrent (jak wcześniej)
HIDDEN = [
    "app",
    "posters_gui",
    "gui_main_apperance",
    "bs4",
    "soupsieve",
    "libtorrent",
    "selenium",
    "selenium.webdriver",
    "selenium.webdriver.common",
    "selenium.webdriver.chrome",
    "selenium.webdriver.support",
    "selenium.webdriver.support.expected_conditions",
    "selenium.webdriver.common.by",
] \
+ collect_submodules("PySide6") \
+ collect_submodules("plexapi") \
+ collect_submodules("bs4") \
+ collect_submodules("soupsieve")

# ——— DATAS (zasoby nie-Python) ———
DATAS = [
    ("templates", "templates"),
    ("static",    "static"),
]

# Dodatkowe pliki w głównym katalogu, które mają trafić do dist/
for fn in [
    "available_cache.json",
    "poster_cache.json",
    "torrent_history.json",
    "version.json",
    "manifest.json",
    "sw.js",
]:
    if os.path.isfile(fn):
        DATAS.append((fn, "."))

# Dołóż updater.exe jeśli zbudowany
if os.path.isfile("updater.exe"):
    DATAS.append(("updater.exe", "."))

# (opcjonalnie) chromedriver, jeśli go dystrybuujesz ręcznie
for cand in [
    "chromedriver.exe",
    os.path.join("tools", "chromedriver.exe"),
]:
    if os.path.isfile(cand):
        DATAS.append((cand, "."))

a = Analysis(
    [ENTRY],
    pathex=["."],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    # noarchive=True → brak wspólnego archiwum .pyz, szybszy start w onedir
    noarchive=True,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=APP_NAME,               # wynikowy exe: PiotrFlix.exe
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,               # GUI app
    icon=ICON,
)

# onedir: wszystko w jednym katalogu dist/PiotrFlix/
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=APP_NAME,
)

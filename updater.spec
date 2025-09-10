# updater.spec
# build: pyinstaller --noconfirm --clean updater.spec

block_cipher = None

APP_NAME = "updater"
ENTRY    = "updater.py"

EXCLUDES = ["PyQt6", "PyQt6.*", "PyQt5", "PyQt5.*"]
HIDDEN   = ["PySide6.QtWidgets", "PySide6.QtGui", "PySide6.QtCore"]

DATAS = [
    ("static/logo.png", "static"),
    ("static/icon.png", "static"),
    ("static/icon.ico", "static"),
]

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
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon="icon.ico",
)

# gui_main_apperance.py
# -*- coding: utf-8 -*-
import os
import sys
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

# ───────────────────────────── grafika / flagi (HiDPI itp.) ─────────────────
def setup_graphics_env():
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

# ──────────────────────────────── MOTYW / UI ─────────────────────────────────
_DARK = {
    "bg":            "#0b0b12",
    "bgElev":        "rgba(22,22,34,0.78)",
    "bgElevSolid":   "#161622",
    "bgHover":       "#1a1d22",
    "bgActive":      "#1f2329",
    "cardBorder":    "rgba(255,255,255,0.18)",
    "text":          "#eef2ff",
    "textDim":       "#cfd3ff",
    "textMuted":     "#a9b0d3",
    "border":        "rgba(255,255,255,.12)",
    "accent1":       "#c600ff",
    "accent2":       "#00f0ff",
    "accent3":       "#7cff00",
    "danger1":       "#ff3b30",
    "danger2":       "#ff9500",
    "success1":      "#27ae60",
    "success2":      "#36d17c",
    "scrollTrack":   "rgba(255,255,255,.06)",
}
_LIGHT = {
    "bg":            "#f5f7fb",
    "bgElev":        "rgba(255,255,255,0.86)",
    "bgElevSolid":   "#ffffff",
    "bgHover":       "#f1f3f9",
    "bgActive":      "#e9ecf6",
    "cardBorder":    "rgba(0,0,0,0.06)",
    "text":          "#0a0a0f",
    "textDim":       "#515170",
    "textMuted":     "#6b6b80",
    "border":        "rgba(0,0,0,.10)",
    "accent1":       "#c600ff",
    "accent2":       "#00CCD6",
    "accent3":       "#60cc20",
    "danger1":       "#ff3b30",
    "danger2":       "#ff9500",
    "success1":      "#27ae60",
    "success2":      "#36d17c",
    "scrollTrack":   "rgba(0,0,0,.06)",
}

def _qcolor(arg: str) -> QtGui.QColor:
    c = QtGui.QColor()
    c.setNamedColor(arg)
    return c

def apply_theme(app: QtWidgets.QApplication, theme: str = "dark"):
    colors = _DARK if theme == "dark" else _LIGHT

    pal = QtGui.QPalette()
    pal.setColor(QtGui.QPalette.Window,       _qcolor(colors["bg"]))
    pal.setColor(QtGui.QPalette.Base,         _qcolor(colors["bgElevSolid"]))
    pal.setColor(QtGui.QPalette.AlternateBase,_qcolor(colors["bg"]))
    pal.setColor(QtGui.QPalette.ToolTipBase,  _qcolor(colors["bgElevSolid"]))
    pal.setColor(QtGui.QPalette.ToolTipText,  _qcolor(colors["text"]))
    pal.setColor(QtGui.QPalette.Text,         _qcolor(colors["text"]))
    pal.setColor(QtGui.QPalette.Button,       _qcolor(colors["bgElevSolid"]))
    pal.setColor(QtGui.QPalette.ButtonText,   _qcolor(colors["text"]))
    pal.setColor(QtGui.QPalette.WindowText,   _qcolor(colors["text"]))
    pal.setColor(QtGui.QPalette.Highlight,    _qcolor(colors["accent2"]))
    pal.setColor(QtGui.QPalette.HighlightedText, _qcolor("#ffffff"))
    app.setPalette(pal)

    if sys.platform.startswith("win"):
        app.setFont(QtGui.QFont("Segoe UI", 10))
    elif sys.platform == "darwin":
        app.setFont(QtGui.QFont(".SF NS Text", 13))
    else:
        app.setFont(QtGui.QFont("Inter", 10))

    # brak „czarnych plam” – wymuszamy przezroczyste tła na labelach
    ss = f"""
    * {{
        selection-background-color: {colors["accent1"]};
        selection-color: #ffffff;
        outline: 0;
    }}
    QWidget {{
        background: {colors["bg"]};
        color: {colors["text"]};
    }}
    QLabel {{ background: transparent; }}

    /* karty/kontenery */
    QFrame#card, QWidget[role="card"] {{
        background: {colors["bgElevSolid"]};
        color: {colors["text"]};
        border: 1px solid {colors["cardBorder"]};
        border-radius: 16px;
    }}

    /* menu / statusbar */
    QMenuBar, QMenu {{
        background: {colors["bgElevSolid"]};
        color: {colors["text"]};
        border: none;
    }}
    QMenu::item:selected {{ background: {colors["bgHover"]}; }}
    QStatusBar {{
        background: {colors["bgElevSolid"]};
        border-top: 1px solid {colors["cardBorder"]};
        color: {colors["textDim"]};
    }}
    QStatusBar::item {{ border: none; }}

    /* taby (pigułki) */
    QTabWidget::pane {{ border: none; top: 0px; }}
    QTabBar {{ qproperty-drawBase: 0; }}
    QTabBar::tab {{
        background: transparent;
        color: {colors["textDim"]};
        padding: 8px 14px;
        margin: 6px;
        border: 0px;
        border-radius: 12px;
    }}
    QTabBar::tab:hover {{ background: {colors["bgHover"]}; color: {colors["text"]}; }}
    QTabBar::tab:selected {{
        color: #fff;
        background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 {colors["accent1"]}, stop:0.8 {colors["accent2"]});
        border: 1px solid rgba(255,255,255,.06);
    }}

    /* przyciski (bazowe) */
    QPushButton {{
        background: {colors["bgElevSolid"]};
        color: {colors["text"]};
        padding: 9px 14px;
        border: 1px solid {colors["cardBorder"]};
        border-radius: 12px;
        font-weight: 700;
    }}
    QPushButton:hover {{ background: {colors["bgHover"]}; }}
    QPushButton:pressed {{ background: {colors["bgActive"]}; }}

    /* mały przycisk-ikona */
    QPushButton#icon {{
        min-width: 60px;  max-width: 60px;
        min-height: 36px;
        padding: 0;
        border-radius: 12px;
    }}

    /* pigułka */
    QPushButton#pill {{ border-radius: 16px; padding: 8px 16px; }}

    /* warianty po property (można łączyć z #icon/#pill) */
    QPushButton[accent="true"] {{
        background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 {colors["accent1"]}, stop:0.8 {colors["accent2"]});
        color: #ffffff;
        border: 1px solid rgba(255,255,255,.06);
    }}
    QPushButton[danger="true"] {{
        background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
            stop:0 {colors["danger1"]}, stop:1 {colors["danger2"]});
        color: #fff;
        border: none;
    }}
    QPushButton[success="true"] {{
        background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
            stop:0 {colors["success1"]}, stop:1 {colors["success2"]});
        color: #fff;
        border: 1px solid {colors["cardBorder"]};
    }}

    /* wejścia / comboboxy */
    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QDateEdit, QTimeEdit, QDateTimeEdit {{
        background: {colors["bgElevSolid"]};
        color: {colors["text"]};
        border: 1px solid {colors["cardBorder"]};
        border-radius: 12px;
        padding: 8px 10px;
    }}
    QComboBox {{
        background: {colors["bgElevSolid"]};
        color: {colors["text"]};
        border: 1px solid {colors["cardBorder"]};
        border-radius: 12px;
        padding: 6px 10px;
    }}
    QComboBox::drop-down {{ border: none; width: 26px; }}
    QComboBox QAbstractItemView {{
        background: {colors["bgElevSolid"]};
        color: {colors["text"]};
        border: 1px solid {colors["cardBorder"]};
        selection-background-color: {colors["bgHover"]};
        selection-color: {colors["text"]};
    }}

    /* viewporty */
    QListWidget, QTreeWidget, QTableView, QTreeView {{
        background: {colors["bg"]};
        color: {colors["text"]};
        border: none;
        alternate-background-color: {colors["bgElevSolid"]};
    }}

    /* progress bar */
    QProgressBar {{
        background: #1b1b29;
        color: {colors["textDim"]};
        border: 1px solid rgba(255,255,255,.06);
        border-radius: 8px;
        text-align: center;
        height: 14px;
    }}
    QProgressBar::chunk {{
        background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 {colors["accent1"]}, stop:0.6 {colors["accent2"]}, stop:1 {colors["accent3"]});
        border-radius: 8px;
    }}

    /* scrollbary */
    QScrollBar:vertical, QScrollBar:horizontal {{ background: transparent; border: none; margin: 0; }}
    QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
        background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 {colors["accent1"]}, stop:1 {colors["accent2"]});
        border-radius: 10px; min-height: 28px; min-width: 28px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{ background: transparent; border: none; width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: {colors["scrollTrack"]}; border-radius: 10px; }}

    QLabel#subtle, QLabel[dim="true"] {{ color: {colors["textDim"]}; }}
    QDialog {{ background: {colors["bg"]}; }}
    """
    app.setStyleSheet(ss)

# ───────────────────────────── efekty / helpery ──────────────────────────────
def add_drop_shadow(widget: QtWidgets.QWidget, radius: int = 24, y_offset: int = 10, opacity: float = 0.35):
    eff = QtWidgets.QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(radius)
    eff.setXOffset(0)
    eff.setYOffset(y_offset)
    eff.setColor(QtGui.QColor(0, 0, 0, int(255 * opacity)))
    widget.setGraphicsEffect(eff)

def make_card(parent: Optional[QtWidgets.QWidget] = None) -> QtWidgets.QFrame:
    card = QtWidgets.QFrame(parent)
    card.setObjectName("card")
    add_drop_shadow(card, radius=28, y_offset=12, opacity=0.32)
    return card

def _apply_prop(widget: QtWidgets.QWidget, key: str, val):
    widget.setProperty(key, val)
    widget.style().unpolish(widget); widget.style().polish(widget)

def style_tab_button(btn: QtWidgets.QPushButton, *, active: bool = False):
    btn.setCursor(Qt.PointingHandCursor)
    if btn.objectName() == "":
        btn.setObjectName("pill")
    _apply_prop(btn, "accent", bool(active))

def style_danger(btn: QtWidgets.QPushButton):
    btn.setCursor(Qt.PointingHandCursor)
    _apply_prop(btn, "danger", True)

def style_success(btn: QtWidgets.QPushButton):
    btn.setCursor(Qt.PointingHandCursor)
    _apply_prop(btn, "success", True)

def style_accent(btn: QtWidgets.QPushButton):
    btn.setCursor(Qt.PointingHandCursor)
    _apply_prop(btn, "accent", True)

# ───────────────────────────── ładna statusbar ───────────────────────────────
class PrettyStatusBar(QtWidgets.QStatusBar):
    def __init__(self, parent=None, logo_path: str | None = None, theme: str = "dark"):
        super().__init__(parent)
        self.setSizeGripEnabled(False)
        colors = _DARK if theme == "dark" else _LIGHT
        self.setStyleSheet(f"""
        QStatusBar{{ background:{colors["bgElevSolid"]}; border-top:1px solid {colors["cardBorder"]}; }}
        QStatusBar::item{{ border:none; }}
        QLabel{{ color:{colors["textDim"]}; background:transparent; }}
        """)
        self._container = QtWidgets.QWidget(self)
        h = QtWidgets.QHBoxLayout(self._container)
        h.setContentsMargins(8, 2, 8, 2); h.setSpacing(10)

        self.logo = QtWidgets.QLabel(self._container)
        pix = QtGui.QPixmap(logo_path) if logo_path else QtGui.QPixmap()
        if pix.isNull():
            pix = QtGui.QPixmap(18, 18); pix.fill(QtGui.QColor("#444"))
        self.logo.setPixmap(pix.scaledToHeight(18, Qt.SmoothTransformation))
        self.logo.setFixedHeight(18)

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

# ───────────────────────────── Splash ────────────────────────────────────────
class LoadingSplash(QtWidgets.QWidget):
    def __init__(self, app_name: str = "Piotrflix", icon_path: str | None = None, theme: str = "dark"):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(460, 360)
        colors = _DARK if theme == "dark" else _LIGHT

        container = QtWidgets.QFrame(self)
        container.setObjectName("card")
        container.setFixedSize(460, 360)
        container.setStyleSheet(f"""
        #card {{ background-color:{colors["bgElevSolid"]}; border-radius:20px; }}
        QLabel {{ color:{colors["text"]}; background:transparent; }}
        """)
        add_drop_shadow(container, 36, 12, 0.55)

        v = QtWidgets.QVBoxLayout(container)
        v.setContentsMargins(28, 24, 28, 24); v.setSpacing(16)

        self.icon_label = QtWidgets.QLabel(); self.icon_label.setFixedSize(128, 128)
        self._set_rounded_icon(icon_path, 20)
        self.icon_label.setAlignment(Qt.AlignCenter)

        self.title = QtWidgets.QLabel(app_name)
        self.title.setStyleSheet("font-size:22px;font-weight:700;letter-spacing:.2px;")
        self.title.setAlignment(Qt.AlignCenter)

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

    def _set_rounded_icon(self, path: str | None, radius: int):
        pix = QtGui.QPixmap(path) if path else QtGui.QPixmap()
        if pix.isNull():
            pix = QtGui.QPixmap(128, 128); pix.fill(Qt.transparent)
            p = QtGui.QPainter(pix)
            p.setRenderHint(QtGui.QPainter.Antialiasing)
            p.setBrush(QtGui.QColor("#444")); p.setPen(Qt.NoPen)
            p.drawEllipse(0, 0, 128, 128); p.end()

        pix = pix.scaled(128, 128, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)

        mask = QtGui.QBitmap(128, 128); mask.clear()
        painter = QtGui.QPainter(mask)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setBrush(Qt.black); painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(0, 0, 128, 128, radius, radius); painter.end()

        rounded = QtGui.QPixmap(128, 128); rounded.fill(Qt.transparent)
        p2 = QtGui.QPainter(rounded)
        p2.setRenderHint(QtGui.QPainter.Antialiasing)
        p2.setClipRegion(QtGui.QRegion(mask)); p2.drawPixmap(0, 0, pix); p2.end()

        self.icon_label.setPixmap(rounded)

    def set_status(self, txt: str):
        self.status.setText(txt)

# ───────────────────────────── małe utility ──────────────────────────────────
def progress_bar(value: float = 0) -> QtWidgets.QProgressBar:
    bar = QtWidgets.QProgressBar()
    bar.setRange(0, 100); bar.setValue(int(value or 0))
    bar.setTextVisible(True); bar.setFormat("%p%")
    return bar

def hline() -> QtWidgets.QFrame:
    f = QtWidgets.QFrame()
    f.setFrameShape(QtWidgets.QFrame.HLine); f.setFrameShadow(QtWidgets.QFrame.Sunken)
    return f

if __name__ == "__main__":
    setup_graphics_env()
    app = QtWidgets.QApplication(sys.argv)
    apply_theme(app, theme="dark")
    w = QtWidgets.QMainWindow(); w.resize(900, 600); w.show()
    sys.exit(app.exec())

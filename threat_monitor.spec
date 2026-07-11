# -*- mode: python ; coding: utf-8 -*-
# ─────────────────────────────────────────────────────────────────────────────
#  EVE Threat Monitor — PyInstaller build spec  (surgical PyQt6 — lean build)
#
#  Build command (run from TM/):
#      .\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm threat_monitor.spec
#
#  Output: dist/Eve Threat.exe  (Windows) — binaire homonyme sur macOS/Linux
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
import glob
from PyInstaller.utils.hooks import collect_submodules

# Le spec est multiplateforme : la découpe chirurgicale des DLL Qt et le
# manifeste/icône ne valent que sur Windows ; ailleurs on laisse le hook PyQt6
# standard embarquer les bons plugins (libqcocoa / libqxcb, imageformats…).
IS_WIN = sys.platform == "win32"

# ── Assets ────────────────────────────────────────────────────────────────────
added_datas = [
    ("alert_hostile.wav", "."),
    ("threat_icon.ico",   "."),
]

# ── Surgical PyQt6 platform binaries ─────────────────────────────────────────
# Only grab the DLLs we actually need — skips WebEngine, Qt3D, Multimedia, etc.
def _qt_plugin(subpath):
    """Return (src, dest) tuples for a Qt plugin glob, relative to PyQt6."""
    import PyQt6
    root = os.path.join(os.path.dirname(PyQt6.__file__), "Qt6", "plugins")
    matches = glob.glob(os.path.join(root, subpath))
    dest = os.path.join("PyQt6", "Qt6", "plugins", os.path.dirname(subpath))
    return [(m, dest) for m in matches]

qt_binaries = (
    _qt_plugin("platforms/qwindows.dll")   +
    _qt_plugin("imageformats/qico.dll")
) if IS_WIN else []

# ── Hidden imports ────────────────────────────────────────────────────────────
# Backends chargés dynamiquement (plyer) ou conditionnellement (winsound, mss.*) :
# l'analyse statique de PyInstaller les rate, on les nomme par plateforme.
if sys.platform == "darwin":
    platform_hidden = ["plyer.platforms.macosx.notification", "mss.darwin"]
elif IS_WIN:
    platform_hidden = ["winsound", "plyer.platforms.win.notification", "mss.windows"]
else:
    platform_hidden = ["plyer.platforms.linux.notification", "mss.linux"]

hidden = [
    # PyQt6 — only the modules we import
    "PyQt6.sip",
    "PyQt6.QtWidgets",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    # backends plateforme (winsound / plyer / mss)
    "plyer",
    *platform_hidden,
    # mss screen-capture
    "mss",
    # numpy internals
    "numpy",
    "numpy.core._multiarray_umath",
    "numpy.core._dtype_ctypes",
    # requests + urllib3 (bundled cert store needs explicit inclusion)
    "requests",
    "requests.adapters",
    "requests.auth",
    "requests.cookies",
    "requests.exceptions",
    "requests.sessions",
    "urllib3",
    "urllib3.util.retry",
    "urllib3.util.ssl_",
    "certifi",
    "charset_normalizer",
    "idna",
    # full tm package tree
    *collect_submodules("tm"),
]

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    ["threat_monitor.py"],
    pathex=["."],
    binaries=qt_binaries,
    datas=added_datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Qt modules we never use — biggest size offenders
        "PyQt6.QtWebEngine",
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtMultimedia",
        "PyQt6.QtMultimediaWidgets",
        "PyQt6.Qt3DCore",
        "PyQt6.Qt3DRender",
        "PyQt6.QtBluetooth",
        "PyQt6.QtLocation",
        "PyQt6.QtPositioning",
        "PyQt6.QtSensors",
        "PyQt6.QtSerialPort",
        "PyQt6.QtSql",
        "PyQt6.QtTest",
        "PyQt6.QtXml",
        # heavy Python packages we never import
        "matplotlib",
        "scipy",
        "pandas",
        "IPython",
        "pygments",
        "PIL._imagingtk",
        "unittest",
        "pydoc",
        "xmlrpc",
        "tkinter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

# ── Élague le fallback OpenGL software (~20 Mo brut) ─────────────────────────
# L'app peint uniquement en raster (QPainter) et force QT_QPA_NO_DIRECT2D :
# opengl32sw.dll (fallback OpenGL logiciel, requis seulement par QtQuick /
# QOpenGLWidget) ne sert jamais. Exclusion classique et sûre pour une app
# QWidget. NB : re-tester le rendu une fois après build — seul changement non
# couvert par les tests. Pour aller plus loin, la stack ANGLE
# (libEGL/libGLESv2/d3dcompiler) est aussi inutilisée mais à retirer prudemment.
a.binaries = [b for b in a.binaries if "opengl32sw" not in b[0].lower()]

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# ── Single EXE ────────────────────────────────────────────────────────────────
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Eve Threat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[
        "qwindows.dll",
        "qmodernwindowsstyle.dll",
        "qwindowsvistastyle.dll",
        "Qt6Core.dll",
        "Qt6Gui.dll",
        "Qt6Widgets.dll",
    ],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # .ico et manifeste sont des artefacts Windows ; sur macOS un vrai bundle
    # .app demanderait un .icns + BUNDLE(), hors périmètre de ce spec.
    icon="threat_icon.ico" if IS_WIN else None,
    manifest="app.manifest" if IS_WIN else None,
)

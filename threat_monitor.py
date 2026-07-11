# -*- coding: utf-8 -*-
# EVE Threat Monitor — point d'entrée de l'application.
# Lancement :  python threat_monitor.py
# Isolé du package tm pour que PyInstaller ait un script racine simple et que
# les variables d'environnement Qt soient posées AVANT tout import PyQt6.
import os
import sys

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding='utf-8')

if sys.platform == "win32":
    # Désactive le backend Direct2D de Qt avant QApplication.
    # Sur Windows 10/11, Qt6 utilise Direct2D pour le backing store, mais dans un
    # exe PyInstaller la surface D2D est créée sans canal alpha → fenêtres opaques.
    # Sans Direct2D, Qt bascule vers le rasterizer software (QImage ARGB32) qui
    # supporte correctement WA_TranslucentBackground.
    #
    # NB : ne PAS utiliser QT_QPA_PLATFORM="windows:nodirect2d" — cette sous-option
    # date de Qt5 (plugin Direct2D séparé, supprimé dans Qt6). Sur PyQt6 6.11 elle
    # est inconnue et Qt logge « Unknown option "nodirect2d" ».
    os.environ.setdefault("QT_D3DCREATE_MULTITHREADED", "1")
    os.environ.setdefault("QT_QPA_NO_DIRECT2D", "1")
elif sys.platform.startswith("linux"):
    # Force le backend X11 (xcb). Sous Wayland natif, un client ne peut ni se
    # positionner en absolu ni rester au-dessus (WindowStaysOnTopHint ignoré),
    # et mss ne capture l'écran que via X11 : l'overlay serait inutilisable.
    # xcb fonctionne aussi en session Wayland grâce à XWayland. setdefault :
    # l'utilisateur peut toujours forcer QT_QPA_PLATFORM=wayland pour essayer.
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

try:
    from PyQt6.QtWidgets import QApplication
except ImportError:
    print("CRITICAL: PyQt6 not found. Run:  pip install PyQt6 mss numpy")
    sys.exit(1)

from tm.config  import setup_logging
from tm.monitor import ThreatMonitor


def main() -> None:
    # Construit QApplication + la fenêtre principale et lance la boucle Qt.
    # quitOnLastWindowClosed=False : la fenêtre peut se cacher dans le tray
    # sans que l'app ne se ferme ; la sortie passe par _real_close.
    setup_logging()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    _monitor = ThreatMonitor()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
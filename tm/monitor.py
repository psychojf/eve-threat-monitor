# -*- coding: utf-8 -*-
# Widget principal de ThreatMonitor.
#
# Responsabilités (orchestration uniquement) :
#   • Layout UI et thème
#   • Machine d'état : IDLE / MONITORING / PAUSED / HOSTILE / ALL CLEAR
#   • Animations typewriter + brackets
#   • Câblage entre détection, audio et couches visuelles
#   • Délégation de la persistance config
#
# Détection pixel → tm.detection | Audio → tm.audio | Sous-fenêtres → tm.widgets
import os
import random
import re
import logging

import mss
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QFrame, QMenu, QSystemTrayIcon,
)

from PyQt6.QtCore import Qt, QTimer, QEvent, QPoint, QRect
from PyQt6.QtGui  import QPainter, QColor, QPen, QIcon, QPixmap, QPolygon

from .config  import load_config, save_config, ICON_FILE, SOUND_FILE, SOUND_FILE_EXISTS
from .themes  import THEMES, DEFAULT_THEME_NAME, hex_to_qcolor
from .detection import detect_threats, strip_bbox
from .coordinates import (
    build_screen_mappings,
    native_bbox_to_qt_rect,
    qt_rect_to_native_bbox,
)
from . import audio
from .qtutil    import make_overlay_window, clamp_to_screen, mono_family
from .widgets    import AreaSelector, MirrorWindow, TransparencySlider
from .zkill_card import ZkillCard

log = logging.getLogger(__name__)


class ThreatMonitor(QWidget):
    # Widget principal de surveillance EVE — état, UI, animations et alertes.

    # Charset de scramble typewriter
    _TW_CHARS     = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789$+-*/=%#&_<>^~"
    _TW_CHARS_LEN = len(_TW_CHARS)

    def __init__(self) -> None:
        # Initialise la fenêtre, charge la config, construit l'UI et démarre.
        super().__init__()

        make_overlay_window(self)
        self.setWindowTitle("EVE Threat Monitor")

        self.app_config    = load_config()
        self._bbox         = self.app_config.get("detection_bbox")
        self._relative_bbox = self.app_config.get("relative_bbox")

        # ── Drapeaux d'état ───────────────────────────────────────────────
        self._monitoring        = False
        self.fake_threat_mode   = False
        self.last_threat_count  = 0
        self._alerts_paused     = False
        self.alert_acknowledged = True
        self.is_alerting        = False
        self._monitor_errors    = 0          # erreurs consécutives
        self._has_successful_sample = False  # seul fondement possible de ALL CLEAR
        self._quitting          = False
        self._tray_usable       = False
        self._nomirror_notified = False

        # État logique affiché (clé de _STATE_STYLE). Distinct des messages
        # transitoires (READY, SCAN ON, ERR n/3…) qui n'écrasent pas _state.
        self._state = "idle"

        # Affichage du delta de menaces
        self.threat_diff_text  = ""
        self.threat_diff_ticks = 0

        # ── Références aux sous-fenêtres ──────────────────────────────────
        self.mirror_window       = None
        self.transparency_slider = None
        self._sct                = None
        self._screen_mappings    = None

        # ── Scanner clipboard zKill ───────────────────────────────────────
        self._zkill_scanning  = False
        self._zkill_last_clip = ""
        self._zkill_card      = None
        self._clipboard = QApplication.clipboard()
        self._clipboard.dataChanged.connect(self._on_clipboard_changed)

        # ── Invalidation du mapping écran ─────────────────────────────────
        # Le mapping Qt↔MSS est mémorisé pour éviter de le reconstruire à
        # chaque poll ; il devient faux si un écran est branché/débranché ou
        # si l'échelle DPI change en cours de session.
        app = QApplication.instance()
        app.screenAdded.connect(self._on_screen_added)
        app.screenRemoved.connect(self._on_screens_changed)
        app.primaryScreenChanged.connect(self._on_screens_changed)
        for screen in app.screens():
            screen.geometryChanged.connect(self._on_screens_changed)

        # ── Animation typewriter ──────────────────────────────────────────
        self._tw_target = "IDLE"
        self._tw_color  = ""
        self._tw_step   = 0
        self._tw_timer  = QTimer(self)
        self._tw_timer.timeout.connect(self._tw_tick)

        # ── Animation brackets de coin ────────────────────────────────────
        self._bracket_offset = 0
        self._bracket_state  = "idle"
        self._bracket_color  = QColor(90, 112, 133)
        self._bracket_timer  = QTimer(self)
        self._bracket_timer.timeout.connect(self._animate_bracket_lockon)

        # ── Timer de poll monitoring ──────────────────────────────────────
        self.monitor_timer = QTimer(self)
        self.monitor_timer.timeout.connect(self._update_monitor)

        # ── Timer de répétition d'alerte (5 s) ────────────────────────────
        # Un seul timer réutilisable — évite l'empilement de chaînes
        # QTimer.singleShot qui pouvait provoquer des bips en double.
        self._alert_timer = QTimer(self)
        self._alert_timer.setInterval(5000)
        self._alert_timer.timeout.connect(self._alert_tick)

        # ── Timer de retombée du flash SCAN ON/OFF (F5) ───────────────────
        # Le flash donne le retour immédiat que l'utilisateur attend, mais il
        # ne doit pas masquer ALL CLEAR/HOSTILE : après 1,5 s le texte d'état
        # courant est restauré. Timer réutilisable — un double F5 le relance
        # au lieu d'empiler des singleShot.
        self._scan_msg_timer = QTimer(self)
        self._scan_msg_timer.setSingleShot(True)
        self._scan_msg_timer.setInterval(1500)
        self._scan_msg_timer.timeout.connect(self._restore_after_scan_flash)

        # ── Debounce de sauvegarde d'opacité ──────────────────────────────
        # Le slider émet en continu pendant le drag ; on ne persiste qu'une
        # fois l'utilisateur stabilisé (évite des dizaines d'écritures disque).
        self._opacity_save_timer = QTimer(self)
        self._opacity_save_timer.setSingleShot(True)
        self._opacity_save_timer.setInterval(400)
        self._opacity_save_timer.timeout.connect(self._save_config)

        # ── Drag / double-clic ────────────────────────────────────────────
        # Drag activé après 3 px de mouvement — pas de délai timer.
        # Double-clic détecté nativement par Qt via MouseButtonDblClick.
        self._drag_pos     = None
        self._press_pos    = None
        self._drag_started = False

        self._build_ui()
        self._install_titlebar_filter()
        self._apply_theme(self.app_config.get("theme", DEFAULT_THEME_NAME))
        self._validate_coordinate_config()
        self._restore_position()
        self._check_and_auto_start()
        self._setup_tray()
        self.show()

        saved_opacity = self.app_config.get('opacity', 1.0)
        self.setWindowOpacity(max(0.2, min(1.0, saved_opacity)))


    # ═════════════════════════════════════════════════════════════════════════
    #  Config (wrappers minces — les widgets enfants peuvent appeler self._save_config)
    # ═════════════════════════════════════════════════════════════════════════

    def _save_config(self) -> None:
        # Sauvegarde la config courante sur disque.
        save_config(self.app_config)

    def _on_screen_added(self, screen) -> None:
        # Un écran vient d'apparaître : surveille aussi ses changements de géométrie.
        screen.geometryChanged.connect(self._on_screens_changed)
        self._on_screens_changed()

    def _on_screens_changed(self, *args) -> None:
        # Topologie/échelle écran modifiée : le mapping mémorisé est périmé.
        self._screen_mappings = None
        # Après un réarrangement, les anciennes coordonnées de capture peuvent
        # rester CAPTURABLES tout en visant un autre contenu → faux ALL CLEAR
        # silencieux. On arrête donc bruyamment ; l'utilisateur vérifie la
        # région (F3 si besoin) et relance (F1). La config n'est pas effacée :
        # si la disposition revient à l'identique, F1 suffit.
        if self._monitoring:
            log.warning("display layout changed — monitoring stopped for safety")
            self._stop_monitoring()
            audio.send_notification(
                "Monitoring stopped",
                "Display layout changed — verify region (F3) and restart (F1)",
            )
            self._typewriter_status("SCREEN CHG", self.theme['YELLOW'])

    def _validate_coordinate_config(self) -> None:
        # Versionne les bboxes et invalide les coordonnées legacy à DPI élevé.
        if self.app_config.get("coordinate_space_version", 0) >= 2:
            return

        try:
            self._screen_mappings = build_screen_mappings()
        except Exception as exc:
            log.warning("coordinate mapping unavailable during config validation: %s", exc)
            # Sans mapping fiable, une ancienne bbox est dangereuse : mieux vaut
            # demander une nouvelle sélection que surveiller les mauvais pixels.
            if any(
                key in self.app_config
                for key in ("detection_bbox", "relative_bbox", "mirror_bbox")
            ):
                for key in ("detection_bbox", "relative_bbox", "mirror_bbox"):
                    self.app_config.pop(key, None)
                self._bbox = None
                self._relative_bbox = None
                self._typewriter_status("REQ F2/F3", self.theme['YELLOW'])
                self._save_config()
            return

        scaled = any(
            abs(mapping.scale_x - 1.0) > 0.01
            or abs(mapping.scale_y - 1.0) > 0.01
            for mapping in self._screen_mappings
        )
        if scaled:
            for key in ("detection_bbox", "relative_bbox", "mirror_bbox"):
                self.app_config.pop(key, None)
            self._bbox = None
            self._relative_bbox = None
            self._typewriter_status("REQ F2/F3", self.theme['YELLOW'])

        self.app_config["coordinate_space_version"] = 2
        self._save_config()

    # ═════════════════════════════════════════════════════════════════════════
    #  Tray système
    # ═════════════════════════════════════════════════════════════════════════

    def _setup_tray(self) -> None:
        # Crée l'icône systray avec menu Show/Hide + Quit.
        self._tray_usable = False
        try:
            if QSystemTrayIcon.isSystemTrayAvailable() and os.path.exists(ICON_FILE):
                icon = QIcon(ICON_FILE)
                if icon.isNull():
                    log.warning("icône tray nulle : %s", ICON_FILE)
                    return
                self.tray = QSystemTrayIcon(self)
                self.tray.setIcon(icon)
                menu = QMenu()
                # Show / Hide ne touchent QUE la fenêtre principale — le miroir
                # reste visible car la détection en bbox relative lit ses pixels.
                menu.addAction("Show").triggered.connect(lambda: self.show())
                menu.addAction("Hide").triggered.connect(lambda: self.hide())
                menu.addSeparator()
                menu.addAction("Quit").triggered.connect(self._real_close)
                self.tray.setContextMenu(menu)
                self.tray.show()
                self._tray_usable = True
        except Exception as exc:
            log.warning("échec init tray : %s", exc)

    # ═════════════════════════════════════════════════════════════════════════
    #  Construction UI
    # ═════════════════════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        # Construit le layout complet : titlebar, séparateur, corps.
        self._collapsed = False
        self._expanded_height = 118

        self.resize(165, self._expanded_height)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(0)

        self.content_frame = QFrame()
        self.content_frame.setObjectName("content")
        content_layout = QVBoxLayout(self.content_frame)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        main_layout.addWidget(self.content_frame)

        # ── Titlebar ──────────────────────────────────────────────────────
        self.titlebar = QFrame()
        self.titlebar.setObjectName("titlebar")
        self.titlebar.setFixedHeight(20)
        self.titlebar.setCursor(Qt.CursorShape.SizeAllCursor)
        title_row = QHBoxLayout(self.titlebar)
        title_row.setContentsMargins(6, 0, 4, 0)
        title_row.setSpacing(4)

        self.title_lbl = QLabel("◈ EVE MONITOR")
        self.title_lbl.setObjectName("title_lbl")

        self.play_btn  = self._make_icon_btn("play_btn",  cursor=True)
        self.pause_btn = self._make_icon_btn("pause_btn", cursor=True)
        self.close_btn = self._make_icon_btn("close_btn", cursor=True)

        # Clic GAUCHE uniquement : un clic droit sur ▶ coupait la protection
        # et sur × quittait l'app, au lieu de laisser passer le menu contextuel.
        self.play_btn.mousePressEvent  = self._left_click_only(self._toggle_monitoring)
        self.pause_btn.mousePressEvent = self._left_click_only(self._toggle_pause)
        self.close_btn.mousePressEvent = self._left_click_only(self._real_close)

        title_row.addWidget(self.title_lbl)
        title_row.addStretch()
        title_row.addWidget(self.play_btn)
        title_row.addWidget(self.pause_btn)
        title_row.addWidget(self.close_btn)
        content_layout.addWidget(self.titlebar)

        # ── Séparateur ───────────────────────────────────────────────────
        sep = QFrame()
        sep.setObjectName("separator")
        sep.setFixedHeight(1)
        content_layout.addWidget(sep)

        # ── Corps (repliable) ────────────────────────────────────────────
        self.body_frame = QFrame()
        self.body_frame.setObjectName("body")
        body_layout = QVBoxLayout(self.body_frame)
        body_layout.setContentsMargins(8, 6, 8, 6)
        body_layout.setSpacing(4)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 2, 0, 2)

        self.status_bar = QFrame()
        self.status_bar.setFixedWidth(3)
        self.status_bar.setObjectName("status_bar")

        self.status_lbl = QLabel("IDLE")
        self.status_lbl.setObjectName("status_lbl")
        self.status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        status_row.addWidget(self.status_bar)
        status_row.addWidget(self.status_lbl, 1)
        body_layout.addLayout(status_row)

        self.threat_lbl = QLabel("Neuts: –")
        self.threat_lbl.setObjectName("threat_lbl")
        self.threat_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.ally_lbl = QLabel("Friends: –")
        self.ally_lbl.setObjectName("ally_lbl")
        self.ally_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        body_layout.addWidget(self.threat_lbl)
        body_layout.addWidget(self.ally_lbl)
        content_layout.addWidget(self.body_frame)

        self._drag_pos = None

    @staticmethod
    def _left_click_only(handler):
        # Fabrique un mousePressEvent qui ne déclenche `handler` qu'au bouton
        # gauche. Les autres boutons sont ignorés : le clic droit reste
        # disponible pour le menu contextuel et ne peut plus stopper la
        # protection ou quitter l'app par accident.
        def _dispatch(event):
            if event.button() == Qt.MouseButton.LeftButton:
                handler()
        return _dispatch

    @staticmethod
    def _make_icon_btn(obj_name: str, cursor: bool = False) -> QLabel:
        # Retourne un QLabel 14x14 utilisé comme micro-bouton icône.
        btn = QLabel()
        btn.setObjectName(obj_name)
        btn.setFixedSize(14, 14)
        btn.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if cursor:
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
        return btn

    def _apply_theme(self, theme_name: str) -> None:
        # Applique le thème choisi, met à jour le stylesheet et repeint les boutons.
        theme_name = theme_name if theme_name in THEMES else DEFAULT_THEME_NAME
        self.theme = THEMES[theme_name]
        self._bracket_color = hex_to_qcolor(self.theme['DIM'])
        self.app_config['theme'] = theme_name
        self._save_config()

        t = self.theme
        self.setStyleSheet(f"""
            QFrame#content   {{ background-color: {t['BG']}; border-radius: 2px; }}
            QFrame#titlebar  {{ background-color: {t['BG_PANEL']}; border-radius: 2px; }}
            QFrame#body      {{ background-color: {t['BG']}; }}
            QFrame#separator {{ background-color: {t['BORDER']}; }}
            QLabel           {{ font-family: "{mono_family()}"; }}
            QLabel#title_lbl {{ color: {t['CYAN']}; font-size: 9px; font-weight: bold;
                                letter-spacing: 1px; }}
            QLabel#play_btn, QLabel#pause_btn, QLabel#close_btn {{
                background-color: transparent;
                border-radius: 1px;
            }}
            QLabel#play_btn  {{ border: 1px solid {t['GREEN']}; }}
            QLabel#pause_btn {{ border: 1px solid {t['YELLOW']}; }}
            QLabel#close_btn {{ border: 1px solid {t['RED']}; }}
            QLabel#status_lbl {{ color: {t['DIM']}; font-size: 14px; font-weight: bold; }}
            QLabel#threat_lbl {{ color: {t['RED']}; font-size: 11px; font-weight: bold; }}
            QLabel#ally_lbl   {{ color: {t['GREEN']}; font-size: 11px; }}
            QFrame#status_bar {{ background-color: {t['DIM']}; }}
        """)

        # Redessine les icônes avec les couleurs du thème courant
        self._repaint_icon_btns()
        # Réapplique l'indicateur de scan : son style inline (titre jaune)
        # doit suivre le thème au lieu de garder l'ancienne couleur.
        self._refresh_scan_indicator()

        if self.mirror_window:
            self.mirror_window.theme = self.theme
            self.mirror_window._apply_theme()

        # Rejoue le style de l'état courant. Sans cela, changer de thème en
        # HOSTILE gardait `_bracket_state == "hostile"` mais remettait sa
        # couleur à DIM, et le garde same-state empêchait toute correction.
        self._bracket_state = ""
        self._set_state(self._state)
        self.update()

    def _repaint_icon_btns(self) -> None:
        # Redessine les trois micro-boutons (▶ ⏸ ×) avec les couleurs du thème.
        t = self.theme

        def _draw_btn(draw_fn) -> QPixmap:
            # Crée un QPixmap 14x14 transparent et exécute draw_fn dessus.
            pm = QPixmap(14, 14)
            pm.fill(QColor(0, 0, 0, 0))
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            draw_fn(p)
            p.end()
            return pm

        # ▶ play — triangle plein vert
        play_color = hex_to_qcolor(t['GREEN'])
        def draw_play(p):
            p.setBrush(play_color)
            p.setPen(Qt.PenStyle.NoPen)
            tri = QPolygon([QPoint(4, 3), QPoint(4, 11), QPoint(11, 7)])
            p.drawPolygon(tri)

        # ⏸ pause — deux rectangles jaune/orange
        pause_color = hex_to_qcolor(t['YELLOW'])
        def draw_pause(p):
            p.setBrush(pause_color)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRect(3, 3, 3, 8)
            p.drawRect(8, 3, 3, 8)

        # × close — deux diagonales, toujours rouge
        close_color = hex_to_qcolor(t['RED'])
        def draw_close(p):
            pen = QPen(close_color, 2)
            p.setPen(pen)
            p.drawLine(4, 4, 10, 10)
            p.drawLine(10, 4, 4, 10)

        self.play_btn.setPixmap(_draw_btn(draw_play))
        self.pause_btn.setPixmap(_draw_btn(draw_pause))
        self.close_btn.setPixmap(_draw_btn(draw_close))

    def _set_btn_border(self, btn, color: str) -> None:
        # Met à jour uniquement la bordure d'un micro-bouton sans toucher au fond.
        btn.setStyleSheet(
            f"border: 1px solid {color}; border-radius: 1px; background-color: transparent;"
        )

    # ═════════════════════════════════════════════════════════════════════════
    #  Dessin
    # ═════════════════════════════════════════════════════════════════════════

    def paintEvent(self, event) -> None:
        # Dessine la bordure externe et les quatre brackets de coin.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        painter.setPen(QPen(hex_to_qcolor(self.theme['BORDER']), 1))
        painter.drawRect(self.rect().adjusted(10, 10, -10, -10))

        painter.setPen(QPen(self._bracket_color, 2))
        s   = 8
        pad = 8 - self._bracket_offset
        w, h = self.width(), self.height()

        # Quatre brackets de coin
        for (x0, y0, dx, dy) in [
            (pad,         pad,         1,  1),
            (w-pad-1,     pad,        -1,  1),
            (pad,         h-pad-1,     1, -1),
            (w-pad-1,     h-pad-1,    -1, -1),
        ]:
            painter.drawLine(x0, y0, x0 + dx * s, y0)
            painter.drawLine(x0, y0, x0, y0 + dy * s)

    # ═════════════════════════════════════════════════════════════════════════
    #  Repli (double-clic titlebar)
    # ═════════════════════════════════════════════════════════════════════════

    def _toggle_collapse(self) -> None:
        # Bascule entre la vue réduite (titlebar seule) et vue complète.
        self._collapsed = not self._collapsed
        self.body_frame.setVisible(not self._collapsed)
        if self._collapsed:
            self.setFixedHeight(self.titlebar.height() + 22)
        else:
            self.setFixedHeight(self._expanded_height)
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
        self._update_titlebar_tooltip()

    def _update_titlebar_tooltip(self) -> None:
        # Affiche les compteurs dans le tooltip titlebar uniquement en mode réduit.
        lines = []
        if self._collapsed:
            neuts   = self.threat_lbl.text()
            friends = self.ally_lbl.text()
            lines.extend((neuts, friends))
        if self._zkill_scanning:
            lines.append("zKill scan ON")
        self.titlebar.setToolTip("\n".join(lines))

    def _refresh_scan_indicator(self) -> None:
        # Affiche le scan clipboard sans remplacer l'état de sécurité :
        # glyphe ◉ + titre en jaune tant que le scan est actif. Un indicateur
        # PERSISTANT, visible d'un coup d'œil, là où le flash SCAN ON retombe
        # après 1,5 s.
        self.title_lbl.setText(
            "◉ EVE MONITOR" if self._zkill_scanning else "◈ EVE MONITOR"
        )
        if self._zkill_scanning:
            self.title_lbl.setStyleSheet(
                f"color: {self.theme['YELLOW']}; font-size: 9px; "
                f"font-weight: bold; letter-spacing: 1px;"
            )
        else:
            # Stylesheet inline vidé : la règle globale #title_lbl (couleur
            # d'accent du thème) reprend la main.
            self.title_lbl.setStyleSheet("")
        self._update_titlebar_tooltip()

    # ═════════════════════════════════════════════════════════════════════════
    #  Souris (drag titlebar + double-clic repli)
    # ═════════════════════════════════════════════════════════════════════════

    # ═════════════════════════════════════════════════════════════════════════
    #  Filtre d'événements titlebar — capture les events souris des enfants
    #  (les QLabel enfants avalent normalement les events avant le parent)
    # ═════════════════════════════════════════════════════════════════════════

    def _install_titlebar_filter(self) -> None:
        # Installe le filtre uniquement sur la surface déplaçable du titlebar.
        for widget in [self.titlebar, self.title_lbl]:
            widget.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        # Gère drag, release et double-clic pour toute la surface du widget.
        if obj not in (self.titlebar, self.title_lbl):
            return super().eventFilter(obj, event)
        t = event.type()

        if t == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos      = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._press_pos     = event.globalPosition().toPoint()
            self._drag_started  = False
            return False  # les boutons reçoivent quand même le clic

        if t == QEvent.Type.MouseMove and event.buttons() == Qt.MouseButton.LeftButton:
            if self._drag_pos is not None:
                delta = event.globalPosition().toPoint() - self._press_pos
                # Drag activé dès 3 px de mouvement, sans délai timer
                if not self._drag_started and (abs(delta.x()) > 3 or abs(delta.y()) > 3):
                    self._drag_started = True
                if self._drag_started:
                    self.move(event.globalPosition().toPoint() - self._drag_pos)
            return False

        if t == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            if self._drag_started:
                self.app_config["win_geom"] = {'x': self.x(), 'y': self.y()}
                self._save_config()
            self._drag_pos     = None
            self._drag_started = False
            return False

        if t == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos     = None
            self._drag_started = False
            self._toggle_collapse()
            return True  # événement consommé

        return super().eventFilter(obj, event)

    # ═════════════════════════════════════════════════════════════════════════
    #  Clavier
    # ═════════════════════════════════════════════════════════════════════════

    def keyPressEvent(self, event) -> None:
        # Dispatch les touches F1–F10, Espace et Échap vers leurs actions.
        dispatch = {
            Qt.Key.Key_F1:     self._toggle_monitoring,
            Qt.Key.Key_F2:     self._open_mirror,
            Qt.Key.Key_F3:     self._reselect_area,
            Qt.Key.Key_F5:     self._toggle_zkill_scan,
            Qt.Key.Key_F10:    self._toggle_fake_threat,
            Qt.Key.Key_Space:  self._acknowledge_alert,
            Qt.Key.Key_Escape: self._real_close,
        }
        action = dispatch.get(event.key())
        if action:
            action()

    # ═════════════════════════════════════════════════════════════════════════
    #  Scanner clipboard zKill
    # ═════════════════════════════════════════════════════════════════════════

    def _toggle_zkill_scan(self) -> None:
        # F5 — active/désactive le scan clipboard zKill.
        self._zkill_scanning = not self._zkill_scanning
        if self._zkill_scanning:
            self._zkill_last_clip = ""
        else:
            if self._zkill_card:
                self._zkill_card.close()
                self._zkill_card = None
        self._refresh_scan_indicator()
        # Retour immédiat et visible (l'ancien « SCAN ON ») — un simple
        # message transitoire : _state n'est jamais touché, donc aucun risque
        # de fabriquer/masquer un résultat de sécurité. La retombée est gérée
        # par _scan_msg_timer.
        self._typewriter_status(
            "SCAN ON" if self._zkill_scanning else "SCAN OFF",
            self.theme['CYAN'],
        )
        self._scan_msg_timer.start()

    def _restore_after_scan_flash(self) -> None:
        # Retombée du flash SCAN ON/OFF : restaure le texte de l'état courant.
        # Garde : si un message plus récent (HOSTILE, ERR n/3…) a déjà
        # remplacé le flash, on ne l'écrase pas — il est plus important.
        if self._tw_target in ("SCAN ON", "SCAN OFF"):
            self._set_state(self._state)

    def _on_clipboard_changed(self) -> None:
        # Appelé par le signal dataChanged — filtre si le scan n'est pas actif.
        if self._zkill_scanning:
            self._check_clipboard()

    def _check_clipboard(self) -> None:
        # Déclenche un lookup zKill si le clipboard contient un nom EVE valide.
        clip = self._clipboard.text().strip()

        if not clip or clip == self._zkill_last_clip:
            return

        # Validation nom EVE : 3-37 chars, une seule ligne, chars autorisés
        if '\n' in clip or len(clip) < 3 or len(clip) > 37:
            return
        if re.search(r"[^ 'a-zA-Z0-9\-]", clip):
            return

        # Ferme la carte précédente (déclenche _on_zkill_card_dismissed, qui
        # remet _zkill_last_clip à "") AVANT de fixer le nouveau nom, sinon le
        # callback effacerait la valeur qu'on vient d'écrire.
        if self._zkill_card:
            self._zkill_card.close()
            self._zkill_card = None

        self._zkill_card = ZkillCard(
            clip, self.theme, self.frameGeometry(),
            on_closed=self._on_zkill_card_dismissed,
        )
        self._zkill_last_clip = clip

    def _on_zkill_card_dismissed(self) -> None:
        # Carte zKill fermée (×, F5 off, ou remplacement) : ré-arme le scan.
        #
        # Remettre _zkill_last_clip à "" permet de re-copier le MÊME nom après une
        # erreur transitoire ou une fermeture, au lieu de rester bloqué sur le
        # garde `clip == self._zkill_last_clip`.
        self._zkill_card      = None
        self._zkill_last_clip = ""

    # ═════════════════════════════════════════════════════════════════════════
    #  Menu contextuel
    # ═════════════════════════════════════════════════════════════════════════

    def contextMenuEvent(self, event) -> None:
        # Affiche le menu clic-droit (monitoring, fenêtres, transparence, thèmes).
        menu = QMenu(self)
        menu.setStyleSheet(
            f"background-color: {self.theme['BG_PANEL']}; "
            f"color: {self.theme['CYAN']}; font-family: \"{mono_family()}\";"
        )

        # ── Monitoring (identique à F1) ───────────────────────────────────
        mon_label = "■  Stop Monitoring" if self._monitoring else "▶  Start Monitoring"
        menu.addAction(mon_label).triggered.connect(
            lambda checked: self._toggle_monitoring()
        )
        menu.addSeparator()

        # ── Fenêtres / zones (identique à F2 / F3 / F5) ───────────────────
        menu.addAction("◇  Open Mirror Window...").triggered.connect(
            lambda checked: self._open_mirror()
        )
        menu.addAction("◆  Re-select Capture Region...").triggered.connect(
            lambda checked: self._reselect_area()
        )
        zkill_label = "◉  zKill Scan: ON" if self._zkill_scanning else "◉  zKill Scan: OFF"
        menu.addAction(zkill_label).triggered.connect(
            lambda checked: self._toggle_zkill_scan()
        )
        menu.addSeparator()

        # ── Apparence ─────────────────────────────────────────────────────
        menu.addAction("◐  Adjust Transparency...").triggered.connect(
            self._show_transparency_slider
        )

        theme_menu = menu.addMenu("◈  Theme")
        theme_menu.setStyleSheet(
            f"background-color: {self.theme['BG_PANEL']}; "
            f"color: {self.theme['CYAN']}; font-family: \"{mono_family()}\";"
        )
        for name in THEMES:
            theme_menu.addAction(name).triggered.connect(
                lambda checked, n=name: self._apply_theme(n)
            )

        menu.exec(event.globalPos())

    def _show_transparency_slider(self) -> None:
        # Ouvre ou ferme le slider de transparence.
        if self.transparency_slider and self.transparency_slider.isVisible():
            self.transparency_slider.close()
            self.transparency_slider = None
        else:
            slider = TransparencySlider(self)
            slider.opacity_changed.connect(self._on_opacity_changed)
            slider.closed.connect(self._on_slider_closed)
            self.transparency_slider = slider

    def _on_opacity_changed(self, opacity: float) -> None:
        # Applique l'opacité en direct ; l'écriture disque est débouncée.
        self.setWindowOpacity(opacity)
        self.app_config['opacity'] = opacity
        self._opacity_save_timer.start()   # sauvegarde 400 ms après le dernier mouvement

    def _on_slider_closed(self) -> None:
        # Le slider s'est fermé : déréférence et force une dernière sauvegarde.
        self.transparency_slider = None
        if self._opacity_save_timer.isActive():
            self._opacity_save_timer.stop()
            self._save_config()

    # ═════════════════════════════════════════════════════════════════════════
    #  Machine d'état affichée
    # ═════════════════════════════════════════════════════════════════════════

    # Un seul endroit qui mappe état → (texte, clé couleur thème, état brackets).
    # Renommer un libellé ici ne casse plus aucune comparaison ailleurs.
    _STATE_STYLE = {
        "idle":     ("IDLE",      "DIM",    "idle"),
        "clear":    ("ALL CLEAR", "GREEN",  "clear"),
        "hostile":  ("HOSTILE",   "RED",    "hostile"),
        "paused":   ("PAUSED",    "YELLOW", "paused"),
        "nomirror": ("NO MIRROR", "YELLOW", "paused"),
        "monfail":  ("MON FAIL",  "RED",    "idle"),
        "checking": ("CHECKING",  "CYAN",   "idle"),
    }

    def _set_state(self, state: str) -> None:
        # Change l'état logique affiché et met à jour texte + couleur + brackets.
        #
        # Point unique de vérité : les appelants comparent self._state (pas la
        # chaîne d'animation _tw_target, qui peut porter un message transitoire).
        self._state = state
        text, color_key, bracket = self._STATE_STYLE[state]
        self._typewriter_status(text, self.theme[color_key])
        self._update_brackets(bracket)

    # ═════════════════════════════════════════════════════════════════════════
    #  Animation typewriter
    # ═════════════════════════════════════════════════════════════════════════

    def _typewriter_status(self, text: str, color: str) -> None:
        # Lance l'animation typewriter vers le texte et la couleur donnés (message brut).
        self._tw_timer.stop()
        self._tw_target = text
        self._tw_color  = color
        self._tw_step   = 0
        self.status_bar.setStyleSheet(f"background-color: {color};")
        self._tw_timer.start(28)

    def _tw_tick(self) -> None:
        # Tick d'animation : scramble les caractères non encore résolus.
        target    = self._tw_target
        step      = self._tw_step
        chars     = self._TW_CHARS
        chars_len = self._TW_CHARS_LEN

        result = [
            ch if (ch == ' ' or step > i + 2)
            else chars[random.randint(0, chars_len - 1)]
            for i, ch in enumerate(target)
        ]

        self.status_lbl.setText("".join(result))
        self.status_lbl.setStyleSheet(
            f"color: {self._tw_color}; font-size: 14px; font-weight: bold;"
        )

        self._tw_step += 1
        if self._tw_step > len(target) + 3:
            self.status_lbl.setText(target)
            self._tw_timer.stop()

    # ═════════════════════════════════════════════════════════════════════════
    #  Animation brackets
    # ═════════════════════════════════════════════════════════════════════════

    def _update_brackets(self, state: str) -> None:
        # Change la couleur et l'état des brackets ; démarre l'animation si hostile.
        if state == self._bracket_state:
            return

        self._bracket_state = state
        self._bracket_timer.stop()
        self._bracket_offset = 0

        color_map = {
            "hostile": self.theme['RED'],
            "clear":   self.theme['GREEN'],
            "paused":  self.theme['YELLOW'],
        }
        self._bracket_color = hex_to_qcolor(color_map.get(state, self.theme['DIM']))

        if state == "hostile":
            self._bracket_timer.start(35)

        self.update()

    def _animate_bracket_lockon(self) -> None:
        # Avance l'offset des brackets d'un pixel par tick jusqu'à 4 px.
        if self._bracket_offset < 4:
            self._bracket_offset += 1
            self.update()
        else:
            self._bracket_timer.stop()

    # ═════════════════════════════════════════════════════════════════════════
    #  Toggles fake-threat / pause
    # ═════════════════════════════════════════════════════════════════════════

    def _toggle_fake_threat(self) -> None:
        # Active/désactive le mode simulation de menace (F10).
        self.fake_threat_mode = not self.fake_threat_mode
        if not self.fake_threat_mode:
            self._acknowledge_alert()
            self.last_threat_count = 0
            if self._monitoring:
                self._update_monitor()

    def _toggle_pause(self) -> None:
        # Met en pause ou reprend les alertes pendant le monitoring.
        if not self._monitoring:
            return

        self._alerts_paused = not self._alerts_paused
        t = self.theme

        if self._alerts_paused:
            self.alert_acknowledged = True
            self.is_alerting        = False
            self._alert_timer.stop()
            self._set_btn_border(self.pause_btn, t['YELLOW'])
            self._set_state("paused")
            audio.stop_sound()
        else:
            self._set_btn_border(self.pause_btn, t['YELLOW'])
            if self._monitor_errors or not self._has_successful_sample:
                self._set_state("checking")
                if self._monitor_errors:
                    self._typewriter_status(
                        f"ERR {self._monitor_errors}/3", self.theme['YELLOW']
                    )
            elif self.last_threat_count > 0:
                self._set_state("hostile")
                self.alert_acknowledged = False
                self._play_alert()
            else:
                self._set_state("clear")

    # ═════════════════════════════════════════════════════════════════════════
    #  Démarrage / arrêt monitoring
    # ═════════════════════════════════════════════════════════════════════════

    def _toggle_monitoring(self) -> None:
        # Démarre ou arrête le monitoring selon l'état courant et la bbox disponible.
        if self._monitoring:
            self._stop_monitoring()
            return

        # Vérifie qu'on a une bbox valide avant de démarrer
        if self._relative_bbox:
            if not self.mirror_window:
                mb = self.app_config.get("mirror_bbox")
                if mb:
                    self._create_mirror(mb)
                    if self.mirror_window:
                        self._start_monitoring()
                else:
                    self._typewriter_status("REQ F2", self.theme['YELLOW'])
            else:
                self._start_monitoring()
        elif self._bbox:
            self._start_monitoring()
        else:
            self._typewriter_status("REQ F2/F3", self.theme['YELLOW'])

    def _start_monitoring(self) -> None:
        # Initialise l'état et démarre le timer de poll à 1 Hz (idempotent).
        if self._monitoring:
            return   # déjà démarré — évite de ré-initialiser l'état en plein session

        try:
            capture = mss.MSS()
        except Exception as exc:
            log.error("initialisation capture impossible : %s", exc)
            self._monitoring = False
            self._sct = None
            self.monitor_timer.stop()
            audio.send_notification(
                "Monitoring unavailable", "Screen capture could not start"
            )
            audio.play_alert(SOUND_FILE, SOUND_FILE_EXISTS)
            self._set_state("monfail")
            self._update_titlebar_tooltip()
            return

        self._monitoring        = True
        self.last_threat_count  = 0
        self.alert_acknowledged = True
        self._alerts_paused     = False
        self.is_alerting        = False
        self._monitor_errors    = 0
        self._has_successful_sample = False

        t = self.theme
        # Remplace l'icône ▶ par un carré rouge ■ pendant le monitoring
        pm = QPixmap(14, 14)
        pm.fill(QColor(0, 0, 0, 0))
        p = QPainter(pm)
        p.setBrush(hex_to_qcolor(t['RED']))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(3, 3, 8, 8)
        p.end()
        self.play_btn.setPixmap(pm)
        self._set_btn_border(self.play_btn, t['RED'])

        self._sct = capture

        self.threat_lbl.setText("Neuts: –")
        self.ally_lbl.setText("Friends: –")
        self._update_titlebar_tooltip()
        self._set_state("checking")
        self.monitor_timer.start(1000)
        self._update_monitor()

    def _stop_monitoring(self) -> None:
        # Arrête le timer, ferme mss et remet l'UI en état IDLE.
        self._acknowledge_alert()
        self._monitoring = False
        self._has_successful_sample = False
        self.monitor_timer.stop()

        if self._sct:
            self._sct.close()
            self._sct = None

        t = self.theme
        # Restaure le triangle vert ▶
        self._repaint_icon_btns()
        self._set_btn_border(self.play_btn,  t['GREEN'])
        self._set_btn_border(self.pause_btn, t['YELLOW'])
        self.threat_lbl.setText("Neuts: –")
        self.ally_lbl.setText("Friends: –")
        self._update_titlebar_tooltip()
        self._set_state("idle")

    # ═════════════════════════════════════════════════════════════════════════
    #  Zone de détection / miroir
    # ═════════════════════════════════════════════════════════════════════════

    def _reselect_area(self) -> None:
        # Ouvre le sélecteur de zone de détection (F3), arrête le monitoring si actif.
        if self._monitoring:
            self._stop_monitoring()

        # L'échelle DPI ou la disposition des écrans a pu changer depuis le
        # démarrage : une nouvelle sélection F3 ne doit surtout pas repasser
        # par un mapping périmé.
        self._screen_mappings = None
        try:
            prev = self._resolve_detection_bbox()
        except Exception as exc:
            log.warning("previous detection mapping unavailable: %s", exc)
            prev = None
        self._open_selector(self._save_detection_area, prev)

    def _open_mirror(self) -> None:
        # Ferme le miroir existant et ouvre un sélecteur pour en créer un nouveau (F2).
        self._screen_mappings = None
        if self.mirror_window:
            self.mirror_window.close()
            self.mirror_window = None
        self._open_selector(self._create_mirror, self.app_config.get("mirror_bbox"))

    def _open_selector(self, callback, prev_bbox) -> None:
        # Ouvre l'AreaSelector sans jamais laisser une exception remonter.
        #
        # build_screen_mappings peut lever (écrans ambigus, Qt/MSS désaccordés) ;
        # dans un handler F2/F3, une exception non rattrapée aborterait le
        # process. On affiche un état d'erreur neutre à la place.
        try:
            self.sel_window = AreaSelector(callback, prev_bbox)
        except Exception as exc:
            log.error("area selector unavailable: %s", exc)
            self._typewriter_status("SCREEN ERR", self.theme['YELLOW'])

    def _create_mirror(self, bbox: dict) -> None:
        # Crée une MirrorWindow sur la bbox donnée, câble ses signaux et sauvegarde.
        previous_bbox = self.app_config.get("mirror_bbox")
        if self.mirror_window:
            self.mirror_window.close()

        try:
            mw = MirrorWindow(bbox, self.theme, self.app_config.get("mirror_position"))
        except Exception as exc:
            # bbox sur un écran débranché, mapping introuvable… Une exception
            # non rattrapée dans un slot Qt (singleShot du démarrage, F1)
            # aborterait tout le process : on dégrade en resélection.
            log.warning("mirror creation failed for %s: %s", bbox, exc)
            self.mirror_window = None
            self.app_config.pop("mirror_bbox", None)
            self._save_config()
            self._typewriter_status("REQ F2", self.theme['YELLOW'])
            return
        mw.position_changed.connect(self._on_mirror_moved)
        mw.closed.connect(self._on_mirror_closed)
        self.mirror_window = mw
        self._nomirror_notified = False

        # Les offsets relatifs n'ont de sens QUE pour la mirror_bbox contre
        # laquelle ils ont été calculés. Après un F2 vers une AUTRE région,
        # ils viseraient du contenu arbitraire — capture qui RÉUSSIT mais lit
        # les mauvais pixels (faux ALL CLEAR). La bbox absolue héritée du même
        # tracé est tout aussi périmée : on purge les deux et on exige F3.
        if self._relative_bbox is not None and previous_bbox != bbox:
            self._relative_bbox = None
            self._bbox = None
            self.app_config.pop("relative_bbox", None)
            self.app_config.pop("detection_bbox", None)
            self._typewriter_status("REQ F3", self.theme['YELLOW'])

        self.app_config["mirror_bbox"]     = bbox
        # Persiste la position initiale (sauvegardée ou centrée+recadrée).
        self.app_config["mirror_position"] = {'x': mw.x(), 'y': mw.y()}
        self._save_config()

    def _on_mirror_moved(self, pos: dict) -> None:
        # Le miroir a été déplacé : persiste sa nouvelle position.
        self.app_config["mirror_position"] = pos
        self._save_config()

    def _on_mirror_closed(self) -> None:
        # Le miroir s'est fermé : déréférence et avertit si la détection en dépendait.
        self.mirror_window = None
        if self._monitoring and self._relative_bbox:
            if self._state != "nomirror":
                self._set_state("nomirror")
            if not self._nomirror_notified:
                audio.send_notification(
                    "Monitoring suspended",
                    "Mirror closed — protection is waiting for a capture source",
                )
                self._nomirror_notified = True

    def _resolve_detection_bbox(self):
        # Retourne la bbox absolue de détection, ou None si irrésolvable.
        #
        # En mode relatif, la bbox est convertie en coords écran via la position
        # courante du miroir. Si le miroir est fermé alors qu'on est en mode
        # relatif, on renvoie None PLUTÔT QUE de retomber sur l'ancienne bbox
        # absolue : lire une zone périmée produirait de faux "ALL CLEAR" et
        # acquitterait des alertes réelles. L'appelant affiche alors "NO MIRROR".
        if self._relative_bbox:
            if self.mirror_window:
                content = self.mirror_window.get_content_qt_rect()
                logical = QRect(
                    content.x() + self._relative_bbox['offset_left'],
                    content.y() + self._relative_bbox['offset_top'],
                    self._relative_bbox['width'],
                    self._relative_bbox['height'],
                )
                if self._screen_mappings is None:
                    self._screen_mappings = build_screen_mappings()
                return qt_rect_to_native_bbox(logical, self._screen_mappings)
            return None
        return self._bbox

    def _save_detection_area(self, bbox: dict) -> None:
        # Enregistre la bbox de détection et calcule la bbox relative si dans le miroir.
        self._bbox = bbox

        if self.mirror_window:
            try:
                if self._screen_mappings is None:
                    self._screen_mappings = build_screen_mappings()
                selected = native_bbox_to_qt_rect(bbox, self._screen_mappings)
                content = self.mirror_window.get_content_qt_rect()
                rl = selected.x() - content.x()
                rt = selected.y() - content.y()
                inside = (
                    rl >= 0
                    and rt >= 0
                    and rl + selected.width() <= content.width()
                    and rt + selected.height() <= content.height()
                )
            except Exception as exc:
                log.warning("relative detection bbox conversion failed: %s", exc)
                inside = False

            if inside:
                self._relative_bbox = {
                    'offset_left': rl,
                    'offset_top':  rt,
                    'width':       selected.width(),
                    'height':      selected.height(),
                }
            else:
                self._relative_bbox = None
        else:
            self._relative_bbox = None

        self.app_config["detection_bbox"]  = bbox
        self.app_config["relative_bbox"]   = self._relative_bbox
        self.app_config["coordinate_space_version"] = 2
        self._save_config()

        if not self._monitoring:
            self._typewriter_status("READY", self.theme['CYAN'])

    # ═════════════════════════════════════════════════════════════════════════
    #  Poll monitoring (1 Hz)
    # ═════════════════════════════════════════════════════════════════════════

    def _update_monitor(self) -> None:
        # Capture la zone, détecte les menaces et met à jour l'état + UI.
        if not self._monitoring:
            return

        try:
            if self.fake_threat_mode:
                t_cnt, a_cnt = random.randint(1, 4), 0
            else:
                if (
                    self._relative_bbox
                    and self.mirror_window is not None
                    and not self.mirror_window.capture_healthy
                ):
                    # La capture SOURCE du miroir échoue : son label affiche un
                    # frame FIGÉ. L'analyser serait un « échantillon réussi »
                    # sur des pixels périmés → faux ALL CLEAR. On attend en
                    # CHECKING que le miroir redevienne sain (il se ferme seul
                    # après ~10 s d'échec → chemin NO MIRROR).
                    if self._alerts_paused:
                        if self._state != "paused":
                            self._set_state("paused")
                    elif self._state != "checking":
                        self._set_state("checking")
                    return
                bbox = self._resolve_detection_bbox()
                if bbox is None:
                    # Miroir fermé en mode relatif : ne pas analyser une zone
                    # périmée. On signale et on attend son retour (F2/F3).
                    if self._state != "nomirror":
                        self._set_state("nomirror")
                    return
                # Ne capture que la colonne d'icônes de standing (~12px) au lieu
                # de toute la région : détection lit alors depuis x=0.
                strip = strip_bbox(bbox)
                t_cnt, a_cnt = detect_threats(self._sct.grab(strip), x_start=0)
                self._has_successful_sample = True

            recovered_from_error = self._monitor_errors > 0
            self._monitor_errors = 0

            # Indicateur de delta
            if t_cnt != self.last_threat_count:
                diff   = t_cnt - self.last_threat_count
                sign   = "+" if diff > 0 else ""
                self.threat_diff_text  = f"  [{sign}{diff}]"
                self.threat_diff_ticks = 3

            if self.threat_diff_ticks > 0:
                self.threat_diff_ticks -= 1
            else:
                self.threat_diff_text = ""

            self.threat_lbl.setText(f"Neuts: {t_cnt}{self.threat_diff_text}")
            self.ally_lbl.setText(f"Friends: {a_cnt}")
            self._update_titlebar_tooltip()

            # ── Machine d'état ───────────────────────────────────────────
            if self._alerts_paused:
                if self._state != "paused" or recovered_from_error:
                    self._set_state("paused")
                self.last_threat_count = t_cnt
                return

            if t_cnt > 0:
                if self._state != "hostile" or recovered_from_error:
                    self._set_state("hostile")
                if t_cnt > self.last_threat_count:
                    self.alert_acknowledged = False
                    if not self.is_alerting:
                        self._play_alert()
            else:
                if self._state != "clear" or recovered_from_error:
                    self._set_state("clear")
                self._acknowledge_alert()

            self.last_threat_count = t_cnt

        except Exception as exc:
            self._monitor_errors += 1
            log.error("_update_monitor (%d/3) : %s", self._monitor_errors, exc)
            if self._monitor_errors >= 3:
                log.error("3 erreurs consécutives — arrêt du monitoring.")
                self._stop_monitoring()
                # L'app est là pour surveiller pendant qu'on joue : un arrêt qui
                # retombe en IDLE silencieux ferait croire qu'on est protégé.
                # On alerte (son + toast) et on affiche un état distinct.
                audio.send_notification("Monitoring stopped",
                                        "Capture errors — protection is OFF")
                audio.play_alert(SOUND_FILE, SOUND_FILE_EXISTS)
                self._set_state("monfail")
            else:
                self._typewriter_status(f"ERR {self._monitor_errors}/3", self.theme['YELLOW'])

    # ═════════════════════════════════════════════════════════════════════════
    #  Alertes / acquittement
    # ═════════════════════════════════════════════════════════════════════════

    def _play_alert(self) -> None:
        # Démarre la boucle d'alerte : notification + son, puis répétition via timer.
        if self._alerts_paused or self.alert_acknowledged or not self._monitoring:
            return
        if self.is_alerting:
            return   # boucle déjà active — ne pas démarrer un second timer

        self.is_alerting = True
        audio.send_notification("THREAT DETECTED!", "Hostile(s) in local!")
        audio.play_alert(SOUND_FILE, SOUND_FILE_EXISTS)
        self._alert_timer.start()

    def _alert_tick(self) -> None:
        # Re-joue le son toutes les 5 s tant que l'alerte n'est pas acquittée.
        if self._alerts_paused or self.alert_acknowledged or not self._monitoring:
            self._alert_timer.stop()
            self.is_alerting = False
            return
        audio.play_alert(SOUND_FILE, SOUND_FILE_EXISTS)

    def _acknowledge_alert(self) -> None:
        # Acquitte l'alerte courante et stoppe la boucle sonore.
        if self.is_alerting:
            # Coupe aussi le WAV en cours de lecture, pas seulement la
            # répétition. Gardé par is_alerting : le chemin « clear » appelle
            # cet ack à chaque poll (1 Hz) — pas de PlaySound(None) en boucle.
            audio.stop_sound()
        self.alert_acknowledged = True
        self.is_alerting        = False
        self._alert_timer.stop()

    # ═════════════════════════════════════════════════════════════════════════
    #  Config / démarrage automatique
    # ═════════════════════════════════════════════════════════════════════════

    def _check_and_auto_start(self) -> None:
        # Recrée le miroir automatiquement si une mirror_bbox est en config.
        mb = self.app_config.get("mirror_bbox")
        if mb and 'width' in mb:
            QTimer.singleShot(100, lambda: self._create_mirror(mb))

    def _restore_position(self) -> None:
        # Restaure la position sauvegardée, recadrée à l'écran (sinon (100, 100)).
        x, y = 100, 100
        geom = self.app_config.get("win_geom")
        if isinstance(geom, dict) and 'x' in geom and 'y' in geom:
            x, y = geom['x'], geom['y']
        elif isinstance(geom, str):
            try:
                parts = geom.replace('-', '+').split('+')
                x, y = int(parts[1]), int(parts[2])
            except (IndexError, ValueError):
                x, y = 100, 100
        # Recadre : évite une fenêtre frameless coincée hors-écran (moniteur
        # secondaire débranché) et donc injoignable.
        x, y = clamp_to_screen(x, y, self.width(), self.height())
        self.move(x, y)

    # ═════════════════════════════════════════════════════════════════════════
    #  Fermeture
    # ═════════════════════════════════════════════════════════════════════════

    def closeEvent(self, event) -> None:
        # Cache vers un tray utilisable, sinon effectue une fermeture réelle.
        if self._quitting:
            event.accept()
            super().closeEvent(event)
            return
        if self._tray_usable:
            self.hide()
            event.ignore()
            return
        self._real_close()
        event.accept()

    def _real_close(self) -> None:
        # Sauvegarde la géométrie, ferme toutes les sous-fenêtres et quitte l'app.
        if self._quitting:
            return
        self._quitting = True
        self._stop_monitoring()
        if self._zkill_card:
            self._zkill_card.close()
            self._zkill_card = None

        if self.isVisible():
            self.app_config["win_geom"] = {'x': self.x(), 'y': self.y()}
        if self.mirror_window and self.mirror_window.isVisible():
            self.app_config["mirror_position"] = {
                'x': self.mirror_window.x(), 'y': self.mirror_window.y()
            }

        self._save_config()

        if self.mirror_window:
            self.mirror_window.close()

        QApplication.quit()

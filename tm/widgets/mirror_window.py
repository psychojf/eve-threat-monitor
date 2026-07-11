# -*- coding: utf-8 -*-
# Fenêtre flottante de prévisualisation live de la région EVE sélectionnée.
# Déplaçable, position persistée en config (via signaux vers le monitor).
#
# Perf : rafraîchit à ~10 FPS (largement suffisant pour un aperçu de liste), ne
# recopie/reconvertit/repaint que lorsque l'image a réellement changé, et tolère
# quelques erreurs de capture transitoires (Win+L, changement de résolution/DPI,
# veille écran) avant de se fermer.
import logging
import time

import mss
from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout, QHBoxLayout, QFrame
from PyQt6.QtCore    import Qt, QRect, QTimer, pyqtSignal
from PyQt6.QtGui     import QPixmap, QImage

from ..coordinates import native_bbox_to_qt_rect
from ..qtutil import make_overlay_window, clamp_to_screen, mono_family, DragMoveMixin

log = logging.getLogger(__name__)

REFRESH_MS         = 100   # ~10 FPS — un aperçu de member-list n'a pas besoin de 30
CAPTURE_ERROR_TOLERANCE_SECONDS = 10.0
CAPTURE_ERROR_LOG_INTERVAL_SECONDS = 1.0


class MirrorWindow(DragMoveMixin, QWidget):
    # Aperçu live de la zone de capture EVE dans une fenêtre sans bordure.

    position_changed = pyqtSignal(dict)   # {'x', 'y'} après un déplacement
    closed           = pyqtSignal()       # émis à la fermeture

    def __init__(self, bbox: dict, theme: dict, saved_position=None) -> None:
        # bbox est en pixels NATIFS (espace MSS) ; la taille d'affichage est
        # dérivée en unités logiques Qt via native_bbox_to_qt_rect, sinon la
        # fenêtre serait 1,5× trop grande sur un écran à 150 %. Peut lever si
        # la bbox n'est plus sur un écran branché — l'appelant (_create_mirror)
        # rattrape et demande une resélection.
        super().__init__()
        self.bbox  = bbox
        self.theme = theme
        self._source_qt_rect = native_bbox_to_qt_rect(bbox)

        # Fond opaque (géré par stylesheet) → pas de WA_TranslucentBackground
        make_overlay_window(self, translucent=False)

        self._build_layout()
        self._apply_theme()
        self._position_window(saved_position)

        self._sct      = mss.MSS()
        # Santé de la capture SOURCE, lue par le moniteur : pendant une panne
        # le label garde son dernier frame FIGÉ — l'analyser produirait un
        # faux ALL CLEAR. False tant qu'aucun grab n'a réussi.
        self.capture_healthy = False
        self._last_raw = None       # dernier frame brut, pour sauter les frames identiques
        self._capture_error_since = None
        self._last_capture_error_log = None
        self._timer    = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(REFRESH_MS)

        self.show()

    # ── Layout ───────────────────────────────────────────────────────────────

    def _build_layout(self) -> None:
        # Construit la barre de titre et le label image.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._top_bar = QFrame()
        self._top_bar.setFixedHeight(20)
        top_row = QHBoxLayout(self._top_bar)
        top_row.setContentsMargins(2, 0, 2, 0)
        top_row.addStretch()

        self._close_btn = QLabel("×")
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.mousePressEvent = lambda e: self.close()
        top_row.addWidget(self._close_btn)

        self._image_lbl = QLabel()
        self._image_lbl.setFixedSize(self._source_qt_rect.size())

        layout.addWidget(self._top_bar)
        layout.addWidget(self._image_lbl)
        self.setFixedSize(
            self._source_qt_rect.width(), self._source_qt_rect.height() + 20
        )

    def _apply_theme(self) -> None:
        # Applique le thème couleur à la fenêtre.
        t = self.theme
        self.setStyleSheet(f"""
            MirrorWindow {{ background-color: {t['BORDER']}; }}
            QFrame        {{ background-color: {t['BG']}; }}
            QLabel        {{ color: {t['DIM']}; font-weight: bold; font-family: "{mono_family()}"; }}
            QLabel:hover  {{ color: {t['RED']}; }}
        """)

    def _position_window(self, saved_position) -> None:
        # Positionne la fenêtre (position sauvegardée ou centrée), recadrée à l'écran.
        if saved_position and 'x' in saved_position and 'y' in saved_position:
            x, y = saved_position['x'], saved_position['y']
        else:
            screen = self.screen().availableGeometry()
            x = screen.x() + (screen.width()  - self.width())  // 2
            y = screen.y() + (screen.height() - self.height()) // 2
        x, y = clamp_to_screen(x, y, self.width(), self.height())
        self.move(x, y)

    # ── Live refresh ─────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        # Capture l'écran et met à jour l'image — seulement si le frame a changé.
        try:
            sct_img = self._sct.grab(self.bbox)
        except Exception as exc:
            # Frame affiché désormais périmé : le moniteur suspend l'analyse.
            self.capture_healthy = False
            now = time.monotonic()
            if self._capture_error_since is None:
                self._capture_error_since = now
            elapsed = now - self._capture_error_since
            if (
                self._last_capture_error_log is None
                or now - self._last_capture_error_log
                >= CAPTURE_ERROR_LOG_INTERVAL_SECONDS
            ):
                log.error(
                    "MirrorWindow._refresh (%.1fs/%.1fs): %s",
                    elapsed,
                    CAPTURE_ERROR_TOLERANCE_SECONDS,
                    exc,
                )
                self._last_capture_error_log = now
            if elapsed >= CAPTURE_ERROR_TOLERANCE_SECONDS:
                self.close()   # échec persistant — on abandonne l'aperçu
            return
        self._capture_error_since = None
        self._last_capture_error_log = None
        # Grab réussi : le contenu affiché reflète bien l'écran source —
        # marqué sain AVANT le saut de frame identique (le grab a réussi).
        self.capture_healthy = True

        raw = sct_img.raw
        if raw == self._last_raw:
            return   # image identique (liste statique) → ni conversion ni repaint
        self._last_raw = bytes(raw)

        # Format_RGB32 : ignore l'octet alpha (invalide côté mss) ; QPixmap.fromImage
        # copie immédiatement donc la durée de vie de `raw` est sûre.
        img = QImage(
            raw, sct_img.width, sct_img.height,
            sct_img.width * 4, QImage.Format.Format_RGB32,
        )
        pixmap = QPixmap.fromImage(img)
        if pixmap.deviceIndependentSize().toSize() != self._image_lbl.size():
            pixmap = pixmap.scaled(
                self._image_lbl.size(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._image_lbl.setPixmap(pixmap)

    # ── Public helpers ───────────────────────────────────────────────────────

    def get_content_position(self) -> dict:
        # Retourne {'x', 'y'} de la zone image (sous la barre de titre).
        rect = self.get_content_qt_rect()
        return {'x': rect.x(), 'y': rect.y()}

    def get_content_qt_rect(self) -> QRect:
        # Retourne la zone image globale en coordonnées logiques Qt.
        return QRect(
            self.x(),
            self.y() + self._top_bar.height(),
            self._image_lbl.width(),
            self._image_lbl.height(),
        )

    # ── Drag (via DragMoveMixin) ───────────────────────────────────────────────

    def _on_drag_end(self) -> None:
        # Notifie le monitor de la nouvelle position (persistée côté monitor).
        self.position_changed.emit({'x': self.x(), 'y': self.y()})

    # ── Cleanup ──────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        # Arrête le timer, libère mss et notifie le monitor.
        self._timer.stop()
        self._sct.close()
        self.closed.emit()
        super().closeEvent(event)

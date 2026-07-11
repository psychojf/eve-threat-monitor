# -*- coding: utf-8 -*-
# Overlay plein écran transparent pour sélectionner une zone de capture à la souris.
# Entrée confirme (renvoie la bbox via callback), Échap annule silencieusement.
import logging

from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore    import Qt, QRect
from PyQt6.QtGui     import QPainter, QColor, QPen

from ..coordinates import (
    build_screen_mappings,
    native_bbox_to_qt_rect,
    qt_rect_to_native_bbox,
    virtual_qt_geometry,
)
from ..qtutil import make_overlay_window


log = logging.getLogger(__name__)


def _fit_minimum_rect(
    rect: QRect,
    bounds: QRect,
    min_width: int = 10,
    min_height: int = 10,
) -> QRect:
    # Agrandit une sélection à sa taille minimale (10×10) sans quitter son
    # écran : un clic sans glisser donnait un rect 1×1 inutilisable pour la
    # détection, et l'agrandissement ne doit pas déborder sur l'écran voisin
    # (deux échelles DPI = conversion fausse).
    rect = QRect(rect).normalized()
    bounds = QRect(bounds)
    width = min(max(rect.width(), min_width), bounds.width())
    height = min(max(rect.height(), min_height), bounds.height())
    x = min(
        max(rect.x(), bounds.x()),
        bounds.x() + bounds.width() - width,
    )
    y = min(
        max(rect.y(), bounds.y()),
        bounds.y() + bounds.height() - height,
    )
    return QRect(x, y, width, height)


def _rect_is_inside(rect: QRect, bounds: QRect) -> bool:
    # Contenance complète avec bords droite/bas EXCLUSIFS — les bords
    # inclusifs de QRect.contains() laissaient passer un débordement d'un
    # pixel sur l'écran voisin.
    return (
        rect.x() >= bounds.x()
        and rect.y() >= bounds.y()
        and rect.x() + rect.width() <= bounds.x() + bounds.width()
        and rect.y() + rect.height() <= bounds.y() + bounds.height()
    )


class AreaSelector(QWidget):
    # Sélecteur de région par glisser-déposer sur un overlay plein écran.

    def __init__(self, callback, prev_bbox=None) -> None:
        # Construit l'overlay sur tout le bureau virtuel ; la sélection
        # précédente (native) est convertie en logique pour être peinte en
        # pointillés — si la conversion échoue (écran débranché), on affiche
        # l'erreur au lieu de crasher, et l'utilisateur resélectionne.
        super().__init__()
        self.callback  = callback
        self.prev_bbox = None
        self.selection_error = None

        make_overlay_window(self)
        self.setCursor(Qt.CursorShape.CrossCursor)

        self._mappings = build_screen_mappings()
        desktop = virtual_qt_geometry()
        self.setGeometry(desktop)

        self.start_pos    = None
        self.current_rect = None
        self._selection_bounds = None
        self._previous_qt_rect = None
        if prev_bbox is not None:
            try:
                self._previous_qt_rect = native_bbox_to_qt_rect(
                    prev_bbox, self._mappings
                )
            except Exception as exc:
                self._set_selection_error(
                    f"Previous selection is invalid: {str(exc) or 'conversion failed'}"
                )
            else:
                self.prev_bbox = prev_bbox

        self.show()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.activateWindow()
        self.setFocus()
        self.grabKeyboard()

    # ── Paint ────────────────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:
        # Dessine l'assombrissement de l'écran et le rectangle de sélection.
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 76))

        if self.current_rect is not None:
            painter.setPen(QPen(QColor(255, 0, 0), 3, Qt.PenStyle.SolidLine))
            painter.drawRect(
                self.current_rect.translated(-self.geometry().x(), -self.geometry().y())
            )
        elif self._previous_qt_rect is not None:
            painter.setPen(QPen(QColor(255, 255, 0), 3, Qt.PenStyle.DashLine))
            painter.drawRect(
                self._previous_qt_rect.translated(
                    -self.geometry().x(), -self.geometry().y()
                )
            )
        if self.selection_error:
            painter.setPen(QPen(QColor(255, 80, 80), 2, Qt.PenStyle.SolidLine))
            painter.drawText(20, 30, self.selection_error)

    def _set_selection_error(self, message: str) -> None:
        # Garde la sélection invalide ÉDITABLE et affiche pourquoi elle est
        # refusée — fermer l'overlay sans explication laissait l'utilisateur
        # dans le noir. Logue seulement au changement (pas à chaque repaint).
        if self.selection_error != message:
            log.warning("Area selection rejected: %s", message)
        self.selection_error = message
        self.update()

    def _clear_selection_error(self) -> None:
        # Efface le message d'erreur affiché (si présent) et repeint.
        if self.selection_error is not None:
            self.selection_error = None
            self.update()

    # ── Mouse ────────────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        # Démarre la sélection au point de clic.
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.start_pos = event.globalPosition().toPoint()
        self._selection_bounds = next(
            (
                QRect(mapping.logical)
                for mapping in self._mappings
                if (
                    mapping.logical.x() <= self.start_pos.x()
                    < mapping.logical.x() + mapping.logical.width()
                    and mapping.logical.y() <= self.start_pos.y()
                    < mapping.logical.y() + mapping.logical.height()
                )
            ),
            None,
        )
        if self._selection_bounds is None:
            self.start_pos = None
            self.current_rect = None
            self._set_selection_error("Selection must begin on one screen")
            return
        self.current_rect = QRect(self.start_pos, self.start_pos)
        self._clear_selection_error()
        self.update()

    def mouseMoveEvent(self, event) -> None:
        # Met à jour le rectangle de sélection en temps réel.
        if self.start_pos is not None and self._selection_bounds is not None:
            end = event.globalPosition().toPoint()
            self.current_rect = QRect(
                self.start_pos,
                end,
            ).normalized()
            if _rect_is_inside(self.current_rect, self._selection_bounds):
                self._clear_selection_error()
            else:
                self._set_selection_error(
                    "Selection must be fully contained in one screen"
                )
            self.update()

    # ── Keyboard ─────────────────────────────────────────────────────────────

    def keyPressEvent(self, event) -> None:
        # Entrée : confirme la sélection et appelle le callback. Échap : annule.
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            if self.current_rect is not None and self._selection_bounds is not None:
                if not _rect_is_inside(
                    self.current_rect, self._selection_bounds
                ):
                    self._set_selection_error(
                        "Selection must be fully contained in one screen"
                    )
                    return
                fitted = _fit_minimum_rect(
                    self.current_rect, self._selection_bounds
                )
                try:
                    bbox = qt_rect_to_native_bbox(fitted, self._mappings)
                except Exception as exc:
                    self._set_selection_error(str(exc) or "Coordinate conversion failed")
                    return
                self._clear_selection_error()
                self.callback(bbox)
            elif self.prev_bbox is not None:
                self.callback(self.prev_bbox)
            else:
                self._set_selection_error(
                    "Select an area fully contained in one screen"
                )
                return
            self.releaseKeyboard()
            self.close()
        elif key == Qt.Key.Key_Escape:
            event.accept()
            self.releaseKeyboard()
            self.close()

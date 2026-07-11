# -*- coding: utf-8 -*-
# Petits utilitaires Qt partagés — évite de dupliquer le boilerplate de fenêtre
# overlay (sans bordure, toujours au-dessus, type Tool), le drag-to-move et le
# recadrage à l'écran dans chaque widget.
from PyQt6.QtCore import Qt
from PyQt6.QtGui  import QFontDatabase, QGuiApplication


# Familles candidates par ordre de préférence : Consolas d'abord pour garder le
# rendu Windows historique à l'identique, puis les monospaces standard de macOS
# et des distributions Linux courantes.
_MONO_CANDIDATES = (
    "Consolas", "Menlo", "SF Mono", "DejaVu Sans Mono",
    "Ubuntu Mono", "Liberation Mono", "Noto Sans Mono",
)
_mono_family = None


def mono_family() -> str:
    # Famille monospace du HUD, résolue une seule fois. Coder « Consolas » en
    # dur rendait la police aléatoire (proportionnelle) sur macOS/Linux où elle
    # n'existe pas. Nécessite une QGuiApplication vivante — n'appeler que
    # depuis du code widget, jamais au niveau module.
    global _mono_family
    if _mono_family is None:
        available = set(QFontDatabase.families())
        _mono_family = next(
            (name for name in _MONO_CANDIDATES if name in available),
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family(),
        )
    return _mono_family


def make_overlay_window(widget, translucent: bool = True) -> None:
    # Configure un widget en overlay : sans bordure, toujours au premier plan, Tool.
    #
    # translucent=True active WA_TranslucentBackground (fond peint manuellement).
    # Le miroir passe False car il a un fond opaque géré par stylesheet.
    widget.setWindowFlags(
        Qt.WindowType.FramelessWindowHint  |
        Qt.WindowType.WindowStaysOnTopHint |
        Qt.WindowType.Tool
    )
    if translucent:
        widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)


def clamp_to_screen(x: int, y: int, w: int, h: int) -> tuple[int, int]:
    # Garantit que toute la fenêtre w×h reste dans la zone disponible d'un écran.
    #
    # Les fenêtres sont frameless/Tool (pas de barre de titre ni d'entrée taskbar) :
    # une position sauvegardée sur un moniteur depuis débranché les rendrait
    # injoignables. L'écran qui recouvre la plus grande partie de la fenêtre est
    # choisi ; sans recouvrement, le centre le plus proche est utilisé.
    geometries = [screen.availableGeometry() for screen in QGuiApplication.screens()]
    if not geometries:
        primary = QGuiApplication.primaryScreen()
        if primary is None:
            return x, y
        geometries = [primary.availableGeometry()]

    right = x + max(0, w)
    bottom = y + max(0, h)
    center_x = x + w / 2.0
    center_y = y + h / 2.0

    def rank(g):
        intersection_width = max(
            0, min(right, g.x() + g.width()) - max(x, g.x())
        )
        intersection_height = max(
            0, min(bottom, g.y() + g.height()) - max(y, g.y())
        )
        area = intersection_width * intersection_height
        screen_center_x = g.x() + g.width() / 2.0
        screen_center_y = g.y() + g.height() / 2.0
        distance = (
            (center_x - screen_center_x) ** 2
            + (center_y - screen_center_y) ** 2
        )
        return (-area, distance, g.x(), g.y(), g.width(), g.height())

    g = min(geometries, key=rank)
    nx = g.x() if w >= g.width() else min(
        max(x, g.x()), g.x() + g.width() - w
    )
    ny = g.y() if h >= g.height() else min(
        max(y, g.y()), g.y() + g.height() - h
    )
    return nx, ny


class DragMoveMixin:
    # Rend une fenêtre sans bordure déplaçable au bouton gauche.
    #
    # Factorise le trio press/move/release recopié dans plusieurs widgets. Une
    # sous-classe peut redéfinir `_on_drag_end()` pour réagir à la fin d'un drag
    # (ex. persister ou émettre la nouvelle position). À utiliser en premier
    # parent : `class MaFenetre(DragMoveMixin, QWidget)`.
    _drag_pos = None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_pos is not None:
            self._drag_pos = None
            self._on_drag_end()

    def _on_drag_end(self) -> None:
        # Hook optionnel appelé à la fin d'un drag. No-op par défaut.
        pass

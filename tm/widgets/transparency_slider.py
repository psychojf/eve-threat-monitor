# -*- coding: utf-8 -*-
# Popup compact pour régler l'opacité de la fenêtre principale (20–100 %).
#
# Émet opacity_changed(float) à chaque mouvement (le monitor applique et persiste,
# en débounçant l'écriture disque) et closed() à la fermeture — le slider ne
# touche plus directement l'état du parent.
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider
from PyQt6.QtCore    import Qt, pyqtSignal
from PyQt6.QtGui     import QPainter, QPen

from ..themes import hex_to_qcolor
from ..qtutil import make_overlay_window, clamp_to_screen, mono_family, DragMoveMixin


class TransparencySlider(DragMoveMixin, QWidget):
    # Curseur d'opacité flottant, déplaçable, lié à la fenêtre parente.

    opacity_changed = pyqtSignal(float)   # 0.2–1.0, en direct
    closed          = pyqtSignal()

    def __init__(self, parent_monitor) -> None:
        # Construit le popup sous la fenêtre principale, recadré entier à
        # l'écran (près du bord bas, il s'ouvrirait sinon hors de vue).
        super().__init__()
        # parent_monitor est lu uniquement (thème, géométrie, opacité initiale) —
        # aucune écriture d'état : la persistance passe par opacity_changed.
        self.parent_monitor = parent_monitor

        make_overlay_window(self)
        self.setFixedSize(180, 60)

        # Apparaît juste sous la fenêtre parente
        p = parent_monitor.pos()
        x = p.x() + 10
        y = p.y() + parent_monitor.height() + 5
        x, y = clamp_to_screen(x, y, self.width(), self.height())
        self.move(x, y)

        self._build_ui()
        self.show()

    # ── Layout ───────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Construit l'en-tête (titre, valeur, bouton fermer) et le slider.
        t = self.parent_monitor.theme

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        # En-tête : libellé + valeur courante + bouton fermer
        header = QHBoxLayout()
        header.setSpacing(4)

        mono = mono_family()
        title = QLabel("OPACITY")
        title.setStyleSheet(
            f"color: {t['CYAN']}; font-family: \"{mono}\"; font-size: 10px; font-weight: bold;"
        )

        self._value_lbl = QLabel(f"{int(self.parent_monitor.windowOpacity() * 100)}%")
        self._value_lbl.setStyleSheet(
            f"color: {t['WHITE']}; font-family: \"{mono}\"; font-size: 10px;"
        )

        close_btn = QLabel("×")
        close_btn.setStyleSheet(
            f"color: {t['DIM']}; font-family: \"{mono}\"; font-size: 12px; font-weight: bold;"
        )
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.mousePressEvent = self._on_close_clicked

        header.addWidget(title)
        header.addStretch()
        header.addWidget(self._value_lbl)
        header.addWidget(close_btn)
        layout.addLayout(header)

        # Curseur horizontal
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setMinimum(20)
        self._slider.setMaximum(100)
        self._slider.setValue(int(self.parent_monitor.windowOpacity() * 100))
        self._slider.valueChanged.connect(self._on_value_changed)
        self._slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                background: {t['BG']};
                height: 6px;
                border: 1px solid {t['BORDER']};
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: {t['CYAN']};
                width: 12px;
                margin: -4px 0;
                border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: {t['CYAN']};
                border-radius: 2px;
            }}
        """)
        layout.addWidget(self._slider)

    # ── Slots ────────────────────────────────────────────────────────────────

    def _on_value_changed(self, value: int) -> None:
        # Met à jour le libellé et émet l'opacité — l'application/persistance est au monitor.
        self._value_lbl.setText(f"{value}%")
        self.opacity_changed.emit(value / 100.0)

    def _on_close_clicked(self, event) -> None:
        # Ferme le slider (le monitor est notifié via closed).
        event.accept()
        self.close()

    def closeEvent(self, event) -> None:
        # Notifie le monitor de la fermeture.
        self.closed.emit()
        super().closeEvent(event)

    # ── Paint ────────────────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:
        # Dessine le fond opaque et la bordure du popup.
        t       = self.parent_monitor.theme
        painter = QPainter(self)
        painter.fillRect(self.rect(), hex_to_qcolor(t['BG'], 240))
        painter.setPen(QPen(hex_to_qcolor(t['BORDER']), 1))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

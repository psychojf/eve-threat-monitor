# -*- coding: utf-8 -*-
# ZkillCard — popup sans bordure affichant les stats zKillboard d'un pilote.
#
# Rendu en un seul passage QPainter dans paintEvent — pas de widgets enfants,
# pas de bugs de layout, thème respecté.
#
# Pipeline :
#   1. Nom → character_id  via EVE ESI  POST /universe/ids/
#   2. character_id → stats  via zKillboard  GET /api/stats/characterID/{id}/
#   3. Calcul du danger, playstyle, tags, intel étendu
#   4. Tout est peint dans un seul paintEvent
import logging
import webbrowser

from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore    import Qt, QRect, pyqtSignal, QObject
from PyQt6.QtGui     import (
    QPainter, QColor, QPen, QFont, QFontMetrics, QGuiApplication,
)

from .themes      import hex_to_qcolor
from .qtutil      import make_overlay_window, clamp_to_screen, mono_family
from .zkill_worker import submit_lookup

log = logging.getLogger(__name__)

# ── Layout constants ──────────────────────────────────────────────────────────
_H_HEADER    = 26
_H_DANGER    = 47
_H_COMBAT    = 19 + 6 * 16 + 4   # label + 6 lignes + marge bas
_H_PLAYSTYLE = 19 + 2 * 18 + 3   # 2 barres réelles (solo/groupe), plus de split inventé
_H_FOOTER    = 18
_H_SEP       = 1


# ── Signals ───────────────────────────────────────────────────────────────────

class _Signals(QObject):
    # Signaux Qt pour la communication thread → widget principal.
    ready  = pyqtSignal(object)   # dict de stats — pas de round-trip JSON
    error  = pyqtSignal(str)
    closed = pyqtSignal()         # émis quand la carte se ferme (ré-arme le scan)


# ── ZkillCard ─────────────────────────────────────────────────────────────────

W = 270


class ZkillCard(QWidget):
    # Carte zKillboard sans widgets enfants — tout est dessiné à la main via QPainter.
    # Fermeture, lien zkillboard et toggle MORE sont des rects hit-testés dans mousePressEvent.

    def __init__(self, pilot_name: str, theme: dict, parent_rect, on_closed=None) -> None:
        # Crée et affiche la carte pour le pilote donné, lance le fetch en background.
        #
        # on_closed : callback optionnel appelé à la fermeture de la carte (permet
        # au monitor de ré-armer le scan pour que le même nom puisse être re-cherché).
        super().__init__()
        self.theme       = theme
        self._stats      = None
        self._error_msg  = None
        self._pilot_name = pilot_name
        self._parent_rect = QRect(parent_rect)
        self._expanded   = False    # visibilité de la section MORE
        self._closed     = False    # garde contre les signaux après fermeture

        # Rects de clic — recalculés à chaque paintEvent, lus dans mousePressEvent
        self._close_rect = QRect(W - 20, 5, 15, 15)
        self._zkill_rect = QRect(0, 0, 1, 1)
        self._more_rect  = QRect(0, 0, 1, 1)
        self._drag_pos   = None

        make_overlay_window(self)
        self.setFixedWidth(W)
        self.setFixedHeight(64)
        self._place_near(self._parent_rect)

        self._signals = _Signals()
        self._signals.ready.connect(self._on_ready)
        self._signals.error.connect(self._on_error)
        if on_closed is not None:
            self._signals.closed.connect(on_closed)

        # Les callbacks émettent des signaux Qt : leur livraison au thread UI est
        # mise en file automatiquement. Le pool partagé borne les requêtes réseau.
        self._job = submit_lookup(
            pilot_name,
            self._signals.ready.emit,
            self._signals.error.emit,
        )
        self.show()

    # ── Height helpers ────────────────────────────────────────────────────────

    def _base_height(self) -> int:
        # Hauteur de la partie toujours visible.
        return (
            _H_HEADER + _H_DANGER +
            _H_SEP + _H_COMBAT +
            _H_SEP + _H_PLAYSTYLE +
            _H_SEP + self._tags_section_height() +
            _H_SEP + _H_FOOTER
        )

    def _more_section_height(self) -> int:
        # Hauteur de la section intel étendu (dépliable).
        s = self._stats
        if not s:
            return 0
        n_ships = len(s.get("top_ships", []))
        # titre(14) + ligne kills_30d(16) + entête ships(12) + lignes ships + zone + marges
        h = 14 + 16
        if n_ships:
            h += 12 + n_ships * 14
        if s.get("active_zone"):
            h += 14
        h += 8   # bottom padding
        return h

    def _tags_section_height(self) -> int:
        # Hauteur de la section tags, rangées repliées incluses — la carte
        # doit grandir quand les chips passent à la ligne, sinon elles
        # seraient peintes par-dessus le footer.
        tags = self._stats.get("tags", []) if self._stats else []
        return self._tag_layout(tags)[1]

    def _tag_layout(self, tags) -> tuple[list[tuple[str, QRect]], int]:
        # Dispose les chips de tags en rangées sans dépasser le padding de la
        # carte. Mesuré avec QFontMetrics : quatre tags larges débordaient du
        # bord droit avant ce wrapping. Retourne (placements, hauteur totale)
        # pour que calcul de hauteur et dessin partagent la même vérité.
        pad = 10
        right = W - pad
        chip_y = 19
        chip_h = 14
        row_step = 19
        gap = 5
        x = pad
        placements: list[tuple[str, QRect]] = []
        metrics = QFontMetrics(self._f(8, bold=True))

        for tag in tags:
            label = str(tag)
            width = min(metrics.horizontalAdvance(label) + 10, right - pad)
            if x != pad and x + width > right:
                x = pad
                chip_y += row_step
            placements.append((label, QRect(x, chip_y, width, chip_h)))
            x += width + gap

        # Une rangée vide/par défaut préserve la hauteur compacte d'origine.
        return placements, max(36, chip_y + chip_h + 3)

    def _elided_header_name(self, name: str) -> str:
        # Élide un nom de pilote long avant qu'il n'atteigne le bouton fermer.
        # Un nom EVE peut faire 37 caractères : sans élision il recouvrait le
        # « x ». Deux jeux de métriques (police dessinée + police du widget)
        # par prudence — on garde le rendu le plus court des deux.
        available = max(0, self._close_rect.x() - 35)
        draw_metrics = QFontMetrics(self._f(10, bold=True))
        widget_metrics = self.fontMetrics()
        candidates = [
            draw_metrics.elidedText(
                name, Qt.TextElideMode.ElideRight, available
            ),
            widget_metrics.elidedText(
                name, Qt.TextElideMode.ElideRight, available
            ),
        ]
        rendered = min(candidates, key=len)
        while rendered and max(
            draw_metrics.horizontalAdvance(rendered),
            widget_metrics.horizontalAdvance(rendered),
        ) > available:
            if rendered.endswith("…"):
                rendered = rendered[:-2] + "…"
            else:
                rendered = rendered[:-1]
        return rendered

    def _place_near(self, parent_rect: QRect) -> None:
        # Place la carte à droite du parent, sinon à gauche, toujours entière
        # à l'écran — près du bord droit elle s'ouvrait à moitié hors-écran.
        gap = 15
        x = parent_rect.x() + parent_rect.width() + gap
        y = parent_rect.y()
        screen = QGuiApplication.screenAt(parent_rect.center())
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geometry = screen.availableGeometry()
            if x + self.width() > geometry.x() + geometry.width():
                left = parent_rect.x() - self.width() - gap
                if left >= geometry.x():
                    x = left
        self.move(*clamp_to_screen(x, y, self.width(), self.height()))

    def _ensure_on_screen(self) -> None:
        # Re-recadre après tout changement de hauteur (chargement, MORE/LESS) :
        # une carte qui grandit près du bas de l'écran sortirait de l'écran.
        self.move(*clamp_to_screen(
            self.x(), self.y(), self.width(), self.height()
        ))

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_ready(self, stats: dict) -> None:
        # Reçoit le dict de stats, redimensionne et redessine.
        if self._closed:
            return   # carte fermée pendant le fetch — l'objet C++ peut être détruit
        self._stats = stats
        self.setFixedHeight(self._base_height())
        self._ensure_on_screen()
        self.update()

    def _on_error(self, msg: str) -> None:
        # Affiche le message d'erreur à la place des stats.
        if self._closed:
            return
        self._error_msg = msg
        self.setFixedHeight(64)
        self._ensure_on_screen()
        self.update()

    def closeEvent(self, event) -> None:
        # Marque la carte comme fermée, neutralise les signaux en vol et notifie le parent.
        if not self._closed:
            self._closed = True
            self._job.cancel()
            self._signals.closed.emit()
        super().closeEvent(event)

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:
        # Point d'entrée unique du rendu — fond, bordure, puis contenu.
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        p.fillRect(self.rect(), hex_to_qcolor(t['BG']))
        p.setPen(QPen(hex_to_qcolor(t['BORDER']), 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))

        if self._stats is None:
            self._draw_loading(p, t)
        else:
            self._draw_full(p, t)
        p.end()

    def _draw_loading(self, p, t) -> None:
        # Affiche le header + message d'attente ou d'erreur.
        self._draw_header(p, t, self._pilot_name, y=0)
        msg = self._error_msg or "SCANNING..."
        col = self._loading_message_color()
        p.setPen(QColor(col))
        p.setFont(self._f(10))
        p.drawText(QRect(0, 26, W, 38), Qt.AlignmentFlag.AlignCenter, msg)

    def _loading_message_color(self) -> str:
        # Intel indisponible = couleur neutre (DIM), pas rouge menace : une
        # panne réseau ne doit pas ressembler à un pilote dangereux.
        return self.theme['DIM'] if self._error_msg else self.theme['CYAN']

    def _draw_full(self, p, t) -> None:
        # Dessine toutes les sections de la carte dans l'ordre.
        s = self._stats
        y = self._draw_header(p, t, s['name'], y=0)
        y = self._draw_danger(p, t, s, y)
        y = self._draw_sep(p, t, y)
        y = self._draw_combat(p, t, s, y)
        y = self._draw_sep(p, t, y)
        y = self._draw_playstyle(p, t, s, y)
        y = self._draw_sep(p, t, y)
        y = self._draw_tags(p, t, s, y)
        y = self._draw_sep(p, t, y)
        y = self._draw_footer(p, t, s, y)
        if self._expanded:
            y = self._draw_sep(p, t, y)
            self._draw_more(p, t, s, y)

    # ── Section drawers ───────────────────────────────────────────────────────

    def _draw_header(self, p, t, name, y=0) -> int:
        # Dessine le header : avatar initiales, nom du pilote, bouton fermer.
        H = _H_HEADER
        p.fillRect(0, y, W, H, hex_to_qcolor(t['BG_PANEL']))

        # Boîte avatar avec les initiales
        p.fillRect(5, y + 4, 20, 18, hex_to_qcolor(t['BORDER']))
        initials = "".join(w[0] for w in name.split()[:2]).upper() or "??"
        p.setPen(QColor(t['CYAN']))
        p.setFont(self._f(8, bold=True))
        p.drawText(QRect(5, y + 4, 20, 18), Qt.AlignmentFlag.AlignCenter, initials)

        # Nom du pilote
        p.setPen(QColor(t['CYAN']))
        p.setFont(self._f(10, bold=True))
        p.drawText(30, y + 17, self._elided_header_name(name))

        # Bouton fermer
        self._close_rect = QRect(W - 20, y + 5, 15, 15)
        p.setPen(QColor(t['DIM']))
        p.setFont(self._f(12, bold=True))
        p.drawText(self._close_rect, Qt.AlignmentFlag.AlignCenter, "x")

        return y + H

    def _draw_danger(self, p, t, s, y) -> int:
        # Dessine la barre de danger rating et le verdict coloré.
        PAD  = 10
        vcol = QColor(t[s['vcol']])
        y   += 7

        p.setPen(QColor(t['WHITE']))
        p.setFont(self._f(8))
        p.drawText(PAD, y + 9, "DANGER RATING")

        pct_str = f"{s['danger']}%"
        p.setPen(vcol)
        p.setFont(self._f(9, bold=True))
        fw = QFontMetrics(p.font()).horizontalAdvance(pct_str)
        p.drawText(W - PAD - fw, y + 9, pct_str)
        y += 13

        bw = W - PAD * 2
        p.fillRect(PAD, y, bw, 5, hex_to_qcolor(t['BORDER']))
        p.fillRect(PAD, y, max(2, int(bw * s['danger'] / 100)), 5, vcol)
        y += 9

        p.setPen(vcol)
        p.setFont(self._f(10, bold=True))
        vw = QFontMetrics(p.font()).horizontalAdvance(s['verdict'])
        p.drawText(W - PAD - vw, y + 11, s['verdict'])
        y += 18

        return y

    def _draw_combat(self, p, t, s, y) -> int:
        # Dessine le résumé combat : kills, pertes, ISK, efficacité, solo.
        PAD = 10
        y  += 5

        p.setPen(QColor(t['CYAN']))
        p.setFont(self._f(8))
        p.drawText(PAD, y + 9, "COMBAT SUMMARY")
        y += 14

        rows = [
            ("Ships destroyed", str(s['ships_d']),    t['GREEN']),
            ("Ships lost",      str(s['ships_l']),    t['RED']),
            ("ISK destroyed",   s['isk_d'],            t['GREEN']),
            ("ISK lost",        s['isk_l'],            t['RED']),
            ("Efficiency",      f"{s['isk_eff']}%",   t['CYAN']),
            ("Solo kills",      str(s['solo_kills']), t['YELLOW']),
        ]
        for key, val, col in rows:
            p.setPen(QColor(t['WHITE']))
            p.setFont(self._f(10))
            p.drawText(PAD, y + 11, key)
            p.setPen(QColor(col))
            p.setFont(self._f(10, bold=True))
            vw = QFontMetrics(p.font()).horizontalAdvance(val)
            p.drawText(W - PAD - vw, y + 11, val)
            y += 16

        return y + 4

    def _draw_playstyle(self, p, t, s, y) -> int:
        # Dessine les barres de playstyle : solo / small gang / fleet.
        PAD     = 10
        LABEL_W = 62
        VAL_W   = 32
        bar_x   = PAD + LABEL_W + 4
        bar_w   = W - bar_x - PAD - VAL_W
        y      += 5

        p.setPen(QColor(t['CYAN']))
        p.setFont(self._f(8))
        p.drawText(PAD, y + 9, "PLAYSTYLE")
        y += 14

        # Deux barres RÉELLES : solo (ratio zKill) et son complément. L'ancien
        # split gang/fleet était fabriqué (60/40 du non-solo) — trompeur.
        bars = [
            ("Solo",  s['solo_pct'],  t['RED']),
            ("Group", s['group_pct'], t['CYAN']),
        ]
        for label, pct, col in bars:
            p.setPen(QColor(t['WHITE']))
            p.setFont(self._f(9))
            p.drawText(PAD, y + 10, label)

            p.fillRect(bar_x, y + 5, bar_w, 4, hex_to_qcolor(t['BORDER']))
            fill = max(0, int(bar_w * pct / 100))
            if fill > 0:
                p.fillRect(bar_x, y + 5, fill, 4, QColor(col))

            val_str = f"{pct}%"
            p.setPen(QColor(col))
            p.setFont(self._f(9, bold=True))
            vw = QFontMetrics(p.font()).horizontalAdvance(val_str)
            p.drawText(W - PAD - vw, y + 10, val_str)
            y += 18

        return y + 3

    def _draw_tags(self, p, t, s, y) -> int:
        # Dessine les tags colorés du pilote (DANGEROUS, SOLO HUNTER, etc.).
        PAD = 10
        y  += 6

        p.setPen(QColor(t['CYAN']))
        p.setFont(self._f(8))
        p.drawText(PAD, y + 9, "TAGS")
        y += 13

        TAG_COLORS = {
            "DANGEROUS":    t['RED'],    "MODERATE":    t['YELLOW'],
            "SNUGGLY":      t['GREEN'],  "SOLO HUNTER": t['RED'],
            "GROUP PILOT":  t['CYAN'],
            "HIGH EFF.":    t['GREEN'],  "SOLO KILL":   t['YELLOW'],
        }
        p.setFont(self._f(8, bold=True))
        placements, height = self._tag_layout(s.get('tags', []))
        for tag, rect in placements:
            col = QColor(TAG_COLORS.get(tag, t['DIM']))
            draw_rect = QRect(rect)
            draw_rect.translate(0, y - 19)
            p.setPen(QPen(col, 1))
            p.drawRect(draw_rect)
            p.setPen(col)
            p.drawText(draw_rect, Qt.AlignmentFlag.AlignCenter, tag)

        return y - 19 + height

    def _draw_sep(self, p, t, y) -> int:
        # Trace un séparateur horizontal d'1px.
        p.fillRect(0, y, W, 1, hex_to_qcolor(t['BORDER']))
        return y + 1

    def _draw_footer(self, p, t, s, y) -> int:
        # Barre de pied compacte :
        # Gauche — lien 'zkillboard' en cyan, cliquable (ouvre le navigateur)
        # Droite — bouton '[MORE ▼]' / '[LESS ▲]' pour déplier l'intel étendu
        H   = _H_FOOTER
        PAD = 10
        p.fillRect(0, y, W, H, hex_to_qcolor(t['BG_PANEL']))

        # Lien zkillboard cliquable
        p.setFont(self._f(8))
        p.setPen(QColor(t['CYAN']))
        link = "zkillboard"
        lw   = QFontMetrics(p.font()).horizontalAdvance(link)
        p.drawText(PAD, y + 12, link)
        self._zkill_rect = QRect(PAD, y + 1, lw, H - 2)

        # Bouton MORE / LESS
        btn  = "[LESS ▲]" if self._expanded else "[MORE ▼]"
        p.setFont(self._f(8, bold=True))
        p.setPen(QColor(t['DIM']))
        bw   = QFontMetrics(p.font()).horizontalAdvance(btn)
        bx   = W - PAD - bw
        p.drawText(bx, y + 12, btn)
        self._more_rect = QRect(bx - 2, y + 1, bw + 4, H - 2)

        return y + H

    def _draw_more(self, p, t, s, y) -> int:
        # Dessine l'intel étendu : kills du mois, top ships, zone active.
        PAD = 10
        y  += 5

        # Label de section
        p.setPen(QColor(t['CYAN']))
        p.setFont(self._f(8))
        p.drawText(PAD, y + 9, "EXTENDED INTEL")
        y += 14

        # Kills du mois courant
        k30 = str(s.get("kills_30d", 0))
        p.setPen(QColor(t['WHITE']))
        p.setFont(self._f(9))
        p.drawText(PAD, y + 11, "Kills (this month)")
        p.setPen(QColor(t['YELLOW']))
        p.setFont(self._f(9, bold=True))
        vw = QFontMetrics(p.font()).horizontalAdvance(k30)
        p.drawText(W - PAD - vw, y + 11, k30)
        y += 16

        # Top ships utilisés
        top_ships = s.get("top_ships", [])
        if top_ships:
            p.setPen(QColor(t['CYAN']))
            p.setFont(self._f(8))
            p.drawText(PAD, y + 9, "TOP SHIPS")
            y += 12
            for ship_name, count in top_ships:
                p.setFont(self._f(9))
                fm      = QFontMetrics(p.font())
                max_w   = W - PAD * 2 - 32
                elided  = fm.elidedText(ship_name, Qt.TextElideMode.ElideRight, max_w)
                p.setPen(QColor(t['WHITE']))
                p.drawText(PAD, y + 10, elided)
                cnt_str = f"×{count}"
                p.setFont(self._f(8))
                p.setPen(QColor(t['DIM']))
                cw = QFontMetrics(p.font()).horizontalAdvance(cnt_str)
                p.drawText(W - PAD - cw, y + 10, cnt_str)
                y += 14

        # Système solaire le plus actif
        zone = s.get("active_zone", "")
        if zone:
            p.setPen(QColor(t['DIM']))
            p.setFont(self._f(9))
            p.drawText(PAD, y + 10, f"Active: {zone}")
            y += 14

        return y + 3

    # ── Font helper ───────────────────────────────────────────────────────────

    _font_cache: dict[tuple, QFont] = {}

    @classmethod
    def _f(cls, size: int, bold: bool = False) -> QFont:
        # Retourne une QFont monospace à la taille et graisse demandées (mise en cache).
        key = (size, bold)
        if key not in cls._font_cache:
            f = QFont(mono_family(), size)
            f.setBold(bold)
            cls._font_cache[key] = f
        return cls._font_cache[key]

    # ── Mouse ─────────────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        # Gère clic gauche : fermer, ouvrir zkill, toggle MORE, ou démarrer le drag.
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()

        # Fermer
        if self._close_rect.contains(pos):
            self.close()
            return

        # Lien zkillboard → navigateur
        if self._zkill_rect.contains(pos) and self._stats:
            webbrowser.open_new_tab(
                f"https://zkillboard.com/character/{self._stats['char_id']}/"
            )
            return

        # Toggle MORE / LESS
        if self._more_rect.contains(pos) and self._stats:
            self._expanded = not self._expanded
            new_h = (
                self._base_height() + _H_SEP + self._more_section_height()
                if self._expanded
                else self._base_height()
            )
            self.setFixedHeight(new_h)
            self._ensure_on_screen()
            self.update()
            return

        # Sinon : début de drag
        self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:
        # Déplace la fenêtre si un drag est en cours.
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_pos:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event) -> None:
        # Termine le drag.
        self._drag_pos = None
        self._ensure_on_screen()

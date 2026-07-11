# -*- coding: utf-8 -*-
# Moteur de thèmes — registre THEMES et utilitaires couleur.
# Nécessite PyQt6 (QColor utilisé par hex_to_qcolor).
from PyQt6.QtGui import QColor


# ── Helpers privés ───────────────────────────────────────────────────────────

def _lighten(hx: str, amt: int) -> str:
    # Éclaircit une couleur hex d'un montant fixe par canal.
    h = hx.lstrip('#')
    r = min(255, int(h[0:2], 16) + amt)
    g = min(255, int(h[2:4], 16) + amt)
    b = min(255, int(h[4:6], 16) + amt)
    return f"#{r:02x}{g:02x}{b:02x}"


def _gen_theme(base: str, accent: str) -> dict:
    # Génère un dict de thème complet à partir d'une couleur de fond et d'une couleur d'accent.
    return {
        "BG":       base,
        "BG_PANEL": _lighten(base, 10),
        "BORDER":   _lighten(base, 30),
        "CYAN":     accent,
        "RED":      "#cc3325",
        "GREEN":    "#2ecc40",
        "YELLOW":   "#f39c12",
        "DIM":      "#5a7085",
        "WHITE":    "#ffffff",
    }


# ── API publique ─────────────────────────────────────────────────────────────

def hex_to_qcolor(hx: str, alpha: int = 255) -> QColor:
    # Convertit une couleur hex en QColor avec alpha optionnel.
    hx = hx.lstrip('#')
    return QColor(int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16), alpha)


THEMES: dict[str, dict] = {
    "EVE Online (Default)": {
        "BG": "#0b0e17", "BG_PANEL": "#111827", "BORDER": "#1e3a4a",
        "CYAN": "#3dd8e0", "RED": "#cc3325", "GREEN": "#2ecc40",
        "YELLOW": "#f39c12", "DIM": "#5a7085", "WHITE": "#ffffff",
    },
    "Caldari":                       _gen_theme("#191919", "#3C5F73"),
    "Caldari II":                    _gen_theme("#0F1114", "#8A8F9A"),
    "Minmatar":                      _gen_theme("#161414", "#5A3737"),
    "Minmatar II":                   _gen_theme("#140D0F", "#8C5055"),
    "Amarr":                         _gen_theme("#191714", "#BBA183"),
    "Amarr II":                      _gen_theme("#12110A", "#9A6928"),
    "Gallente":                      _gen_theme("#0F1414", "#576866"),
    "Gallente II":                   _gen_theme("#0A0F0F", "#9EAE95"),
    "Guristas Pirates":              _gen_theme("#261500", "#FF9100"),
    "Blood Raiders":                 _gen_theme("#260505", "#BE0000"),
    "Angel Cartel":                  _gen_theme("#26110E", "#FF4D00"),
    "Serpentis":                     _gen_theme("#060A0C", "#BBC400"),
    "Sansha's Nation":               _gen_theme("#0a0a0a", "#218000"),
    "Triglavian Collective":         _gen_theme("#262218", "#DE1400"),
    "Sisters of EVE":                _gen_theme("#262626", "#B60000"),
    "EDENCOM":                       _gen_theme("#001926", "#039DFF"),
    "Intaki Syndicate":              _gen_theme("#060A0C", "#393780"),
    "ORE":                           _gen_theme("#1A1A1A", "#D9A600"),
    "Mordu's Legion":                _gen_theme("#1A1F22", "#4B6B78"),
    "Thukker Tribe":                 _gen_theme("#1F1A17", "#B35900"),
    "CONCORD":                       _gen_theme("#0A1428", "#0088FF"),
    "Society of Conscious Thought":  _gen_theme("#0A111A", "#00E8FF"),
}

DEFAULT_THEME_NAME = "EVE Online (Default)"
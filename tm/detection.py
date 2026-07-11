# -*- coding: utf-8 -*-
# Détection de menaces — analyse pixel NumPy, sans dépendance Qt.
#
# Scanne une région capturée pour trouver les icônes de standing EVE :
#   • Menaces : pixels rouges + pixels gris neutres
#   • Alliés  : pixels verts / bleus / violets
#
# Retourne des tuples (threat_count, ally_count).
from collections.abc import Mapping

import numpy as np

# Position de la colonne d'icônes de standing dans une ligne de liste EVE.
# Défini une seule fois ici : monitor.py s'en sert pour ne capturer que cette
# bande (via strip_bbox), et detect_threats pour la découper — les deux ne
# peuvent donc pas diverger.
ICON_STRIP_LEFT  = 4    # décalage x du début de la colonne, en px
ICON_STRIP_WIDTH = 12   # largeur de la colonne, en px


def strip_bbox(bbox: dict) -> dict:
    # Réduit une bbox de détection à la seule colonne d'icônes de standing.
    #
    # Évite de capturer (BitBlt) et transférer toute la largeur de la liste alors
    # que seuls ~12 px sont analysés. Clampe pour ne jamais déborder de la
    # sélection : pour une bbox large le résultat est exactement 12 px à x+4,
    # reproduisant à l'identique l'ancien découpage `[:, 4:16]`.
    if not isinstance(bbox, Mapping):
        raise ValueError("bbox must be a mapping")

    fields = ("left", "top", "width", "height")
    if any(
        isinstance(bbox.get(field), bool) or not isinstance(bbox.get(field), int)
        for field in fields
    ):
        raise ValueError("bbox fields must be integers")
    if bbox["width"] <= ICON_STRIP_LEFT or bbox["height"] <= 0:
        raise ValueError("bbox must contain a positive icon strip and height")

    width = min(ICON_STRIP_WIDTH, bbox["width"] - ICON_STRIP_LEFT)
    result = {
        "left":   bbox["left"] + ICON_STRIP_LEFT,
        "top":    bbox["top"],
        "width":  width,
        "height": bbox["height"],
    }
    if result["left"] + result["width"] > bbox["left"] + bbox["width"]:
        raise ValueError("icon strip extends beyond bbox")
    return result


def detect_threats(
    sct_img,
    x_start: int = ICON_STRIP_LEFT,
    x_width: int = ICON_STRIP_WIDTH,
) -> tuple[int, int]:
    # Analyse vectorisée d'un screenshot mss. Retourne (threat_count, ally_count).
    #
    # Inspecte une colonne de 12px par ligne pour détecter les icônes de standing.
    # Les clusters de ≥3 lignes consécutives (gap ≤2) comptent comme une entité.
    #
    # Par défaut la colonne est lue à x=4..16 (image pleine largeur, ex. tests).
    # Quand l'appelant a déjà restreint la capture à la bande (voir strip_bbox),
    # il passe x_start=0 pour lire toute la largeur reçue.
    #
    # Lève en cas d'entrée invalide — SURTOUT ne pas retourner (0, 0) : un zéro
    # silencieux serait affiché ALL CLEAR et acquitterait les alertes en cours.
    # Le compteur 3-erreurs de monitor._update_monitor transforme l'exception en
    # état MON FAIL visible (son + toast).
    #
    # Vue zéro-copie uint8 sur le buffer mss ; on ne convertit en int16
    # (nécessaire pour éviter l'overflow des comparaisons b*2, max+10) que
    # la ROI de 12px, pas toute la frame.
    arr = np.asarray(sct_img)
    # mss livre toujours du uint8 : un autre dtype (float normalisé, bool…)
    # raterait tous les seuils et produirait un faux ALL CLEAR — on lève.
    if arr.dtype != np.uint8:
        raise ValueError("screenshot must contain uint8 pixel data")
    if arr.ndim != 3:
        raise ValueError("screenshot must have three dimensions")
    if arr.shape[0] <= 0 or arr.shape[1] <= 0:
        raise ValueError("screenshot dimensions must be positive")
    if arr.shape[2] < 3:
        raise ValueError("screenshot must contain at least three channels")
    if isinstance(x_start, bool) or not isinstance(x_start, int) or x_start < 0:
        raise ValueError("x_start must be a nonnegative integer")
    if isinstance(x_width, bool) or not isinstance(x_width, int) or x_width <= 0:
        raise ValueError("x_width must be a positive integer")
    if x_start >= arr.shape[1]:
        raise ValueError("requested ROI does not intersect the screenshot")

    roi = arr[:, x_start:x_start + x_width, :3].astype(np.int16)
    if roi.shape[1] <= 0:
        raise ValueError("requested ROI is empty")
    b, g, r = roi[:, :, 0], roi[:, :, 1], roi[:, :, 2]

    # Exclut les pixels trop sombres ou trop blancs (bruit de fond)
    dark_mask  = (r < 35)  & (g < 35)  & (b < 35)
    white_mask = (r > 240) & (g > 240) & (b > 240)
    valid_mask = ~(dark_mask | white_mask)

    # Alliés : icônes vertes / bleues / violettes
    green_mask  = (g > 80) & (g > r * 1.8) & (g > b * 1.8)
    blue_mask   = (b > 55) & (b > r * 2)   & (b > g * 1.5)
    purple_mask = (r > 50) & (b > 80)       & (b > g * 1.8) & (r > g)
    ally_pixels = valid_mask & (green_mask | blue_mask | purple_mask)

    # Menaces : icône rouge + icône neutre (gris)
    red_mask     = (r > 100) & (r > g * 1.5) & (r > b * 2)
    max_rgb      = np.maximum.reduce([r, g, b])
    min_rgb      = np.minimum.reduce([r, g, b])
    neutral_mask = (
        (r > 100) & (g > 100) & (b > 100) &
        (r < 210) & (g < 210) & (b < 210) &
        ((max_rgb - min_rgb) < 20) &
        (g <= np.maximum(r, b) + 10)
    )
    threat_pixels = valid_mask & (red_mask | neutral_mask)

    ally_count   = _count_clusters_1d(np.any(ally_pixels,   axis=1))
    threat_count = _count_clusters_1d(np.any(threat_pixels, axis=1))

    return threat_count, ally_count


def _count_clusters_1d(
    boolean_1d: np.ndarray,
    max_gap: int = 2,
    min_size: int = 3,
) -> int:
    # Compte les clusters de True consécutifs dans un tableau 1D booléen.
    #
    # Un cluster se termine si le gap dépasse `max_gap`. Clusters < `min_size` ignorés (bruit).
    true_indices = np.where(boolean_1d)[0]
    if len(true_indices) == 0:
        return 0

    gaps          = np.diff(true_indices) > (max_gap + 1)
    split_indices = np.where(gaps)[0] + 1
    clusters      = np.split(true_indices, split_indices)

    return sum(1 for c in clusters if len(c) >= min_size)

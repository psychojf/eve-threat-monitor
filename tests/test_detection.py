# -*- coding: utf-8 -*-
# Tests de tm.detection — logique pixel pure, sans Qt.
import numpy as np
import pytest

from tm.detection import (
    detect_threats, strip_bbox, _count_clusters_1d,
    ICON_STRIP_LEFT, ICON_STRIP_WIDTH,
)

# Couleurs en ordre BGR (mss livre du BGRA)
RED     = (37, 37, 220)     # icône hostile
GREEN   = (40, 204, 46)     # icône alliée
BLUE    = (200, 30, 30)     # icône alliée (bleu)
NEUTRAL = (150, 150, 150)   # icône neutre (gris) — compte comme menace


def make_img(h: int = 60, w: int = 30) -> np.ndarray:
    # Image BGRA noire (fond « sombre » exclu par le masque de validité).
    img = np.zeros((h, w, 4), dtype=np.uint8)
    img[:, :, 3] = 255
    return img


def paint(img: np.ndarray, row_start: int, row_end: int, bgr: tuple) -> None:
    # Peint la colonne d'icônes (x=4..16) entre deux lignes.
    img[row_start:row_end, ICON_STRIP_LEFT:ICON_STRIP_LEFT + ICON_STRIP_WIDTH, 0] = bgr[0]
    img[row_start:row_end, ICON_STRIP_LEFT:ICON_STRIP_LEFT + ICON_STRIP_WIDTH, 1] = bgr[1]
    img[row_start:row_end, ICON_STRIP_LEFT:ICON_STRIP_LEFT + ICON_STRIP_WIDTH, 2] = bgr[2]


# ── detect_threats ────────────────────────────────────────────────────────────

def test_empty_image_is_clear():
    assert detect_threats(make_img()) == (0, 0)

def test_red_icon_counts_as_threat():
    img = make_img()
    paint(img, 10, 18, RED)
    assert detect_threats(img) == (1, 0)

def test_neutral_gray_icon_counts_as_threat():
    img = make_img()
    paint(img, 10, 18, NEUTRAL)
    assert detect_threats(img) == (1, 0)

def test_green_icon_counts_as_ally():
    img = make_img()
    paint(img, 10, 18, GREEN)
    assert detect_threats(img) == (0, 1)

def test_blue_icon_counts_as_ally():
    img = make_img()
    paint(img, 10, 18, BLUE)
    assert detect_threats(img) == (0, 1)

def test_two_separated_red_icons_count_as_two_threats():
    img = make_img()
    paint(img, 5, 12, RED)
    paint(img, 25, 32, RED)   # gap > 2 lignes → deuxième cluster
    assert detect_threats(img) == (2, 0)

def test_mixed_threat_and_ally():
    img = make_img()
    paint(img, 5, 12, RED)
    paint(img, 25, 32, GREEN)
    assert detect_threats(img) == (1, 1)

def test_tiny_blob_ignored_as_noise():
    img = make_img()
    paint(img, 10, 12, RED)   # 2 lignes < min_size=3
    assert detect_threats(img) == (0, 0)

def test_x_start_zero_reads_precropped_strip():
    # Mode production : la capture est déjà réduite à la bande de 12 px.
    img = make_img(w=ICON_STRIP_WIDTH)
    img[10:18, :, 2] = 220    # rouge sur toute la largeur de la bande
    img[10:18, :, 0] = 37
    img[10:18, :, 1] = 37
    assert detect_threats(img, x_start=0) == (1, 0)

@pytest.mark.parametrize("img,x_start,x_width", [
    (np.zeros((10, 0, 4), dtype=np.uint8), 0, 12),
    (np.zeros((0, 12, 4), dtype=np.uint8), 0, 12),
    (np.zeros((10, 12, 2), dtype=np.uint8), 0, 12),
    (np.zeros((10, 4, 4), dtype=np.uint8), 4, 12),
    (np.zeros((10, 12, 4), dtype=np.uint8), 0, 0),
])
def test_detect_threats_rejects_empty_or_nonintersecting_roi(img, x_start, x_width):
    with pytest.raises(ValueError):
        detect_threats(img, x_start=x_start, x_width=x_width)

@pytest.mark.parametrize("dtype", [np.float64, np.float32, np.int16, np.uint16, bool])
def test_detect_threats_rejects_non_uint8_frames(dtype):
    # Un buffer non-uint8 (float normalisé, bool…) rate tous les seuils et
    # retournerait (0, 0) — faux ALL CLEAR silencieux. mss livre toujours du
    # uint8 : tout autre dtype signale une entrée corrompue et doit lever.
    img = make_img()
    paint(img, 10, 18, RED)
    with pytest.raises(ValueError):
        detect_threats(img.astype(dtype))

@pytest.mark.parametrize("img,x_start,x_width", [
    (None, 0, 12),
    (np.zeros((10, 12), dtype=np.uint8), 0, 12),
    (np.zeros((10, 12, 4), dtype=np.uint8), -1, 12),
    (np.zeros((10, 12, 4), dtype=np.uint8), 0.5, 12),
    (np.zeros((10, 12, 4), dtype=np.uint8), False, 12),
    (np.zeros((10, 12, 4), dtype=np.uint8), 0, True),
    (np.zeros((10, 12, 4), dtype=np.uint8), 0, -1),
])
def test_detect_threats_rejects_malformed_dimensions_or_coordinates(
    img, x_start, x_width,
):
    # Une entrée invalide doit LEVER, pas retourner (0, 0) : un (0, 0) silencieux
    # serait affiché comme ALL CLEAR et acquitterait les alertes en cours.
    # Le gestionnaire 3-erreurs de monitor.py transforme l'exception en état
    # MON FAIL visible + son.
    with pytest.raises(ValueError):
        detect_threats(img, x_start=x_start, x_width=x_width)


# ── _count_clusters_1d ────────────────────────────────────────────────────────

def _mask(length: int, true_at: list) -> np.ndarray:
    m = np.zeros(length, dtype=bool)
    m[true_at] = True
    return m

def test_clusters_empty():
    assert _count_clusters_1d(_mask(20, [])) == 0

def test_clusters_single():
    assert _count_clusters_1d(_mask(20, [3, 4, 5, 6])) == 1

def test_clusters_below_min_size_ignored():
    assert _count_clusters_1d(_mask(20, [3, 4])) == 0

def test_clusters_gap_within_tolerance_bridges():
    # gap de 2 (indices 5,8) ≤ max_gap → un seul cluster
    assert _count_clusters_1d(_mask(20, [3, 4, 5, 8, 9, 10])) == 1

def test_clusters_gap_beyond_tolerance_splits():
    assert _count_clusters_1d(_mask(30, [3, 4, 5, 15, 16, 17])) == 2


# ── strip_bbox ────────────────────────────────────────────────────────────────

def test_strip_bbox_wide_selection():
    bbox = {"left": 100, "top": 50, "width": 200, "height": 300}
    assert strip_bbox(bbox) == {"left": 104, "top": 50, "width": 12, "height": 300}

def test_strip_bbox_narrow_selection_clamps():
    bbox = {"left": 100, "top": 50, "width": 10, "height": 300}
    s = strip_bbox(bbox)
    assert s["left"] == 104
    assert s["width"] == 6            # 10 − 4 : jamais au-delà de la sélection
    assert s["width"] >= 1
    assert s["left"] + s["width"] <= bbox["left"] + bbox["width"]

def test_strip_bbox_rejects_too_narrow_or_nonpositive_bbox():
    with pytest.raises(ValueError):
        strip_bbox({"left": 100, "top": 50, "width": 4, "height": 300})
    with pytest.raises(ValueError):
        strip_bbox({"left": 100, "top": 50, "width": 10, "height": 0})

@pytest.mark.parametrize("bbox", [
    None,
    {"left": 100, "top": 50, "width": 10},
    {"left": 100, "top": 50, "width": True, "height": 300},
    {"left": 100.0, "top": 50, "width": 10, "height": 300},
])
def test_strip_bbox_rejects_malformed_mapping_or_noninteger_fields(bbox):
    with pytest.raises(ValueError):
        strip_bbox(bbox)

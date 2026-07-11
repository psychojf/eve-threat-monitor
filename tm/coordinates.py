# -*- coding: utf-8 -*-
# coordinates.py — Conversions entre coordonnées logiques Qt et pixels natifs MSS.
#
# Pourquoi ce module existe : Qt travaille en unités logiques (affectées par
# l'échelle DPI de Windows, ex. 150 %) alors que MSS capture en pixels
# physiques. Mélanger les deux espaces faisait capturer la mauvaise zone sur
# un écran mis à l'échelle — donc de faux « ALL CLEAR » silencieux. Ce module
# est LA frontière unique où l'on convertit, dans les deux sens, et il refuse
# de deviner : appariement ambigu → exception explicite, jamais d'à-peu-près.
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import permutations
import math
from types import MappingProxyType

import mss
from PyQt6.QtCore import QRect
from PyQt6.QtGui import QGuiApplication


_NATIVE_KEYS = ("left", "top", "width", "height")


def _native_int_values(geometry) -> dict:
    # Extraction stricte des quatre champs natifs : int véritables exigés
    # (bool exclu, float/str refusés). Un int() coercitif accepterait "0" ou
    # True et tronquerait 800.5 — géométrie décalée d'un pixel, donc capture
    # de la mauvaise zone sans symptôme visible. Refuser > deviner.
    if not isinstance(geometry, Mapping):
        raise ValueError("Native geometry must be a mapping")
    values = {}
    for key in _NATIVE_KEYS:
        value = geometry.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Native geometry fields must be integers")
        values[key] = value
    return values
_MAX_ASPECT_LOG_ERROR = 0.05
_TOPOLOGY_EPSILON = 0.05
_MIN_ASSIGNMENT_SCORE_MARGIN = 0.10


@dataclass(frozen=True, init=False)
class ScreenMapping:
    # Paire figée « écran logique Qt ↔ moniteur natif MSS ».
    # Immuable (frozen + copies défensives) car partagée entre widgets : une
    # mutation accidentelle fausserait silencieusement toutes les conversions.

    _logical_values: tuple[int, int, int, int]
    _native_items: tuple[tuple[str, int], ...]

    def __init__(self, logical: QRect, native: Mapping) -> None:
        # Copie puis fige les deux géométries ; dimensions > 0 exigées des
        # deux côtés — une géométrie nulle rendrait les échelles infinies.
        logical = QRect(logical)
        native_values = _native_int_values(native)
        if logical.width() <= 0 or logical.height() <= 0:
            raise ValueError("Qt screen geometry must have positive dimensions")
        if native_values["width"] <= 0 or native_values["height"] <= 0:
            raise ValueError("MSS monitor geometry must have positive dimensions")
        object.__setattr__(
            self,
            "_logical_values",
            (logical.x(), logical.y(), logical.width(), logical.height()),
        )
        object.__setattr__(
            self,
            "_native_items",
            tuple((key, native_values[key]) for key in _NATIVE_KEYS),
        )

    @property
    def logical(self) -> QRect:
        # Copie défensive du rectangle logique — QRect est mutable, on ne
        # donne jamais l'original à un appelant.
        return QRect(*self._logical_values)

    @property
    def native(self) -> Mapping[str, int]:
        # Géométrie native exposée en mapping immuable : impossible à corrompre.
        return MappingProxyType(dict(self._native_items))

    @property
    def scale_x(self) -> float:
        # Facteur d'échelle horizontal natif/logique (ex. 1.5 à 150 % DPI).
        return dict(self._native_items)["width"] / self._logical_values[2]

    @property
    def scale_y(self) -> float:
        # Facteur vertical, séparé de scale_x : Windows autorise des échelles
        # par axe légèrement différentes après certains arrondis.
        return dict(self._native_items)["height"] / self._logical_values[3]


def _qt_geometry(screen) -> QRect:
    # Accepte un QScreen ou directement un QRect (tests synthétiques) et
    # retourne toujours une copie — jamais l'objet d'origine.
    if isinstance(screen, QRect):
        return QRect(screen)
    return QRect(screen.geometry())


def _normalised_center(rect, virtual_rect) -> tuple[float, float]:
    # Centre d'un rect exprimé en 0..1 dans le bureau virtuel : permet de
    # comparer des positions logiques et natives malgré des unités différentes.
    return (
        (rect[0] + rect[2] / 2.0 - virtual_rect[0]) / virtual_rect[2],
        (rect[1] + rect[3] / 2.0 - virtual_rect[1]) / virtual_rect[3],
    )


def _virtual_tuple(rects) -> tuple[int, int, int, int]:
    # Rectangle englobant (le « bureau virtuel ») d'une liste de (x, y, w, h).
    left = min(rect[0] for rect in rects)
    top = min(rect[1] for rect in rects)
    right = max(rect[0] + rect[2] for rect in rects)
    bottom = max(rect[1] + rect[3] for rect in rects)
    return left, top, right - left, bottom - top


def _pair_score(logical, native, logical_virtual, native_virtual) -> float:
    # Score d'un couple logique/natif : distance des centres normalisés +
    # écart d'aspect. Plus petit = meilleur candidat d'appariement.
    logical_tuple = (
        logical.x(), logical.y(), logical.width(), logical.height()
    )
    native_tuple = (
        native["left"], native["top"], native["width"], native["height"]
    )
    logical_center = _normalised_center(logical_tuple, logical_virtual)
    native_center = _normalised_center(native_tuple, native_virtual)
    position_score = (
        abs(logical_center[0] - native_center[0])
        + abs(logical_center[1] - native_center[1])
    )
    scale_x = native["width"] / logical.width()
    scale_y = native["height"] / logical.height()
    size_ratio_score = abs(math.log(scale_x / scale_y))
    return position_score + size_ratio_score


def _pair_is_compatible(logical: QRect, native: dict) -> bool:
    # Exige le même rapport d'aspect des deux côtés : un écran 16:9 ne peut
    # pas être le pendant natif d'un 16:10 — élimine vite les faux couples.
    scale_x = native["width"] / logical.width()
    scale_y = native["height"] / logical.height()
    return abs(math.log(scale_x / scale_y)) <= _MAX_ASPECT_LOG_ERROR


def _dominant_axis(dx: float, dy: float):
    # Axe dominant d'un déplacement relatif : "horizontal", "vertical", ou
    # None si diagonale/trop faible pour trancher (évite les faux positifs).
    abs_x = abs(dx)
    abs_y = abs(dy)
    if abs_x >= _TOPOLOGY_EPSILON and abs_x >= abs_y * 2.0:
        return "horizontal"
    if abs_y >= _TOPOLOGY_EPSILON and abs_y >= abs_x * 2.0:
        return "vertical"
    return None


def _assignment_preserves_topology(
    logical_rects,
    native_rects,
    assignment,
    logical_virtual,
    native_virtual,
) -> bool:
    # Rejette les affectations qui tournent ou inversent la disposition
    # physique : si l'écran A est à gauche de B côté Qt, son pendant natif
    # doit l'être aussi. Empêche d'apparier en croisé deux écrans identiques.
    logical_centers = [
        _normalised_center(
            (rect.x(), rect.y(), rect.width(), rect.height()), logical_virtual
        )
        for rect in logical_rects
    ]
    native_centers = [
        _normalised_center(
            (
                native_rects[index]["left"],
                native_rects[index]["top"],
                native_rects[index]["width"],
                native_rects[index]["height"],
            ),
            native_virtual,
        )
        for index in assignment
    ]

    for first in range(len(logical_centers)):
        for second in range(first + 1, len(logical_centers)):
            logical_dx = logical_centers[first][0] - logical_centers[second][0]
            logical_dy = logical_centers[first][1] - logical_centers[second][1]
            native_dx = native_centers[first][0] - native_centers[second][0]
            native_dy = native_centers[first][1] - native_centers[second][1]

            logical_axis = _dominant_axis(logical_dx, logical_dy)
            native_axis = _dominant_axis(native_dx, native_dy)
            if (
                logical_axis is not None
                and native_axis is not None
                and logical_axis != native_axis
            ):
                return False
            for logical_delta, native_delta in (
                (logical_dx, native_dx),
                (logical_dy, native_dy),
            ):
                if (
                    abs(logical_delta) >= _TOPOLOGY_EPSILON
                    and abs(native_delta) >= _TOPOLOGY_EPSILON
                    and logical_delta * native_delta < 0
                ):
                    return False
    return True


def build_screen_mappings(qt_screens=None, native_monitors=None) -> list[ScreenMapping]:
    # Apparie chaque écran Qt à son moniteur MSS ; refuse toute ambiguïté.
    #
    # Pourquoi lever plutôt que deviner : un mauvais appariement capturerait
    # l'écran voisin et produirait de faux « ALL CLEAR » invisibles. Le score
    # minimal ne gagne que s'il domine nettement le deuxième candidat.
    # Les paramètres sont injectables pour tester sans écran réel.
    if qt_screens is None:
        qt_screens = QGuiApplication.screens()
    logical_rects = [_qt_geometry(screen) for screen in list(qt_screens)]

    if native_monitors is None:
        with mss.MSS() as capture:
            native_monitors = list(capture.monitors[1:])
    native_rects = [
        _native_int_values(monitor) for monitor in list(native_monitors)
    ]

    if len(logical_rects) != len(native_rects):
        raise RuntimeError(
            "Qt and MSS must report the same number of physical screens "
            f"(Qt={len(logical_rects)}, MSS={len(native_rects)})"
        )
    if not logical_rects:
        raise RuntimeError("Qt and MSS reported no physical screens")
    if len(logical_rects) == 1:
        return [ScreenMapping(logical_rects[0], native_rects[0])]

    logical_tuples = [
        (rect.x(), rect.y(), rect.width(), rect.height()) for rect in logical_rects
    ]
    native_tuples = [
        (rect["left"], rect["top"], rect["width"], rect["height"])
        for rect in native_rects
    ]
    logical_virtual = _virtual_tuple(logical_tuples)
    native_virtual = _virtual_tuple(native_tuples)

    candidates = []
    for assignment in permutations(range(len(native_rects))):
        if not all(
            _pair_is_compatible(logical_rects[index], native_rects[native_index])
            for index, native_index in enumerate(assignment)
        ):
            continue
        if not _assignment_preserves_topology(
            logical_rects,
            native_rects,
            assignment,
            logical_virtual,
            native_virtual,
        ):
            continue
        score = sum(
            _pair_score(
                logical_rects[index],
                native_rects[native_index],
                logical_virtual,
                native_virtual,
            )
            for index, native_index in enumerate(assignment)
        )
        candidates.append((score, assignment))
    candidates.sort(key=lambda candidate: (candidate[0], candidate[1]))

    if not candidates:
        raise RuntimeError(
            "Qt and MSS screen geometries have no compatible topology mapping"
        )
    if (
        len(candidates) > 1
        and candidates[1][0] - candidates[0][0]
        < max(_MIN_ASSIGNMENT_SCORE_MARGIN, abs(candidates[0][0]) * 0.25)
    ):
        raise RuntimeError(
            "Qt and MSS screen geometries do not provide an unambiguous mapping"
        )

    mappings = [
        ScreenMapping(logical_rects[index], native_rects[native_index])
        for index, native_index in enumerate(candidates[0][1])
    ]
    return sorted(
        mappings,
        key=lambda mapping: (
            mapping.logical.x(),
            mapping.logical.y(),
            mapping.logical.width(),
            mapping.logical.height(),
        ),
    )


def _rect_edges(rect: QRect) -> tuple[int, int, int, int]:
    # Bords (gauche, haut, droite, bas) d'un QRect ; dimensions > 0 exigées —
    # un rect vide convertirait en bbox vide, inutilisable pour la capture.
    if rect.width() <= 0 or rect.height() <= 0:
        raise ValueError("Coordinate rectangles must have positive dimensions")
    return rect.x(), rect.y(), rect.x() + rect.width(), rect.y() + rect.height()


def _bbox_edges(bbox: dict) -> tuple[int, int, int, int]:
    # Même chose pour une bbox MSS {left, top, width, height}.
    values = _native_int_values(bbox)
    if values["width"] <= 0 or values["height"] <= 0:
        raise ValueError("Coordinate rectangles must have positive dimensions")
    return (
        values["left"],
        values["top"],
        values["left"] + values["width"],
        values["top"] + values["height"],
    )


def _contains_edges(container, edges) -> bool:
    # Contenance stricte de bords dans un conteneur (x, y, w, h) — bords
    # droite/bas exclusifs, cohérents avec la convention des rects Qt/MSS.
    left, top, right, bottom = edges
    return (
        left >= container[0]
        and top >= container[1]
        and right <= container[0] + container[2]
        and bottom <= container[1] + container[3]
    )


def qt_rect_to_native_bbox(rect: QRect, mappings=None) -> dict:
    # Convertit un rectangle logique Qt (contenu dans UN seul écran) en bbox
    # native MSS prête pour grab().
    #
    # On convertit les BORDS puis on en déduit largeur/hauteur : arrondir la
    # largeur séparément accumulerait l'erreur (±1 px par arrondi). Un rect à
    # cheval sur deux écrans est refusé — deux échelles DPI différentes
    # rendraient la conversion fausse d'un côté ou de l'autre.
    mappings = build_screen_mappings() if mappings is None else list(mappings)
    edges = _rect_edges(QRect(rect))
    matches = [
        mapping
        for mapping in mappings
        if _contains_edges(
            (
                mapping.logical.x(),
                mapping.logical.y(),
                mapping.logical.width(),
                mapping.logical.height(),
            ),
            edges,
        )
    ]
    if len(matches) != 1:
        raise ValueError("A coordinate rectangle must be fully contained in one screen")

    mapping = matches[0]
    left = mapping.native["left"] + round(
        (edges[0] - mapping.logical.x()) * mapping.scale_x
    )
    top = mapping.native["top"] + round(
        (edges[1] - mapping.logical.y()) * mapping.scale_y
    )
    right = mapping.native["left"] + round(
        (edges[2] - mapping.logical.x()) * mapping.scale_x
    )
    bottom = mapping.native["top"] + round(
        (edges[3] - mapping.logical.y()) * mapping.scale_y
    )
    return {
        "left": left,
        "top": top,
        "width": right - left,
        "height": bottom - top,
    }


def native_bbox_to_qt_rect(bbox: dict, mappings=None) -> QRect:
    # Conversion inverse : bbox native MSS (UN seul écran) → rectangle logique
    # Qt. Même méthode par les bords que qt_rect_to_native_bbox, pour qu'un
    # aller-retour ne dérive jamais de plus d'un pixel.
    mappings = build_screen_mappings() if mappings is None else list(mappings)
    edges = _bbox_edges(bbox)
    matches = [
        mapping
        for mapping in mappings
        if _contains_edges(
            (
                mapping.native["left"],
                mapping.native["top"],
                mapping.native["width"],
                mapping.native["height"],
            ),
            edges,
        )
    ]
    if len(matches) != 1:
        raise ValueError("A coordinate rectangle must be fully contained in one screen")

    mapping = matches[0]
    left = mapping.logical.x() + round(
        (edges[0] - mapping.native["left"]) / mapping.scale_x
    )
    top = mapping.logical.y() + round(
        (edges[1] - mapping.native["top"]) / mapping.scale_y
    )
    right = mapping.logical.x() + round(
        (edges[2] - mapping.native["left"]) / mapping.scale_x
    )
    bottom = mapping.logical.y() + round(
        (edges[3] - mapping.native["top"]) / mapping.scale_y
    )
    return QRect(left, top, right - left, bottom - top)


def virtual_qt_geometry() -> QRect:
    # Union de tous les écrans Qt en coordonnées logiques : géométrie du
    # sélecteur plein écran (F2/F3), qui doit couvrir tout le bureau pour
    # permettre une sélection sur n'importe quel moniteur.
    geometries = [_qt_geometry(screen) for screen in QGuiApplication.screens()]
    if not geometries:
        raise RuntimeError("Qt reported no screens")
    left = min(rect.x() for rect in geometries)
    top = min(rect.y() for rect in geometries)
    right = max(rect.x() + rect.width() for rect in geometries)
    bottom = max(rect.y() + rect.height() for rect in geometries)
    return QRect(left, top, right - left, bottom - top)

# -*- coding: utf-8 -*-
# Tests synthétiques de la frontière coordonnées logiques Qt ↔ natives MSS.
# Les géométries sont fabriquées : jamais l'écran réel, donc reproductible
# sur n'importe quelle machine, à n'importe quelle échelle DPI.
import importlib
from dataclasses import FrozenInstanceError

import pytest
from PyQt6.QtCore import QRect


def _coordinates():
    # Import paresseux : le run RED initial pouvait ainsi exercer les tests
    # widgets même quand le module coordinates n'existait pas encore.
    return importlib.import_module("tm.coordinates")


class _FakeScreen:
    def __init__(self, geometry: QRect) -> None:
        self._geometry = QRect(geometry)

    def geometry(self) -> QRect:
        return QRect(self._geometry)


def test_screen_mapping_is_immutable():
    coordinates = _coordinates()
    mapping = coordinates.ScreenMapping(
        QRect(0, 0, 800, 600),
        {"left": 0, "top": 0, "width": 800, "height": 600},
    )

    with pytest.raises(FrozenInstanceError):
        mapping.logical = QRect(1, 1, 800, 600)


def test_screen_mapping_returns_a_defensive_logical_rect_copy():
    coordinates = _coordinates()
    original = QRect(-100, 20, 800, 600)
    mapping = coordinates.ScreenMapping(
        original,
        {"left": -200, "top": 40, "width": 1600, "height": 1200},
    )

    exposed = mapping.logical
    exposed.moveTo(999, 999)

    assert mapping.logical == original


def test_screen_mapping_native_geometry_cannot_be_mutated():
    coordinates = _coordinates()
    native = {"left": 0, "top": 0, "width": 1600, "height": 1200}
    mapping = coordinates.ScreenMapping(QRect(0, 0, 800, 600), native)

    with pytest.raises(TypeError):
        mapping.native["left"] = 50

    assert mapping.native == native


@pytest.mark.parametrize("native", [
    None,
    {"left": 0, "top": 0, "width": 800},
    {"left": "0", "top": 0, "width": 800, "height": 600},
    {"left": 0, "top": True, "width": 800, "height": 600},
    {"left": 0, "top": 0, "width": 800.5, "height": 600},
])
def test_screen_mapping_rejects_non_integer_native_geometry(native):
    # La géométrie native doit être en int véritables : un int() coercitif
    # accepterait "0"/True et tronquerait 800.5 — géométrie décalée d'un pixel,
    # donc capture de la mauvaise zone sans aucun symptôme visible.
    coordinates = _coordinates()

    with pytest.raises(ValueError):
        coordinates.ScreenMapping(QRect(0, 0, 800, 600), native)


def test_build_screen_mappings_rejects_non_integer_monitor_fields():
    coordinates = _coordinates()

    with pytest.raises(ValueError):
        coordinates.build_screen_mappings(
            [_FakeScreen(QRect(0, 0, 800, 600))],
            [{"left": 0, "top": 0, "width": 800.5, "height": 600}],
        )


@pytest.mark.parametrize("bbox", [
    None,
    {"left": 10, "top": 10, "width": 20},
    {"left": "10", "top": 10, "width": 20, "height": 20},
    {"left": 10, "top": True, "width": 20, "height": 20},
    {"left": 10, "top": 10, "width": 20.5, "height": 20},
])
def test_native_bbox_to_qt_rect_rejects_non_integer_bbox(bbox):
    coordinates = _coordinates()
    mapping = coordinates.ScreenMapping(
        QRect(0, 0, 800, 600),
        {"left": 0, "top": 0, "width": 800, "height": 600},
    )

    with pytest.raises(ValueError):
        coordinates.native_bbox_to_qt_rect(bbox, [mapping])


def test_qt_rect_maps_to_native_pixels_at_150_percent():
    coordinates = _coordinates()
    mapping = coordinates.ScreenMapping(
        QRect(0, 0, 2293, 960),
        {"left": 0, "top": 0, "width": 3440, "height": 1440},
    )

    assert coordinates.qt_rect_to_native_bbox(
        QRect(1000, 100, 16, 500), [mapping]
    ) == {"left": 1500, "top": 150, "width": 24, "height": 750}


def test_mapping_handles_negative_screen_origins():
    coordinates = _coordinates()
    mapping = coordinates.ScreenMapping(
        QRect(-1280, 0, 1280, 720),
        {"left": -1920, "top": 0, "width": 1920, "height": 1080},
    )

    assert coordinates.qt_rect_to_native_bbox(
        QRect(-1000, 100, 100, 100), [mapping]
    ) == {"left": -1500, "top": 150, "width": 150, "height": 150}


def test_cross_screen_rect_is_rejected():
    coordinates = _coordinates()
    mappings = [
        coordinates.ScreenMapping(
            QRect(0, 0, 100, 100),
            {"left": 0, "top": 0, "width": 100, "height": 100},
        ),
        coordinates.ScreenMapping(
            QRect(100, 0, 100, 100),
            {"left": 100, "top": 0, "width": 100, "height": 100},
        ),
    ]

    with pytest.raises(ValueError, match="one screen"):
        coordinates.qt_rect_to_native_bbox(QRect(90, 10, 20, 20), mappings)


@pytest.mark.parametrize("ratio", [1.0, 1.25, 1.5, 2.0])
def test_coordinate_round_trip_has_at_most_one_pixel_variance(ratio):
    coordinates = _coordinates()
    mapping = coordinates.ScreenMapping(
        QRect(-400, 100, 800, 600),
        {
            "left": -1000,
            "top": 50,
            "width": round(800 * ratio),
            "height": round(600 * ratio),
        },
    )
    original = QRect(-363, 141, 123, 77)

    native = coordinates.qt_rect_to_native_bbox(original, [mapping])
    restored = coordinates.native_bbox_to_qt_rect(native, [mapping])

    for accessor in ("x", "y", "width", "height"):
        assert abs(getattr(restored, accessor)() - getattr(original, accessor)()) <= 1


def test_native_bbox_crossing_screens_is_rejected():
    coordinates = _coordinates()
    mappings = [
        coordinates.ScreenMapping(
            QRect(0, 0, 100, 100),
            {"left": 0, "top": 0, "width": 200, "height": 200},
        ),
        coordinates.ScreenMapping(
            QRect(100, 0, 100, 100),
            {"left": 200, "top": 0, "width": 200, "height": 200},
        ),
    ]

    with pytest.raises(ValueError, match="one screen"):
        coordinates.native_bbox_to_qt_rect(
            {"left": 190, "top": 20, "width": 20, "height": 20}, mappings
        )


def test_build_screen_mappings_pairs_by_geometry_and_sorts_logically():
    coordinates = _coordinates()
    qt_screens = [
        _FakeScreen(QRect(0, 0, 1920, 1080)),
        _FakeScreen(QRect(-1280, 0, 1280, 1024)),
    ]
    native_monitors = [
        {"left": 0, "top": 0, "width": 3840, "height": 2160},
        {"left": -1600, "top": 0, "width": 1600, "height": 1280},
    ]

    mappings = coordinates.build_screen_mappings(qt_screens, reversed(native_monitors))

    assert [mapping.logical.x() for mapping in mappings] == [-1280, 0]
    assert [mapping.native["left"] for mapping in mappings] == [-1600, 0]


def test_build_screen_mappings_single_screen_pairs_directly():
    coordinates = _coordinates()
    native = {"left": -10, "top": 20, "width": 3000, "height": 2000}

    mappings = coordinates.build_screen_mappings(
        [_FakeScreen(QRect(50, 60, 1000, 700))], [native]
    )

    assert len(mappings) == 1
    assert mappings[0].logical == QRect(50, 60, 1000, 700)
    assert mappings[0].native == native


def test_build_screen_mappings_raises_when_pairing_is_ambiguous():
    coordinates = _coordinates()
    qt_screens = [
        _FakeScreen(QRect(0, 0, 800, 600)),
        _FakeScreen(QRect(0, 0, 800, 600)),
    ]
    native_monitors = [
        {"left": 0, "top": 0, "width": 1600, "height": 1200},
        {"left": 0, "top": 0, "width": 1600, "height": 1200},
    ]

    with pytest.raises(RuntimeError, match="unambiguous"):
        coordinates.build_screen_mappings(qt_screens, native_monitors)


def test_build_screen_mappings_rejects_near_symmetric_same_aspect_pairing():
    coordinates = _coordinates()
    qt_screens = [
        _FakeScreen(QRect(0, 0, 1920, 1080)),
        _FakeScreen(QRect(10, 0, 1920, 1080)),
    ]
    native_monitors = [
        {"left": 0, "top": 0, "width": 3840, "height": 2160},
        {"left": 25, "top": 0, "width": 3840, "height": 2160},
    ]

    with pytest.raises(RuntimeError, match="unambiguous"):
        coordinates.build_screen_mappings(qt_screens, native_monitors)


def test_build_screen_mappings_rejects_incompatible_screen_aspect():
    coordinates = _coordinates()
    qt_screens = [
        _FakeScreen(QRect(0, 0, 1920, 1080)),
        _FakeScreen(QRect(1920, 0, 1600, 1200)),
    ]
    native_monitors = [
        {"left": 0, "top": 0, "width": 3840, "height": 2160},
        {"left": 3840, "top": 0, "width": 2560, "height": 1440},
    ]

    with pytest.raises(RuntimeError, match="compatible"):
        coordinates.build_screen_mappings(qt_screens, native_monitors)


def test_build_screen_mappings_rejects_incompatible_topology():
    coordinates = _coordinates()
    qt_screens = [
        _FakeScreen(QRect(0, 0, 1920, 1080)),
        _FakeScreen(QRect(1920, 100, 1920, 1080)),
    ]
    native_monitors = [
        {"left": 0, "top": 0, "width": 3840, "height": 2160},
        {"left": 50, "top": 2160, "width": 3840, "height": 2160},
    ]

    with pytest.raises(RuntimeError, match="compatible"):
        coordinates.build_screen_mappings(qt_screens, native_monitors)


def test_build_screen_mappings_pairs_clear_same_aspect_layout_deterministically():
    coordinates = _coordinates()
    qt_screens = [
        _FakeScreen(QRect(1920, 0, 1920, 1080)),
        _FakeScreen(QRect(0, 0, 1920, 1080)),
    ]
    native_monitors = [
        {"left": 3840, "top": 0, "width": 3840, "height": 2160},
        {"left": 0, "top": 0, "width": 3840, "height": 2160},
    ]

    mappings = coordinates.build_screen_mappings(qt_screens, native_monitors)

    assert [mapping.logical.x() for mapping in mappings] == [0, 1920]
    assert [mapping.native["left"] for mapping in mappings] == [0, 3840]


def test_build_screen_mappings_raises_for_different_screen_counts():
    coordinates = _coordinates()

    with pytest.raises(RuntimeError, match="same number"):
        coordinates.build_screen_mappings(
            [_FakeScreen(QRect(0, 0, 800, 600))],
            [
                {"left": 0, "top": 0, "width": 800, "height": 600},
                {"left": 800, "top": 0, "width": 800, "height": 600},
            ],
        )


def test_virtual_qt_geometry_unites_all_screens(monkeypatch):
    coordinates = _coordinates()
    screens = [
        _FakeScreen(QRect(-1280, -200, 1280, 720)),
        _FakeScreen(QRect(0, 0, 1920, 1080)),
    ]

    class _FakeApplication:
        @staticmethod
        def screens():
            return screens

    monkeypatch.setattr(coordinates, "QGuiApplication", _FakeApplication)

    assert coordinates.virtual_qt_geometry() == QRect(-1280, -200, 3200, 1280)

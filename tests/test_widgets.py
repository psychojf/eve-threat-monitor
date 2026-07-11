# -*- coding: utf-8 -*-
# Tests de régression headless des widgets overlay sensibles aux coordonnées
# (sélecteur, miroir, slider) — géométries écran synthétiques, zéro capture.
from types import SimpleNamespace

from PyQt6.QtCore import QPointF, QRect, QSize, Qt
from PyQt6.QtWidgets import QWidget

from tm import qtutil
from tm.widgets import area_selector, mirror_window, transparency_slider


class _FakeScreen:
    def __init__(self, geometry: QRect) -> None:
        self._geometry = QRect(geometry)

    def availableGeometry(self) -> QRect:
        return QRect(self._geometry)


def _patch_screens(monkeypatch, geometries):
    screens = [_FakeScreen(geometry) for geometry in geometries]

    class _FakeApplication:
        @staticmethod
        def screens():
            return screens

        @staticmethod
        def primaryScreen():
            return screens[0]

    monkeypatch.setattr(qtutil, "QGuiApplication", _FakeApplication)
    return screens


def test_clamp_to_screen_keeps_the_complete_window_inside(monkeypatch):
    _patch_screens(monkeypatch, [QRect(0, 0, 800, 800)])

    assert qtutil.clamp_to_screen(799, 799, 200, 100) == (600, 700)


def test_clamp_to_screen_uses_the_screen_with_most_intersection(monkeypatch):
    _patch_screens(
        monkeypatch,
        [QRect(0, 0, 800, 800), QRect(800, 0, 800, 800)],
    )

    assert qtutil.clamp_to_screen(750, 100, 200, 100) == (800, 100)


def test_clamp_to_screen_pins_oversized_window_to_screen_origin(monkeypatch):
    _patch_screens(monkeypatch, [QRect(-500, 100, 400, 300)])

    assert qtutil.clamp_to_screen(-300, 200, 600, 500) == (-500, 100)


def test_fit_minimum_rect_stays_inside_bounds():
    fitted = area_selector._fit_minimum_rect(
        QRect(795, 795, 1, 1), QRect(0, 0, 800, 800)
    )

    assert fitted == QRect(790, 790, 10, 10)


class _SelectorMSS:
    monitors = [
        {"left": 0, "top": 0, "width": 800, "height": 800},
        {"left": 0, "top": 0, "width": 800, "height": 800},
    ]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _MouseEvent:
    def __init__(self, x, y) -> None:
        self._position = QPointF(x, y)

    def globalPosition(self):
        return self._position

    def button(self):
        return Qt.MouseButton.LeftButton

    def buttons(self):
        return Qt.MouseButton.LeftButton


class _KeyEvent:
    def __init__(self, key) -> None:
        self._key = key
        self.accepted = False

    def key(self):
        return self._key

    def accept(self):
        self.accepted = True


def _patch_selector(monkeypatch):
    mapping = SimpleNamespace(
        logical=QRect(0, 0, 800, 800),
        native={"left": 0, "top": 0, "width": 1600, "height": 1600},
    )
    # Filet de sécurité : si un patch ci-dessous manquait un chemin, MSS()
    # toucherait le vrai écran. Patché sur le module mss lui-même — le
    # sélecteur n'importe plus mss directement.
    monkeypatch.setattr("mss.mss.MSS", _SelectorMSS, raising=False)
    monkeypatch.setattr("mss.MSS", _SelectorMSS, raising=False)
    monkeypatch.setattr(
        area_selector, "virtual_qt_geometry", lambda: QRect(0, 0, 800, 800),
        raising=False,
    )
    monkeypatch.setattr(
        area_selector, "build_screen_mappings", lambda: [mapping], raising=False
    )
    monkeypatch.setattr(
        area_selector,
        "qt_rect_to_native_bbox",
        lambda rect, mappings=None: {
            "left": rect.x() * 2,
            "top": rect.y() * 2,
            "width": rect.width() * 2,
            "height": rect.height() * 2,
        },
        raising=False,
    )
    monkeypatch.setattr(
        area_selector,
        "native_bbox_to_qt_rect",
        lambda bbox, mappings=None: QRect(
            bbox["left"] // 2,
            bbox["top"] // 2,
            bbox["width"] // 2,
            bbox["height"] // 2,
        ),
        raising=False,
    )
    for method in ("show", "activateWindow", "setFocus", "grabKeyboard", "releaseKeyboard"):
        monkeypatch.setattr(area_selector.AreaSelector, method, lambda self: None)
    return mapping


def _patch_two_screen_selector(monkeypatch):
    _patch_selector(monkeypatch)
    mappings = [
        SimpleNamespace(
            logical=QRect(0, 0, 800, 800),
            native={"left": 0, "top": 0, "width": 1600, "height": 1600},
        ),
        SimpleNamespace(
            logical=QRect(800, 0, 800, 800),
            native={"left": 1600, "top": 0, "width": 1600, "height": 1600},
        ),
    ]
    monkeypatch.setattr(area_selector, "build_screen_mappings", lambda: mappings)
    monkeypatch.setattr(
        area_selector, "virtual_qt_geometry", lambda: QRect(0, 0, 1600, 800)
    )
    return mappings


def test_selector_drag_starting_at_zero_zero_updates(qapp, monkeypatch):
    _patch_selector(monkeypatch)
    selector = area_selector.AreaSelector(lambda bbox: None)

    selector.mousePressEvent(_MouseEvent(0, 0))
    selector.mouseMoveEvent(_MouseEvent(20, 20))

    assert selector.current_rect is not None
    assert selector.current_rect.width() > 1
    assert selector.current_rect.height() > 1
    selector.deleteLater()


def test_selector_converts_logical_selection_before_callback(qapp, monkeypatch):
    _patch_selector(monkeypatch)
    selected = []
    selector = area_selector.AreaSelector(selected.append)
    selector.current_rect = QRect(10, 20, 30, 40)
    selector._selection_bounds = QRect(0, 0, 800, 800)
    monkeypatch.setattr(selector, "close", lambda: None)

    event = _KeyEvent(Qt.Key.Key_Return)
    selector.keyPressEvent(event)

    assert selected == [{"left": 20, "top": 40, "width": 60, "height": 80}]
    assert event.accepted
    selector.deleteLater()


def test_selector_converts_previous_native_bbox_for_painting(qapp, monkeypatch):
    _patch_selector(monkeypatch)
    calls = []
    previous = {"left": 20, "top": 40, "width": 60, "height": 80}
    monkeypatch.setattr(
        area_selector,
        "native_bbox_to_qt_rect",
        lambda bbox, mappings=None: calls.append((bbox, mappings)) or QRect(10, 20, 30, 40),
        raising=False,
    )

    selector = area_selector.AreaSelector(lambda bbox: None, previous)

    assert calls and calls[0][0] == previous
    selector.deleteLater()


def test_selector_stale_cross_screen_previous_bbox_keeps_selector_usable(
    qapp, monkeypatch
):
    _patch_two_screen_selector(monkeypatch)
    previous = {"left": 1590, "top": 20, "width": 20, "height": 40}
    monkeypatch.setattr(
        area_selector,
        "native_bbox_to_qt_rect",
        lambda bbox, mappings=None: (_ for _ in ()).throw(
            ValueError("rectangle must be contained in one screen")
        ),
    )
    selected = []

    try:
        selector = area_selector.AreaSelector(selected.append, previous)
    except ValueError:
        selector = None

    assert selector is not None
    released = []
    closed = []
    monkeypatch.setattr(selector, "releaseKeyboard", lambda: released.append(True))
    monkeypatch.setattr(selector, "close", lambda: closed.append(True))
    event = _KeyEvent(Qt.Key.Key_Return)
    selector.keyPressEvent(event)
    assert selected == []
    assert released == []
    assert closed == []
    assert selector.selection_error
    assert event.accepted
    selector.deleteLater()


def test_selector_cross_screen_drag_is_not_silently_clipped(qapp, monkeypatch):
    _patch_two_screen_selector(monkeypatch)
    selector = area_selector.AreaSelector(lambda bbox: None)

    selector.mousePressEvent(_MouseEvent(790, 20))
    selector.mouseMoveEvent(_MouseEvent(810, 40))

    assert selector.current_rect.x() + selector.current_rect.width() > 800
    assert selector.selection_error
    selector.deleteLater()


def test_selector_cross_screen_enter_does_not_confirm_or_close(qapp, monkeypatch):
    _patch_two_screen_selector(monkeypatch)
    selected = []
    selector = area_selector.AreaSelector(selected.append)
    released = []
    closed = []
    monkeypatch.setattr(selector, "releaseKeyboard", lambda: released.append(True))
    monkeypatch.setattr(selector, "close", lambda: closed.append(True))
    selector.mousePressEvent(_MouseEvent(790, 20))
    selector.mouseMoveEvent(_MouseEvent(810, 40))

    event = _KeyEvent(Qt.Key.Key_Return)
    selector.keyPressEvent(event)

    assert selected == []
    assert released == []
    assert closed == []
    assert selector.selection_error
    assert event.accepted
    selector.deleteLater()


def test_selector_conversion_failure_keeps_keyboard_and_window(qapp, monkeypatch):
    _patch_selector(monkeypatch)
    monkeypatch.setattr(
        area_selector,
        "qt_rect_to_native_bbox",
        lambda rect, mappings=None: (_ for _ in ()).throw(
            ValueError("conversion failed")
        ),
    )
    selected = []
    selector = area_selector.AreaSelector(selected.append)
    selector.current_rect = QRect(10, 20, 30, 40)
    selector._selection_bounds = QRect(0, 0, 800, 800)
    released = []
    closed = []
    monkeypatch.setattr(selector, "releaseKeyboard", lambda: released.append(True))
    monkeypatch.setattr(selector, "close", lambda: closed.append(True))
    event = _KeyEvent(Qt.Key.Key_Return)
    errors = []

    try:
        selector.keyPressEvent(event)
    except ValueError as exc:
        errors.append(exc)

    assert errors == []
    assert selected == []
    assert released == []
    assert closed == []
    assert selector.selection_error == "conversion failed"
    assert event.accepted
    selector.deleteLater()


def test_selector_valid_new_drag_clears_previous_rejection(qapp, monkeypatch):
    _patch_two_screen_selector(monkeypatch)
    selector = area_selector.AreaSelector(lambda bbox: None)
    selector.mousePressEvent(_MouseEvent(790, 20))
    selector.mouseMoveEvent(_MouseEvent(810, 40))
    assert selector.selection_error

    selector.mousePressEvent(_MouseEvent(100, 100))
    selector.mouseMoveEvent(_MouseEvent(130, 140))

    assert selector.selection_error is None
    selector.deleteLater()


THEME = {
    "BG": "#101010",
    "BORDER": "#303030",
    "DIM": "#808080",
    "RED": "#ff0000",
    "CYAN": "#00ffff",
    "WHITE": "#ffffff",
}


class _Shot:
    def __init__(self, width=150, height=75) -> None:
        self.width = width
        self.height = height
        self.raw = bytes(width * height * 4)


class _MirrorMSS:
    def __init__(self, shot=None, error=None) -> None:
        self.shot = shot or _Shot()
        self.error = error
        self.closed = False

    def grab(self, bbox):
        if self.error is not None:
            raise self.error
        return self.shot

    def close(self):
        self.closed = True


def _make_mirror(monkeypatch, sct=None):
    sct = sct or _MirrorMSS()
    monkeypatch.setattr(mirror_window.mss, "MSS", lambda: sct)
    monkeypatch.setattr(
        mirror_window,
        "native_bbox_to_qt_rect",
        lambda bbox, mappings=None: QRect(0, 0, 100, 50),
        raising=False,
    )
    monkeypatch.setattr(
        mirror_window, "clamp_to_screen", lambda x, y, w, h: (x, y)
    )
    window = mirror_window.MirrorWindow(
        {"left": 0, "top": 0, "width": 150, "height": 75},
        THEME,
        {"x": 50, "y": 60},
    )
    window._timer.stop()
    return window


def test_mirror_uses_logical_content_size(qapp, monkeypatch):
    window = _make_mirror(monkeypatch)

    assert window._image_lbl.size() == QSize(100, 50)
    assert window.size() == QSize(100, 70)
    window.close()


def test_mirror_displays_capture_at_logical_size(qapp, monkeypatch):
    window = _make_mirror(monkeypatch)

    window._refresh()

    pixmap = window._image_lbl.pixmap()
    assert pixmap is not None
    assert pixmap.deviceIndependentSize().toSize() == QSize(100, 50)
    window.close()


def test_mirror_exposes_global_logical_content_rect(qapp, monkeypatch):
    window = _make_mirror(monkeypatch)

    assert window.get_content_qt_rect() == QRect(50, 80, 100, 50)
    window.close()


def test_mirror_capture_failures_are_tolerated_for_ten_elapsed_seconds(
    qapp, monkeypatch
):
    now = [0.0]
    sct = _MirrorMSS(error=RuntimeError("capture unavailable"))
    monkeypatch.setattr(
        mirror_window, "time", SimpleNamespace(monotonic=lambda: now[0]), raising=False
    )
    window = _make_mirror(monkeypatch, sct)
    closed_at = []
    monkeypatch.setattr(window, "close", lambda: closed_at.append(now[0]))

    for value in (0.0, 0.1, 0.2, 0.3, 0.4):
        now[0] = value
        window._refresh()

    assert closed_at == []
    now[0] = 10.0
    window._refresh()
    assert closed_at == [10.0]
    window.deleteLater()


def test_mirror_capture_health_tracks_grab_results(qapp, monkeypatch):
    # Pendant une panne de capture SOURCE, le miroir affiche son dernier frame
    # figé ; le moniteur ne doit jamais l'analyser comme un échantillon valide.
    # Le miroir expose donc la santé de sa propre capture : False tant qu'aucun
    # grab n'a réussi, True après un succès, False dès le premier échec.
    sct = _MirrorMSS()
    window = _make_mirror(monkeypatch, sct)

    assert window.capture_healthy is False   # aucun grab encore réussi

    window._refresh()
    assert window.capture_healthy is True

    sct.error = RuntimeError("source unavailable")
    window._refresh()
    assert window.capture_healthy is False
    window.close()


def test_mirror_capture_failure_logs_are_throttled(qapp, monkeypatch):
    now = [0.0]
    sct = _MirrorMSS(error=RuntimeError("capture unavailable"))
    monkeypatch.setattr(
        mirror_window, "time", SimpleNamespace(monotonic=lambda: now[0]), raising=False
    )
    window = _make_mirror(monkeypatch, sct)
    logged = []
    monkeypatch.setattr(mirror_window.log, "error", lambda *args: logged.append(args))

    for value in (0.0, 0.1, 0.2, 1.1):
        now[0] = value
        window._refresh()

    assert len(logged) == 2
    window.close()


def test_transparency_slider_complete_rect_stays_on_screen(qapp, monkeypatch):
    bounds = QRect(0, 0, 800, 800)
    _patch_screens(monkeypatch, [bounds])
    parent = QWidget()
    parent.theme = THEME
    parent.setFixedSize(100, 50)
    parent.move(750, 740)

    slider = transparency_slider.TransparencySlider(parent)

    assert slider.x() >= bounds.x()
    assert slider.y() >= bounds.y()
    assert slider.x() + slider.width() <= bounds.x() + bounds.width()
    assert slider.y() + slider.height() <= bounds.y() + bounds.height()
    slider.close()
    parent.close()

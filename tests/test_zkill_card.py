# -*- coding: utf-8 -*-
# Tests headless de la carte zKill : cycle de vie, placement et layout.
# Aucun réseau : le pool de lookup est remplacé par des enregistreurs.
import pytest
from PyQt6.QtCore import QEvent, QPointF, QRect, Qt
from PyQt6.QtGui import QMouseEvent

from tm import qtutil
from tm.themes import DEFAULT_THEME_NAME, THEMES
import tm.zkill_card as cardmod


class _FakeJob:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _FakeScreen:
    def __init__(self, geometry):
        self._geometry = QRect(geometry)

    def availableGeometry(self):
        return QRect(self._geometry)


def _patch_screen(monkeypatch, geometry=QRect(0, 0, 800, 800)):
    screen = _FakeScreen(geometry)

    class FakeApplication:
        @staticmethod
        def screens():
            return [screen]

        @staticmethod
        def primaryScreen():
            return screen

        @staticmethod
        def screenAt(point):
            return screen

    monkeypatch.setattr(qtutil, "QGuiApplication", FakeApplication)
    monkeypatch.setattr(cardmod, "QGuiApplication", FakeApplication, raising=False)
    return geometry


def _make_card(qapp, monkeypatch, parent_rect=QRect(100, 100, 165, 118)):
    _patch_screen(monkeypatch)
    job = _FakeJob()
    monkeypatch.setattr(
        cardmod, "submit_lookup",
        lambda name, ready, error: job,
        raising=False,
    )
    monkeypatch.setattr(cardmod, "fetch_pilot_stats", lambda *args: None, raising=False)
    card = cardmod.ZkillCard(
        "A Very Long Pilot Name For Testing",
        THEMES[DEFAULT_THEME_NAME],
        parent_rect,
    )
    return card, job


def test_card_prefers_left_when_right_side_is_offscreen(qapp, monkeypatch):
    card, _ = _make_card(qapp, monkeypatch, QRect(750, 100, 40, 100))

    assert card.x() + card.width() <= 800
    assert card.x() < 750
    card.close()


def test_card_reclamps_after_loaded_height_change(qapp, monkeypatch):
    card, _ = _make_card(qapp, monkeypatch, QRect(300, 760, 100, 30))
    card._on_ready({
        "tags": ["DANGEROUS", "SOLO HUNTER", "HIGH EFF.", "SOLO KILL"],
        "top_ships": [],
        "active_zone": "",
    })

    assert card.y() >= 0
    assert card.y() + card.height() <= 800
    card.close()


def test_header_name_is_elided_before_close_button(qapp, monkeypatch):
    card, _ = _make_card(qapp, monkeypatch)
    rendered = card._elided_header_name("A" * 37)

    metrics = card.fontMetrics()
    available = card._close_rect.x() - 35
    assert metrics.horizontalAdvance(rendered) <= available
    assert rendered != "A" * 37
    card.close()


def test_four_tags_wrap_without_exceeding_card_width(qapp, monkeypatch):
    card, _ = _make_card(qapp, monkeypatch)
    tags = ["DANGEROUS", "SOLO HUNTER", "HIGH EFF.", "SOLO KILL"]

    placements, height = card._tag_layout(tags)

    assert height > 36
    assert len({rect.y() for _, rect in placements}) > 1
    assert all(rect.x() >= 10 and rect.x() + rect.width() <= cardmod.W - 10
               for _, rect in placements)
    card.close()


def test_closing_card_cancels_lookup_job(qapp, monkeypatch):
    card, job = _make_card(qapp, monkeypatch)

    card.close()

    assert job.cancelled


def test_manual_drag_release_reclamps_complete_card(qapp, monkeypatch):
    card, _ = _make_card(qapp, monkeypatch)
    card.move(-5000, 9000)

    card.mouseReleaseEvent(None)

    assert card.x() >= 0
    assert card.y() >= 0
    assert card.x() + card.width() <= 800
    assert card.y() + card.height() <= 800
    card.close()


def test_expanding_card_near_bottom_reclamps_complete_height(qapp, monkeypatch):
    card, _ = _make_card(qapp, monkeypatch, QRect(300, 760, 100, 30))
    card._on_ready({
        "tags": ["DANGEROUS"],
        "top_ships": [("Vedmak", 4), ("Loki", 2), ("Hecate", 1)],
        "active_zone": "Tama",
    })
    card._more_rect = QRect(10, 10, 50, 18)
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(20, 15),
        QPointF(20, 15),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    card.mousePressEvent(event)

    assert card._expanded
    assert card.y() >= 0
    assert card.y() + card.height() <= 800
    card.close()


def test_danger_drawer_advance_matches_declared_section_height(
    qapp, monkeypatch,
):
    card, _ = _make_card(qapp, monkeypatch)

    class NullPainter:
        def __init__(self):
            self._font = card._f(10)

        def setPen(self, *args):
            pass

        def setFont(self, font):
            self._font = font

        def font(self):
            return self._font

        def drawText(self, *args):
            pass

        def fillRect(self, *args):
            pass

    end = card._draw_danger(
        NullPainter(),
        card.theme,
        {"vcol": "RED", "danger": 75, "verdict": "DANGEROUS"},
        0,
    )

    assert end == cardmod._H_DANGER
    card.close()


@pytest.mark.parametrize(
    "message",
    ["UNKNOWN PILOT", "NO DATA", "RATE LIMITED", "NETWORK ERROR"],
)
def test_unavailable_intelligence_uses_neutral_color(
    qapp, monkeypatch, message,
):
    card, _ = _make_card(qapp, monkeypatch)
    card._error_msg = message

    assert card._loading_message_color() == card.theme["DIM"]
    card.close()

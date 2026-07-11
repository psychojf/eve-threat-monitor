# -*- coding: utf-8 -*-
# Tests de tm.themes — intégrité du registre et helpers couleur (offscreen).
import re

import pytest

# conftest.py a déjà posé QT_QPA_PLATFORM=offscreen avant cet import
from tm.themes import THEMES, DEFAULT_THEME_NAME, hex_to_qcolor

REQUIRED_KEYS = {"BG", "BG_PANEL", "BORDER", "CYAN", "RED",
                 "GREEN", "YELLOW", "DIM", "WHITE"}
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def test_default_theme_exists():
    assert DEFAULT_THEME_NAME in THEMES

@pytest.mark.parametrize("name", list(THEMES))
def test_theme_has_all_keys(name):
    assert REQUIRED_KEYS.issubset(THEMES[name].keys())

@pytest.mark.parametrize("name", list(THEMES))
def test_theme_colors_are_valid_hex(name):
    for key in REQUIRED_KEYS:
        assert HEX_RE.match(THEMES[name][key]), f"{name}[{key}] = {THEMES[name][key]!r}"

def test_hex_to_qcolor():
    c = hex_to_qcolor("#3dd8e0")
    assert (c.red(), c.green(), c.blue(), c.alpha()) == (0x3d, 0xd8, 0xe0, 255)

def test_hex_to_qcolor_alpha_and_no_hash():
    c = hex_to_qcolor("ffffff", alpha=128)
    assert (c.red(), c.green(), c.blue(), c.alpha()) == (255, 255, 255, 128)

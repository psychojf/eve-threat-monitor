# -*- coding: utf-8 -*-
# Tests de tm.config — chargement/sauvegarde JSON (fichier temporaire, jamais le vrai).
import json
import os

import pytest

import tm.config as config
from tm.config import load_config, save_config


def test_load_missing_file_returns_empty(tmp_config):
    assert load_config() == {}

def test_save_then_load_roundtrip(tmp_config):
    cfg = {"theme": "Caldari", "opacity": 0.8,
           "detection_bbox": {"top": 1, "left": 2, "width": 3, "height": 4}}
    save_config(cfg)
    assert load_config() == cfg

def test_load_corrupt_json_returns_empty(tmp_config):
    tmp_config.write_text("{not valid json", encoding="utf-8")
    assert load_config() == {}

def test_load_rejects_non_dict_json(tmp_config):
    # Un JSON valide mais qui n'est pas un objet (liste, chaîne…) doit donner {}
    # — monitor.py appelle .get() sur le résultat dès le démarrage.
    tmp_config.write_text('["not", "a", "dict"]', encoding="utf-8")
    assert load_config() == {}
    tmp_config.write_text('"just a string"', encoding="utf-8")
    assert load_config() == {}

def test_load_discards_invalid_nested_fields_but_keeps_valid_ones(tmp_config):
    tmp_config.write_text(json.dumps({
        "theme": "ORE",
        "opacity": "opaque",
        "win_geom": {"x": 10, "y": "bad"},
        "detection_bbox": {"top": 1, "left": 2, "width": -3, "height": 4},
        "mirror_position": {"x": -1200, "y": 50},
    }), encoding="utf-8")

    assert load_config() == {
        "theme": "ORE",
        "mirror_position": {"x": -1200, "y": 50},
    }

def test_load_clamps_finite_numeric_opacity(tmp_config):
    tmp_config.write_text('{"opacity": 5}', encoding="utf-8")
    assert load_config()["opacity"] == 1.0

def test_normalize_config_clamps_arbitrarily_large_integer_opacity():
    assert config.normalize_config({"opacity": 10 ** 1000}) == {"opacity": 1.0}

@pytest.mark.parametrize("opacity", [True, "0.5", float("nan"), float("inf"), -float("inf")])
def test_normalize_config_rejects_nonfinite_or_nonnumeric_opacity(opacity):
    assert config.normalize_config({"opacity": opacity}) == {}

def test_normalize_config_keeps_valid_points_bboxes_and_coordinate_version():
    assert config.normalize_config({
        "theme": "ORE",
        "opacity": 0.1,
        "win_geom": {"x": -1, "y": 2},
        "detection_bbox": {"top": -3, "left": -4, "width": 5, "height": 6},
        "mirror_bbox": {"top": 7, "left": 8, "width": 9, "height": 10},
        "relative_bbox": {
            "offset_left": 0, "offset_top": 1, "width": 2, "height": 3,
        },
        "coordinate_space_version": 1,
    }) == {
        "theme": "ORE",
        "opacity": 0.2,
        "win_geom": {"x": -1, "y": 2},
        "detection_bbox": {"top": -3, "left": -4, "width": 5, "height": 6},
        "mirror_bbox": {"top": 7, "left": 8, "width": 9, "height": 10},
        "relative_bbox": {
            "offset_left": 0, "offset_top": 1, "width": 2, "height": 3,
        },
        "coordinate_space_version": 1,
    }

def test_normalize_relative_bbox_requires_nonnegative_offsets():
    assert config.normalize_config({"relative_bbox": {
        "offset_left": -1, "offset_top": 0, "width": 10, "height": 10,
    }}) == {}

@pytest.mark.parametrize("data", [None, [], "config", 1, True])
def test_normalize_config_rejects_non_objects(data):
    assert config.normalize_config(data) == {}

def test_normalize_config_discards_bool_coordinates_and_invalid_version():
    assert config.normalize_config({
        "win_geom": {"x": True, "y": 2},
        "detection_bbox": {"top": 1, "left": 2, "width": 3.0, "height": 4},
        "coordinate_space_version": 0,
    }) == {}

def test_save_is_atomic_no_temp_leftover(tmp_config):
    save_config({"a": 1})
    save_config({"a": 2})
    leftovers = [f for f in os.listdir(tmp_config.parent) if f.endswith(".tmp")]
    assert leftovers == []
    assert json.loads(tmp_config.read_text(encoding="utf-8")) == {"a": 2}

def test_save_failure_does_not_raise(monkeypatch):
    # Dossier inexistant : save_config logue et n'explose pas l'appelant.
    import tm.config as config
    monkeypatch.setattr(config, "CONFIG_FILE",
                        os.path.join("Z:\\__nonexistent__", "cfg.json"))
    save_config({"a": 1})   # ne doit pas lever

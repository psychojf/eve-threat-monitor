# -*- coding: utf-8 -*-
# Tests d'intégration headless de ThreatMonitor (Qt offscreen).
#
# La machine d'état est pilotée en appelant _update_monitor() directement
# (le timer 1 Hz ne tourne pas sans boucle d'événements) avec detect_threats
# monkeypatché — aucun accès écran réel.
import pytest
from PyQt6.QtCore import QEvent, QPointF, QRect, Qt
from PyQt6.QtGui import QCloseEvent, QMouseEvent


def _arm_detection(monitor, monkeypatch, results):
    # Prépare un monitoring réel (non-fake) : bbox factice + detect_threats
    # scripté. `results` est une liste de tuples (menaces, alliés) consommée
    # à chaque poll ; une entrée Exception est levée à la place.
    import tm.monitor as monitor_mod
    monitor._bbox = {"top": 0, "left": 0, "width": 100, "height": 40}
    seq = iter(results)

    def scripted(*a, **k):
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(monitor_mod, "detect_threats", scripted)


# ── États de base ─────────────────────────────────────────────────────────────

def test_initial_state_is_idle(monitor):
    assert monitor._state == "idle"
    assert not monitor._monitoring


def test_main_window_has_accessible_title(monitor):
    assert monitor.windowTitle() == "EVE Threat Monitor"

def test_start_monitoring_sets_clear(monitor):
    monitor._start_monitoring()
    assert monitor._monitoring
    assert monitor._state == "clear"

def test_stop_monitoring_returns_to_idle(monitor):
    monitor._start_monitoring()
    monitor._stop_monitoring()
    assert not monitor._monitoring
    assert monitor._state == "idle"


# ── Menaces / alertes ─────────────────────────────────────────────────────────

def test_threat_triggers_hostile_and_alert(monitor, audio_calls, monkeypatch):
    monitor._start_monitoring()
    _arm_detection(monitor, monkeypatch, [(2, 0)])
    monitor._update_monitor()
    assert monitor._state == "hostile"
    assert monitor.is_alerting
    assert audio_calls["play"] >= 1
    assert audio_calls["notify"] >= 1

def test_acknowledge_stops_alert_loop(monitor, audio_calls, monkeypatch):
    monitor._start_monitoring()
    _arm_detection(monitor, monkeypatch, [(1, 0)])
    monitor._update_monitor()
    monitor._acknowledge_alert()          # Espace
    assert monitor.alert_acknowledged
    assert not monitor.is_alerting
    assert not monitor._alert_timer.isActive()

def test_acknowledge_stops_active_sound(monitor, audio_calls, monkeypatch):
    # Espace doit couper le WAV en cours de lecture, pas seulement le timer de
    # répétition — sinon un son long continue après l'acquittement.
    monitor._start_monitoring()
    _arm_detection(monitor, monkeypatch, [(1, 0)])
    monitor._update_monitor()
    assert monitor.is_alerting
    monitor._acknowledge_alert()
    assert audio_calls["stop"] >= 1

def test_acknowledge_when_not_alerting_does_not_touch_audio(monitor, audio_calls):
    # Le chemin « clear » appelle l'ack à chaque poll : pas de stop_sound à 1 Hz.
    monitor._start_monitoring()
    monitor._acknowledge_alert()
    assert audio_calls["stop"] == 0

def test_clear_transition_acknowledges_alert(monitor, audio_calls, monkeypatch):
    monitor._start_monitoring()
    _arm_detection(monitor, monkeypatch, [(1, 0), (0, 0)])
    monitor._update_monitor()             # hostile
    monitor._update_monitor()             # retour au calme
    assert monitor._state == "clear"
    assert not monitor.is_alerting
    assert not monitor._alert_timer.isActive()

def test_escalation_replays_alert(monitor, audio_calls, monkeypatch):
    # +1 menace pendant une alerte acquittée → nouvelle alerte.
    monitor._start_monitoring()
    _arm_detection(monitor, monkeypatch, [(1, 0), (2, 0)])
    monitor._update_monitor()
    monitor._acknowledge_alert()
    monitor._update_monitor()
    assert not monitor.alert_acknowledged
    assert monitor.is_alerting


# ── Pause ─────────────────────────────────────────────────────────────────────

def test_pause_silences_and_sets_paused(monitor, audio_calls, monkeypatch):
    monitor._start_monitoring()
    _arm_detection(monitor, monkeypatch, [(1, 0), (1, 0)])
    monitor._update_monitor()
    monitor._toggle_pause()
    assert monitor._alerts_paused
    assert audio_calls["stop"] >= 1
    monitor._update_monitor()             # poll pendant la pause
    assert monitor._state == "paused"

def test_unpause_with_threats_resumes_hostile(monitor, audio_calls, monkeypatch):
    monitor._start_monitoring()
    _arm_detection(monitor, monkeypatch, [(1, 0)])
    monitor._update_monitor()
    monitor._toggle_pause()
    monitor._toggle_pause()
    assert monitor._state == "hostile"
    assert not monitor.alert_acknowledged


# ── Échec de détection : fail loud, jamais de faux ALL CLEAR ─────────────────

def test_three_detection_errors_stop_monitoring_visibly(monitor, audio_calls, monkeypatch):
    # 3 erreurs consécutives → arrêt du monitoring, état MON FAIL, son + toast.
    # Un moniteur qui meurt en silence ferait croire qu'on est protégé.
    monitor._start_monitoring()
    _arm_detection(monitor, monkeypatch,
                   [RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")])
    plays_before = audio_calls["play"]
    for _ in range(3):
        monitor._update_monitor()
    assert not monitor._monitoring
    assert monitor._state == "monfail"
    assert audio_calls["play"] > plays_before
    assert audio_calls["notify"] >= 1

def test_error_counter_resets_after_clean_poll(monitor, audio_calls, monkeypatch):
    monitor._start_monitoring()
    _arm_detection(monitor, monkeypatch,
                   [RuntimeError("boom"), (0, 0), RuntimeError("boom"), RuntimeError("boom")])
    for _ in range(4):
        monitor._update_monitor()
    assert monitor._monitoring            # jamais 3 erreurs consécutives
    assert monitor._monitor_errors == 2

def test_relative_bbox_without_mirror_shows_nomirror(monitor, monkeypatch):
    # Miroir fermé en mode relatif : NO MIRROR, pas d'analyse de zone périmée.
    monitor._start_monitoring()
    monitor._bbox = None
    monitor._relative_bbox = {"offset_top": 0, "offset_left": 0, "width": 10, "height": 10}
    monitor.mirror_window = None
    monitor._update_monitor()
    assert monitor._state == "nomirror"


# ── Hardening regressions ────────────────────────────────────────────────────

def test_start_monitoring_mss_init_failure_fails_loud(
    monitor, audio_calls, monkeypatch,
):
    import tm.monitor as monitor_mod

    monitor._bbox = {"top": 0, "left": 0, "width": 16, "height": 40}
    before = dict(audio_calls)

    def fail_mss():
        raise RuntimeError("capture unavailable")

    monkeypatch.setattr(monitor_mod.mss, "MSS", fail_mss)
    monitor._start_monitoring()

    assert not monitor._monitoring
    assert monitor._sct is None
    assert not monitor.monitor_timer.isActive()
    assert monitor._state == "monfail"
    assert audio_calls["notify"] == before["notify"] + 1
    assert audio_calls["play"] == before["play"] + 1


def test_start_monitoring_enters_checking_before_first_poll(monitor, monkeypatch):
    monitor._bbox = {"top": 0, "left": 0, "width": 16, "height": 40}
    seen = []
    monkeypatch.setattr(
        monitor, "_update_monitor", lambda: seen.append(monitor._state)
    )

    monitor._start_monitoring()

    assert seen == ["checking"]
    assert monitor.monitor_timer.isActive()


def test_first_detection_error_never_displays_clear(monitor, monkeypatch):
    import tm.monitor as monitor_mod

    monitor._bbox = {"top": 0, "left": 0, "width": 16, "height": 40}
    monkeypatch.setattr(
        monitor_mod, "detect_threats", lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("first poll failed")
        )
    )

    monitor._start_monitoring()

    assert monitor._monitoring
    assert monitor._state == "checking"
    assert monitor._tw_target == "ERR 1/3"
    assert monitor._monitor_errors == 1


def test_zkill_scan_does_not_mask_hostile_state(monitor, monkeypatch):
    monitor._start_monitoring()
    monitor._toggle_zkill_scan()
    _arm_detection(monitor, monkeypatch, [(2, 0)])

    monitor._update_monitor()

    assert monitor._state == "hostile"
    assert monitor._tw_target == "HOSTILE"
    assert monitor._bracket_state == "hostile"
    assert monitor.title_lbl.text().startswith("◉")


def test_disabling_zkill_scan_preserves_paused_state(monitor):
    monitor._start_monitoring()
    monitor._toggle_pause()
    monitor._toggle_zkill_scan()
    monitor._toggle_zkill_scan()

    assert monitor._alerts_paused
    assert monitor._state == "paused"
    assert monitor.title_lbl.text().startswith("◈")


def test_disabling_zkill_scan_cannot_turn_capture_error_clear(
    monitor, monkeypatch,
):
    # L'indicateur de scan clipboard ne doit JAMAIS fabriquer un résultat de
    # sécurité : couper F5 pendant une panne de capture ne rend pas l'écran sûr.
    # Le flash « SCAN OFF » est un accusé de réception, pas un état — après sa
    # retombée, le texte revient à CHECKING, jamais à ALL CLEAR.
    import tm.monitor as monitor_mod

    monitor._bbox = {"top": 0, "left": 0, "width": 16, "height": 40}
    monitor._toggle_zkill_scan()
    monkeypatch.setattr(
        monitor_mod,
        "detect_threats",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("capture failed")),
    )
    monitor._start_monitoring()
    assert monitor._tw_target == "ERR 1/3"

    monitor._toggle_zkill_scan()

    assert monitor._state == "checking"
    assert monitor._tw_target == "SCAN OFF"
    monitor._restore_after_scan_flash()
    assert monitor._tw_target == "CHECKING"


def test_toggle_zkill_scan_flashes_scan_status(monitor):
    # F5 doit donner un retour immédiat et visible (l'ancien « SCAN ON »),
    # sans jamais modifier l'état de sécurité affiché.
    monitor._toggle_zkill_scan()
    assert monitor._tw_target == "SCAN ON"
    assert monitor._state == "idle"
    assert monitor._scan_msg_timer.isActive()

    monitor._toggle_zkill_scan()
    assert monitor._tw_target == "SCAN OFF"
    assert monitor._state == "idle"


def test_scan_flash_restores_current_state_text(monitor):
    # Le flash retombe tout seul : le texte d'état courant revient, pour ne
    # pas masquer ALL CLEAR/HOSTILE plus de ~1,5 s.
    monitor._toggle_zkill_scan()
    monitor._restore_after_scan_flash()
    assert monitor._tw_target == "IDLE"


def test_scan_flash_restore_never_overwrites_newer_message(monitor):
    # Si un message plus récent (ERR n/3, HOSTILE…) a remplacé le flash, la
    # retombée du timer ne doit pas l'écraser.
    monitor._toggle_zkill_scan()
    monitor._typewriter_status("ERR 1/3", monitor.theme['YELLOW'])
    monitor._restore_after_scan_flash()
    assert monitor._tw_target == "ERR 1/3"


def test_scan_indicator_title_is_visibly_marked(monitor):
    # Indication PERSISTANTE : pendant le scan, le glyphe passe à ◉ et le
    # titre prend la couleur d'accent jaune — visible d'un coup d'œil même
    # après la retombée du flash.
    monitor._toggle_zkill_scan()
    assert monitor.title_lbl.text().startswith("◉")
    assert monitor.theme['YELLOW'] in monitor.title_lbl.styleSheet()

    monitor._toggle_zkill_scan()
    assert monitor.title_lbl.text().startswith("◈")
    assert monitor.theme['YELLOW'] not in monitor.title_lbl.styleSheet()


def test_unpause_before_any_successful_sample_cannot_display_clear(
    monitor, monkeypatch,
):
    # Un compteur initialisé à zéro n'est pas une preuve que l'écran est sûr :
    # ALL CLEAR exige au moins un échantillon de détection réussi.
    import tm.monitor as monitor_mod

    monitor._bbox = {"top": 0, "left": 0, "width": 16, "height": 40}
    monkeypatch.setattr(
        monitor_mod,
        "detect_threats",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("capture failed")),
    )
    monitor._start_monitoring()
    monitor._toggle_pause()

    monitor._toggle_pause()

    assert monitor._state == "checking"
    assert monitor._tw_target == "ERR 1/3"


def test_clean_paused_polls_reset_consecutive_error_counter(
    monitor, monkeypatch,
):
    monitor._start_monitoring()
    monitor._toggle_pause()
    _arm_detection(
        monitor,
        monkeypatch,
        [
            RuntimeError("e1"), (0, 0),
            RuntimeError("e2"), (0, 0),
            RuntimeError("e3"),
        ],
    )

    for _ in range(5):
        monitor._update_monitor()

    assert monitor._monitoring
    assert monitor._state == "paused"
    assert monitor._monitor_errors == 1


def test_clean_poll_restores_state_after_transient_error(monitor, monkeypatch):
    monitor._start_monitoring()
    _arm_detection(monitor, monkeypatch, [RuntimeError("temporary"), (0, 0)])

    monitor._update_monitor()
    assert monitor._tw_target == "ERR 1/3"
    monitor._update_monitor()

    assert monitor._monitor_errors == 0
    assert monitor._state == "clear"
    assert monitor._tw_target == "ALL CLEAR"


def test_stop_monitoring_stops_current_sound(
    monitor, audio_calls, monkeypatch,
):
    monitor._start_monitoring()
    _arm_detection(monitor, monkeypatch, [(1, 0)])
    monitor._update_monitor()
    assert monitor.is_alerting
    before = audio_calls["stop"]

    monitor._stop_monitoring()

    assert audio_calls["stop"] == before + 1


def test_leaving_fake_mode_rearms_and_polls_real_detection(
    monitor, monkeypatch,
):
    import tm.monitor as monitor_mod

    monitor._start_monitoring()
    monitor.fake_threat_mode = True
    monitor.last_threat_count = 4
    calls = []

    def real_detection(*args, **kwargs):
        calls.append(True)
        return 1, 0

    monkeypatch.setattr(monitor_mod, "detect_threats", real_detection)
    monitor._toggle_fake_threat()

    assert not monitor.fake_threat_mode
    assert calls == [True]
    assert monitor.last_threat_count == 1
    assert monitor._state == "hostile"
    assert monitor.is_alerting


def test_theme_change_reapplies_hostile_brackets(monitor, monkeypatch):
    monitor._start_monitoring()
    _arm_detection(monitor, monkeypatch, [(1, 0)])
    monitor._update_monitor()

    monitor._apply_theme("ORE")

    assert monitor._state == "hostile"
    assert monitor._bracket_state == "hostile"
    assert monitor._bracket_color.name() == monitor.theme["RED"]


def test_relative_mirror_creation_starts_without_delayed_callback(
    monitor, monkeypatch,
):
    import tm.monitor as monitor_mod

    monitor._relative_bbox = {
        "offset_left": 0, "offset_top": 0, "width": 10, "height": 10,
    }
    monitor.app_config["mirror_bbox"] = {
        "top": 0, "left": 0, "width": 20, "height": 20,
    }
    started = []
    scheduled = []

    def create(_bbox):
        monitor.mirror_window = object()

    monkeypatch.setattr(monitor, "_create_mirror", create)
    monkeypatch.setattr(monitor, "_start_monitoring", lambda: started.append(True))
    monkeypatch.setattr(
        monitor_mod.QTimer,
        "singleShot",
        lambda delay, callback: scheduled.append((delay, callback)),
    )

    monitor._toggle_monitoring()

    assert started == [True]
    assert scheduled == []


def test_required_mirror_close_notifies_only_once(
    monitor, audio_calls,
):
    monitor._monitoring = True
    monitor._relative_bbox = {
        "offset_left": 0, "offset_top": 0, "width": 10, "height": 10,
    }
    before = audio_calls["notify"]

    monitor._on_mirror_closed()
    monitor._on_mirror_closed()

    assert monitor._state == "nomirror"
    assert audio_calls["notify"] == before + 1


def test_native_close_hides_only_when_tray_is_usable(monitor, monkeypatch):
    hidden = []
    closed = []
    monkeypatch.setattr(monitor, "hide", lambda: hidden.append(True))
    monkeypatch.setattr(monitor, "_real_close", lambda: closed.append(True))

    monitor._tray_usable = True
    tray_event = QCloseEvent()
    monitor.closeEvent(tray_event)
    assert hidden == [True]
    assert closed == []
    assert not tray_event.isAccepted()

    monitor._tray_usable = False
    quit_event = QCloseEvent()
    monitor.closeEvent(quit_event)
    assert closed == [True]
    assert quit_event.isAccepted()


def test_double_click_behavior_is_limited_to_titlebar(monitor):
    def double_click():
        return QMouseEvent(
            QEvent.Type.MouseButtonDblClick,
            QPointF(5, 5),
            QPointF(5, 5),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )

    assert not monitor._collapsed
    consumed = monitor.eventFilter(monitor.body_frame, double_click())
    assert not consumed
    assert not monitor._collapsed

    consumed = monitor.eventFilter(monitor.titlebar, double_click())
    assert consumed
    assert monitor._collapsed


# ── Coordinate-space version 2 ───────────────────────────────────────────────

def test_save_detection_area_stores_mirror_offsets_in_qt_logical_units(
    monitor, monkeypatch,
):
    import tm.monitor as monitor_mod

    class Mirror:
        bbox = {"top": 0, "left": 0, "width": 300, "height": 300}

        @staticmethod
        def get_content_position():
            return {"x": 90, "y": 140}

        @staticmethod
        def get_content_qt_rect():
            return QRect(90, 140, 100, 100)

    native_bbox = {"top": 300, "left": 200, "width": 60, "height": 80}
    monkeypatch.setattr(
        monitor_mod,
        "native_bbox_to_qt_rect",
        lambda bbox, mappings=None: QRect(100, 150, 30, 40),
        raising=False,
    )
    monitor.mirror_window = Mirror()

    monitor._save_detection_area(native_bbox)

    assert monitor._bbox == native_bbox
    assert monitor._relative_bbox == {
        "offset_left": 10,
        "offset_top": 10,
        "width": 30,
        "height": 40,
    }
    assert monitor.app_config["coordinate_space_version"] == 2


def test_resolve_relative_detection_bbox_converts_current_qt_rect_to_native(
    monitor, monkeypatch,
):
    import tm.monitor as monitor_mod

    class Mirror:
        @staticmethod
        def get_content_position():
            return {"x": 90, "y": 140}

        @staticmethod
        def get_content_qt_rect():
            return QRect(90, 140, 100, 100)

    converted = []
    expected = {"top": 300, "left": 200, "width": 60, "height": 80}

    def convert(rect, mappings=None):
        converted.append(QRect(rect))
        return expected

    monkeypatch.setattr(
        monitor_mod, "qt_rect_to_native_bbox", convert, raising=False
    )
    monitor.mirror_window = Mirror()
    monitor._relative_bbox = {
        "offset_left": 10,
        "offset_top": 10,
        "width": 30,
        "height": 40,
    }

    assert monitor._resolve_detection_bbox() == expected
    assert converted == [QRect(100, 150, 30, 40)]


def test_legacy_scaled_capture_config_is_invalidated_safely(monitor, monkeypatch):
    import tm.monitor as monitor_mod

    monitor.app_config.update({
        "detection_bbox": {"top": 1, "left": 2, "width": 16, "height": 40},
        "relative_bbox": {
            "offset_left": 1, "offset_top": 2, "width": 16, "height": 40,
        },
        "mirror_bbox": {"top": 3, "left": 4, "width": 20, "height": 50},
    })
    monitor.app_config.pop("coordinate_space_version", None)
    monitor._bbox = monitor.app_config["detection_bbox"]
    monitor._relative_bbox = monitor.app_config["relative_bbox"]
    mapping = type("Mapping", (), {"scale_x": 1.5, "scale_y": 1.5})()
    monkeypatch.setattr(
        monitor_mod, "build_screen_mappings", lambda: [mapping], raising=False
    )

    monitor._validate_coordinate_config()

    assert monitor._bbox is None
    assert monitor._relative_bbox is None
    assert "detection_bbox" not in monitor.app_config
    assert "relative_bbox" not in monitor.app_config
    assert "mirror_bbox" not in monitor.app_config
    assert monitor.app_config["coordinate_space_version"] == 2
    assert monitor._tw_target == "REQ F2/F3"


def test_legacy_dpr_one_capture_config_is_preserved(monitor, monkeypatch):
    import tm.monitor as monitor_mod

    bbox = {"top": 1, "left": 2, "width": 16, "height": 40}
    monitor.app_config["detection_bbox"] = bbox
    monitor.app_config.pop("coordinate_space_version", None)
    monitor._bbox = bbox
    monitor._save_config()
    mapping = type("Mapping", (), {"scale_x": 1.0, "scale_y": 1.0})()
    monkeypatch.setattr(
        monitor_mod, "build_screen_mappings", lambda: [mapping], raising=False
    )

    monitor._validate_coordinate_config()

    assert monitor._bbox == bbox
    assert monitor.app_config["detection_bbox"] == bbox
    assert monitor.app_config["coordinate_space_version"] == 2


def test_absolute_selection_marks_coordinate_space_version_two(monitor):
    bbox = {"top": 10, "left": 20, "width": 16, "height": 40}
    monitor.mirror_window = None

    monitor._save_detection_area(bbox)

    assert monitor.app_config["coordinate_space_version"] == 2


def test_unmappable_legacy_capture_is_removed_from_persisted_config(
    monitor, tmp_config, monkeypatch,
):
    import json
    import tm.monitor as monitor_mod

    bbox = {"top": 1, "left": 2, "width": 16, "height": 40}
    monitor.app_config["detection_bbox"] = bbox
    monitor.app_config.pop("coordinate_space_version", None)
    monitor._bbox = bbox
    monitor._save_config()
    monkeypatch.setattr(
        monitor_mod,
        "build_screen_mappings",
        lambda: (_ for _ in ()).throw(RuntimeError("ambiguous screens")),
    )

    monitor._validate_coordinate_config()

    persisted = json.loads(tmp_config.read_text(encoding="utf-8"))
    assert monitor._bbox is None
    assert "detection_bbox" not in persisted


@pytest.mark.parametrize("action", ["_reselect_area", "_open_mirror"])
def test_new_capture_selection_invalidates_cached_screen_mapping(
    monitor, monkeypatch, action,
):
    # F2/F3 doivent reconstruire le mapping : l'échelle DPI ou la disposition
    # des écrans a pu changer depuis sa mise en cache.
    import tm.monitor as monitor_mod

    created = []
    monitor._screen_mappings = ["stale mapping"]
    monitor._relative_bbox = None
    monkeypatch.setattr(
        monitor_mod,
        "AreaSelector",
        lambda callback, previous=None: created.append((callback, previous)),
    )

    getattr(monitor, action)()

    assert created
    assert monitor._screen_mappings is None


def test_failed_mirror_creation_degrades_visibly_without_crash(
    monitor, monkeypatch,
):
    # Une mirror_bbox périmée (écran débranché) ne doit jamais faire
    # planter l'app : on retire la bbox et on demande une resélection.
    import tm.monitor as monitor_mod

    def broken_mirror(*a, **k):
        raise ValueError("bbox is not on any connected screen")

    monkeypatch.setattr(monitor_mod, "MirrorWindow", broken_mirror)
    stale = {"top": 0, "left": 5000, "width": 20, "height": 20}
    monitor.app_config["mirror_bbox"] = stale

    monitor._create_mirror(stale)   # ne doit pas lever

    assert monitor.mirror_window is None
    assert "mirror_bbox" not in monitor.app_config
    assert monitor._tw_target == "REQ F2"


@pytest.mark.parametrize("action", ["_reselect_area", "_open_mirror"])
def test_selector_open_failure_shows_error_instead_of_crash(
    monitor, monkeypatch, action,
):
    # build_screen_mappings peut lever (écrans ambigus) pendant F2/F3 :
    # l'erreur doit rester visible et neutre, jamais un abort Qt.
    import tm.monitor as monitor_mod

    def broken_selector(*a, **k):
        raise RuntimeError("no unambiguous mapping")

    monkeypatch.setattr(monitor_mod, "AreaSelector", broken_selector)

    getattr(monitor, action)()   # ne doit pas lever

    assert monitor._tw_target == "SCREEN ERR"


def test_screens_changed_slot_drops_cached_mapping(monitor):
    # Le cache de mapping doit être invalidé quand la topologie change.
    monitor._screen_mappings = ["stale mapping"]

    monitor._on_screens_changed()

    assert monitor._screen_mappings is None


def test_stale_mirror_frame_is_never_a_successful_sample(monitor, monkeypatch):
    # Un miroir dont la capture SOURCE échoue continue d'afficher son dernier
    # frame figé pendant ~10 s ; l'analyser produirait un faux ALL CLEAR.
    # Tant que le miroir n'est pas « sain », le poll reste en CHECKING sans
    # jamais appeler le détecteur.
    from types import SimpleNamespace

    import tm.monitor as monitor_mod

    monitor._start_monitoring()
    assert monitor._state == "clear"

    monitor._relative_bbox = {
        "offset_left": 0, "offset_top": 0, "width": 10, "height": 10,
    }
    monitor._bbox = None
    monitor.mirror_window = SimpleNamespace(capture_healthy=False)
    monkeypatch.setattr(
        monitor_mod, "detect_threats",
        lambda *a, **k: pytest.fail("stale mirror pixels must not be analyzed"),
    )

    monitor._update_monitor()

    assert monitor._state == "checking"


def test_mirror_replacement_with_new_bbox_drops_relative_offsets(
    monitor, monkeypatch,
):
    # Les offsets relatifs n'ont de sens QUE pour la mirror_bbox contre
    # laquelle ils ont été calculés : après un F2 vers une AUTRE région, ils
    # pointeraient sur du contenu arbitraire — capture valide mais fausse.
    from PyQt6.QtCore import QObject, pyqtSignal

    import tm.monitor as monitor_mod

    class StubMirror(QObject):
        position_changed = pyqtSignal(dict)
        closed = pyqtSignal()

        def __init__(self, bbox, theme, saved_position=None):
            super().__init__()
            self.bbox = bbox

        def x(self):
            return 0

        def y(self):
            return 0

        def close(self):
            self.closed.emit()

    monkeypatch.setattr(monitor_mod, "MirrorWindow", StubMirror)

    old = {"top": 0, "left": 0, "width": 200, "height": 100}
    new = {"top": 0, "left": 300, "width": 200, "height": 100}
    rel = {"offset_left": 4, "offset_top": 6, "width": 12, "height": 40}
    monitor.app_config["mirror_bbox"] = dict(old)
    monitor._relative_bbox = dict(rel)
    monitor._bbox = {"top": 6, "left": 4, "width": 12, "height": 40}

    monitor._create_mirror(dict(old))      # même région : offsets conservés
    assert monitor._relative_bbox == rel

    monitor._create_mirror(dict(new))      # région différente : purge + REQ F3
    assert monitor._relative_bbox is None
    assert monitor._bbox is None
    assert "detection_bbox" not in monitor.app_config
    assert not monitor.app_config.get("relative_bbox")
    assert monitor._tw_target == "REQ F3"


def test_display_change_stops_monitoring_visibly(monitor, audio_calls):
    # Après un réarrangement d'écrans, les anciennes coordonnées peuvent rester
    # capturables mais viser un AUTRE contenu → faux ALL CLEAR silencieux.
    # On arrête donc le monitoring de façon visible (toast + statut).
    monitor._start_monitoring()
    before = audio_calls["notify"]

    monitor._on_screens_changed()

    assert not monitor._monitoring
    assert monitor._tw_target == "SCREEN CHG"
    assert audio_calls["notify"] == before + 1

    monitor._on_screens_changed()          # au repos : aucune re-notification
    assert audio_calls["notify"] == before + 1


def test_titlebar_buttons_ignore_non_left_clicks(monitor):
    # Un clic droit sur ▶ coupait la protection et un clic droit sur × quittait
    # l'app, au lieu de laisser le menu contextuel documenté s'ouvrir.
    def press(button):
        return QMouseEvent(
            QEvent.Type.MouseButtonPress, QPointF(5, 5), QPointF(5, 5),
            button, button, Qt.KeyboardModifier.NoModifier,
        )

    monitor._start_monitoring()
    monitor.play_btn.mousePressEvent(press(Qt.MouseButton.RightButton))
    assert monitor._monitoring             # toujours protégé

    monitor.close_btn.mousePressEvent(press(Qt.MouseButton.RightButton))
    assert not monitor._quitting           # l'app ne quitte pas

    monitor.play_btn.mousePressEvent(press(Qt.MouseButton.LeftButton))
    assert not monitor._monitoring


def test_screen_geometry_change_signal_invalidates_cached_mapping(
    monitor, qapp,
):
    # Un changement de résolution/échelle DPI émet geometryChanged : le
    # mapping mémorisé au démarrage ne doit pas survivre à ce signal.
    monitor._screen_mappings = ["stale mapping"]

    qapp.screens()[0].geometryChanged.emit(QRect(0, 0, 640, 480))

    assert monitor._screen_mappings is None

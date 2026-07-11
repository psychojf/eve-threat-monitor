# -*- coding: utf-8 -*-
# Tests de la couche audio/notifications multiplateforme.
#
# Aucun son ni process réel : winsound, lecteurs externes, plyer et notifieurs
# natifs sont tous simulés. Les chemins macOS/Linux sont testés en injectant
# platform/which — la suite couvre donc les trois OS depuis n'importe lequel.
import json

import pytest

from tm import audio


class _FakeWinsound:
    # Reproduit la surface de winsound utilisée par tm.audio (constantes + PlaySound).
    SND_FILENAME = 0x20000
    SND_ASYNC = 0x0001

    def __init__(self):
        self.calls = []

    def PlaySound(self, sound, flags):
        self.calls.append((sound, flags))


class _FakeProc:
    # Simule un subprocess.Popen : vivant tant que _returncode est None.
    def __init__(self, cmd):
        self.cmd = cmd
        self.terminated = False
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = -15


class _FakeNotifier:
    # Simule plyer.notification : échoue à la demande (backend natif absent).
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def notify(self, **kwargs):
        if self.fail:
            raise RuntimeError("no usable backend")
        self.calls.append(kwargs)


@pytest.fixture(autouse=True)
def _reset_audio_state(monkeypatch):
    # Les globals mutables du module ne doivent pas fuir d'un test à l'autre.
    monkeypatch.setattr(audio, "_player_proc", None)
    monkeypatch.setattr(audio, "_plyer_broken", False)


def _spawn_recorder(monkeypatch, procs):
    # Remplace _spawn : enregistre la commande et retourne un _FakeProc traçable.
    def fake_spawn(cmd):
        proc = _FakeProc(cmd)
        procs.append(proc)
        return proc
    monkeypatch.setattr(audio, "_spawn", fake_spawn)


# ── Sélection des backends externes ──────────────────────────────────────────

def test_find_player_windows_is_none():
    # Sur Windows, winsound fait le travail — aucun lecteur externe cherché.
    assert audio._find_player("win32", which=lambda name: "/bin/" + name) is None


def test_find_player_macos_uses_afplay():
    assert audio._find_player("darwin", which=lambda name: "/usr/bin/afplay") == ["afplay"]


def test_find_player_linux_prefers_paplay():
    # paplay (PulseAudio/PipeWire) passe avant aplay quand les deux existent.
    assert audio._find_player("linux", which=lambda name: "/usr/bin/" + name) == ["paplay"]


def test_find_player_linux_falls_back_to_aplay():
    which = lambda name: "/usr/bin/aplay" if name == "aplay" else None
    assert audio._find_player("linux", which=which) == ["aplay", "-q"]


def test_find_player_nothing_available():
    assert audio._find_player("linux", which=lambda name: None) is None
    assert audio._find_player("darwin", which=lambda name: None) is None


def test_find_notifier_by_platform():
    # osascript sur macOS, notify-send sur Linux, rien sur Windows (plyer seul).
    assert audio._find_notifier("win32", which=lambda name: "/bin/" + name) is None
    assert audio._find_notifier("darwin", which=lambda name: "/usr/bin/" + name) == ["osascript"]
    assert audio._find_notifier("linux", which=lambda name: "/usr/bin/" + name) == ["notify-send"]
    assert audio._find_notifier("darwin", which=lambda name: None) is None


# ── play_alert / stop_sound ──────────────────────────────────────────────────

def test_play_alert_winsound(monkeypatch):
    fake = _FakeWinsound()
    monkeypatch.setattr(audio, "_HAS_WINSOUND", True)
    monkeypatch.setattr(audio, "_winsound", fake, raising=False)
    audio.play_alert("alert.wav", file_exists=True)
    assert fake.calls == [("alert.wav", fake.SND_FILENAME | fake.SND_ASYNC)]


def test_play_alert_missing_file_is_noop(monkeypatch):
    fake = _FakeWinsound()
    monkeypatch.setattr(audio, "_HAS_WINSOUND", True)
    monkeypatch.setattr(audio, "_winsound", fake, raising=False)
    audio.play_alert("alert.wav", file_exists=False)
    assert fake.calls == []

    procs = []
    monkeypatch.setattr(audio, "_HAS_WINSOUND", False)
    monkeypatch.setattr(audio, "_PLAYER_CMD", ["afplay"])
    _spawn_recorder(monkeypatch, procs)
    audio.play_alert("alert.wav", file_exists=False)
    assert procs == []


def test_play_alert_external_player(monkeypatch):
    procs = []
    monkeypatch.setattr(audio, "_HAS_WINSOUND", False)
    monkeypatch.setattr(audio, "_PLAYER_CMD", ["afplay"])
    _spawn_recorder(monkeypatch, procs)
    audio.play_alert("alert.wav", file_exists=True)
    assert [p.cmd for p in procs] == [["afplay", "alert.wav"]]
    assert audio._player_proc is procs[0]


def test_play_alert_replaces_running_sound(monkeypatch):
    # Sémantique SND_ASYNC de winsound : un nouveau son remplace celui en cours.
    procs = []
    monkeypatch.setattr(audio, "_HAS_WINSOUND", False)
    monkeypatch.setattr(audio, "_PLAYER_CMD", ["paplay"])
    _spawn_recorder(monkeypatch, procs)
    audio.play_alert("alert.wav", file_exists=True)
    audio.play_alert("alert.wav", file_exists=True)
    assert procs[0].terminated
    assert audio._player_proc is procs[1]


def test_play_alert_without_any_backend(monkeypatch):
    monkeypatch.setattr(audio, "_HAS_WINSOUND", False)
    monkeypatch.setattr(audio, "_PLAYER_CMD", None)
    audio.play_alert("alert.wav", file_exists=True)   # ne doit pas lever


def test_play_alert_spawn_failure_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(audio, "_HAS_WINSOUND", False)
    monkeypatch.setattr(audio, "_PLAYER_CMD", ["afplay"])

    def boom(cmd):
        raise OSError("exec failed")
    monkeypatch.setattr(audio, "_spawn", boom)

    with caplog.at_level("ERROR", logger="tm.audio"):
        audio.play_alert("alert.wav", file_exists=True)
    assert audio._player_proc is None
    assert any("play_alert" in rec.message for rec in caplog.records)


def test_stop_sound_winsound(monkeypatch):
    fake = _FakeWinsound()
    monkeypatch.setattr(audio, "_HAS_WINSOUND", True)
    monkeypatch.setattr(audio, "_winsound", fake, raising=False)
    audio.stop_sound()
    assert fake.calls == [(None, fake.SND_ASYNC)]


def test_stop_sound_terminates_external_player(monkeypatch):
    procs = []
    monkeypatch.setattr(audio, "_HAS_WINSOUND", False)
    monkeypatch.setattr(audio, "_PLAYER_CMD", ["afplay"])
    _spawn_recorder(monkeypatch, procs)
    audio.play_alert("alert.wav", file_exists=True)
    audio.stop_sound()
    assert procs[0].terminated
    assert audio._player_proc is None
    audio.stop_sound()   # idempotent : plus rien à arrêter


def test_stop_sound_skips_finished_player(monkeypatch):
    # Un lecteur déjà terminé (son fini naturellement) n'est pas re-signalé.
    procs = []
    monkeypatch.setattr(audio, "_HAS_WINSOUND", False)
    monkeypatch.setattr(audio, "_PLAYER_CMD", ["afplay"])
    _spawn_recorder(monkeypatch, procs)
    audio.play_alert("alert.wav", file_exists=True)
    procs[0]._returncode = 0
    audio.stop_sound()
    assert not procs[0].terminated


# ── send_notification ────────────────────────────────────────────────────────

def test_notification_plyer_ok(monkeypatch):
    notifier = _FakeNotifier()
    procs = []
    monkeypatch.setattr(audio, "_HAS_NOTIFICATION", True)
    monkeypatch.setattr(audio, "_notification", notifier, raising=False)
    monkeypatch.setattr(audio, "_NOTIFIER_CMD", ["notify-send"])
    _spawn_recorder(monkeypatch, procs)
    audio.send_notification("Title", "Message", timeout=2)
    assert notifier.calls == [{"title": "Title", "message": "Message", "timeout": 2}]
    assert procs == []   # plyer a suffi, pas de repli


def test_notification_plyer_failure_falls_back_and_sticks(monkeypatch):
    # plyer casse (pyobjus/dbus absent) : repli natif, puis plyer plus jamais retenté.
    notifier = _FakeNotifier(fail=True)
    procs = []
    monkeypatch.setattr(audio, "_HAS_NOTIFICATION", True)
    monkeypatch.setattr(audio, "_notification", notifier, raising=False)
    monkeypatch.setattr(audio, "_NOTIFIER_CMD", ["osascript"])
    _spawn_recorder(monkeypatch, procs)

    audio.send_notification("Title", "Message")
    audio.send_notification("Title", "Message")
    assert len(procs) == 2
    assert audio._plyer_broken is True
    # plyer n'a été sollicité qu'une seule fois (le premier échec)
    assert notifier.calls == []


def test_notification_plyer_failure_without_fallback_keeps_retrying(monkeypatch, caplog):
    # Comportement Windows historique : pas de repli natif → on logge et on
    # retentera plyer au prochain appel.
    notifier = _FakeNotifier(fail=True)
    monkeypatch.setattr(audio, "_HAS_NOTIFICATION", True)
    monkeypatch.setattr(audio, "_notification", notifier, raising=False)
    monkeypatch.setattr(audio, "_NOTIFIER_CMD", None)
    with caplog.at_level("ERROR", logger="tm.audio"):
        audio.send_notification("Title", "Message")
    assert audio._plyer_broken is False
    assert any("send_notification" in rec.message for rec in caplog.records)


def test_notification_native_osascript_escapes_quotes(monkeypatch):
    # json.dumps == littéral AppleScript : un « " » ne peut pas injecter de script.
    procs = []
    monkeypatch.setattr(audio, "_HAS_NOTIFICATION", False)
    monkeypatch.setattr(audio, "_NOTIFIER_CMD", ["osascript"])
    _spawn_recorder(monkeypatch, procs)
    audio.send_notification('Ti"tle', 'Mes"sage')
    assert len(procs) == 1
    cmd = procs[0].cmd
    assert cmd[:2] == ["osascript", "-e"]
    assert json.dumps('Mes"sage') in cmd[2]
    assert json.dumps('Ti"tle') in cmd[2]


def test_notification_native_notify_send_args(monkeypatch):
    procs = []
    monkeypatch.setattr(audio, "_HAS_NOTIFICATION", False)
    monkeypatch.setattr(audio, "_NOTIFIER_CMD", ["notify-send"])
    _spawn_recorder(monkeypatch, procs)
    audio.send_notification("Title", "Message", timeout=3)
    assert procs[0].cmd == ["notify-send", "-t", "3000", "Title", "Message"]


def test_notification_without_any_backend(monkeypatch):
    monkeypatch.setattr(audio, "_HAS_NOTIFICATION", False)
    monkeypatch.setattr(audio, "_NOTIFIER_CMD", None)
    audio.send_notification("Title", "Message")   # ne doit pas lever

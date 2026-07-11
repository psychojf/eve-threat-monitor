# -*- coding: utf-8 -*-
# Fixtures partagées.
#
# QT_QPA_PLATFORM=offscreen est posé AVANT tout import PyQt6 : les tests
# d'intégration widget tournent sans affichage (CI, session SSH, etc.).
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture(scope="session")
def qapp():
    # QApplication unique pour toute la session de test (offscreen).
    from PyQt6.QtCore import QCoreApplication, QEvent
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    # Dernier filet : collecte les cycles restants PUIS purge les deleteLater,
    # pendant que QApplication existe encore — jamais au gc de fin de process.
    import gc
    gc.collect()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture(autouse=True)
def flush_deferred_qt_deletes():
    # Purge les objets deleteLater après CHAQUE test : les laisser
    # s'accumuler jusqu'au teardown de QApplication provoquait des
    # destructions tardives imprévisibles entre tests.
    yield
    import gc

    from PyQt6.QtCore import QCoreApplication, QEvent
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
    # Collecte immédiate des cycles PyQt (widget ↔ signaux ↔ méthodes liées).
    # Les laisser au gc massif de fin de session faisait détruire des dizaines
    # de wrappers Qt dans un ordre arbitraire, pendant pytest_unconfigure —
    # access violation intermittente (0xC0000005) sous -X dev, sensible au
    # simple NOMBRE de tests. Ici, chaque cycle meurt pendant que Qt est
    # pleinement vivant, immédiatement après son test.
    gc.collect()
    if app is not None:
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    # Redirige CONFIG_FILE vers un fichier temporaire : les tests ne lisent ni
    # n'écrasent jamais le vrai threat_config.json du projet.
    import tm.config as config
    cfg_path = tmp_path / "threat_config.json"
    monkeypatch.setattr(config, "CONFIG_FILE", str(cfg_path))
    return cfg_path


@pytest.fixture
def audio_calls(monkeypatch):
    # Remplace la couche audio par des enregistreurs d'appels.
    # Aucun son ni toast pendant les tests ; on vérifie les intentions.
    import tm.audio as audio
    calls = {"play": 0, "stop": 0, "notify": 0}
    monkeypatch.setattr(audio, "play_alert", lambda *a, **k: calls.__setitem__("play", calls["play"] + 1))
    monkeypatch.setattr(audio, "stop_sound", lambda *a, **k: calls.__setitem__("stop", calls["stop"] + 1))
    monkeypatch.setattr(audio, "send_notification", lambda *a, **k: calls.__setitem__("notify", calls["notify"] + 1))
    return calls


class FakeMSS:
    # Remplace mss.MSS — grab() renvoie une image noire, jamais d'accès écran.

    monitors = [
        {"left": 0, "top": 0, "width": 800, "height": 800},
        {"left": 0, "top": 0, "width": 800, "height": 800},
    ]

    def __init__(self, *a, **k):
        import numpy as np
        self._img = np.zeros((40, 12, 4), dtype=np.uint8)

    def grab(self, bbox):
        return self._img

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


@pytest.fixture
def monitor(qapp, tmp_config, audio_calls, monkeypatch):
    # ThreatMonitor headless : config temporaire, audio enregistré, mss factice.
    import tm.monitor as monitor_mod
    monkeypatch.setattr(monitor_mod.mss, "MSS", FakeMSS)

    w = monitor_mod.ThreatMonitor()
    w._bbox = {"top": 0, "left": 0, "width": 16, "height": 40}
    yield w

    # Teardown : stoppe tous les timers pour ne pas polluer les tests suivants
    for t in (w.monitor_timer, w._alert_timer, w._tw_timer,
              w._bracket_timer, w._opacity_save_timer, w._scan_msg_timer):
        t.stop()
    w.hide()
    w.deleteLater()

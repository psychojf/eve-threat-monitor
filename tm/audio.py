# -*- coding: utf-8 -*-
# Couche audio et notifications.
#
# Encapsule les backends par plateforme pour que le reste de l'app n'ait
# jamais à gérer les imports spécifiques ni les try/except associés :
#   son   — winsound (Windows), afplay (macOS), paplay/aplay (Linux)
#   toast — plyer, avec repli natif osascript (macOS) / notify-send (Linux)
# Tout est best-effort : une alerte qui échoue se logge, elle ne fait jamais
# planter la surveillance.

import json
import logging
import shutil
import subprocess
import sys

# ── Imports optionnels selon la plateforme ───────────────────────────────────
try:
    import winsound as _winsound
    _HAS_WINSOUND = True
except ImportError:
    _HAS_WINSOUND = False

try:
    from plyer import notification as _notification
    _HAS_NOTIFICATION = True
except ImportError:
    _HAS_NOTIFICATION = False

log = logging.getLogger(__name__)


# ── Résolution des commandes externes (macOS / Linux) ────────────────────────

def _find_player(platform: str = sys.platform, which=shutil.which):
    # Lecteur audio système hors Windows : afplay est livré avec macOS ;
    # paplay couvre PulseAudio/PipeWire (desktops actuels), aplay (ALSA) en
    # secours. Sur Windows, winsound suffit — pas de process externe.
    # platform/which sont injectables pour tester les trois OS depuis n'importe lequel.
    if platform == "darwin":
        candidates = (["afplay"],)
    elif platform.startswith("linux"):
        candidates = (["paplay"], ["aplay", "-q"])
    else:
        return None
    for cmd in candidates:
        if which(cmd[0]):
            return cmd
    return None


def _find_notifier(platform: str = sys.platform, which=shutil.which):
    # Commande de notification native, en repli quand plyer échoue (pyobjus
    # absent sur macOS, dbus absent sur Linux). Windows garde plyer seul.
    if platform == "darwin":
        candidates = (["osascript"],)
    elif platform.startswith("linux"):
        candidates = (["notify-send"],)
    else:
        return None
    for cmd in candidates:
        if which(cmd[0]):
            return cmd
    return None


# Résolus une seule fois à l'import : shutil.which à chaque tick d'alerte
# (répétition toutes les 5 s) serait du gaspillage.
_PLAYER_CMD = _find_player()
_NOTIFIER_CMD = _find_notifier()

_player_proc = None     # Popen du son en cours (backends externes uniquement)
_plyer_broken = False   # plyer a échoué ET un repli natif existe : ne plus retenter


def _spawn(cmd):
    # Lance une commande détachée, sorties ignorées. Point unique : les tests
    # le mockent, et le thread Qt n'attend jamais un process externe.
    return subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def _stop_player_proc() -> None:
    # Termine le lecteur externe encore actif, sans jamais bloquer le thread
    # Qt (pas de wait) : le zombie éventuel est moissonné par subprocess au
    # prochain Popen. poll() d'abord — un son fini naturellement n'est pas
    # re-signalé.
    global _player_proc
    proc, _player_proc = _player_proc, None
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception as exc:
        log.error("stop_sound: %s", exc)


# ── API publique ─────────────────────────────────────────────────────────────

def play_alert(sound_file: str, file_exists: bool) -> None:
    # Joue le son d'alerte en asynchrone ; ne fait rien si aucun backend audio
    # n'est disponible ou si le fichier manque.
    if not file_exists:
        return
    if _HAS_WINSOUND:
        try:
            _winsound.PlaySound(sound_file, _winsound.SND_FILENAME | _winsound.SND_ASYNC)
        except Exception as exc:
            log.error("play_alert: %s", exc)
        return
    if _PLAYER_CMD is None:
        return
    global _player_proc
    try:
        # Même sémantique que SND_ASYNC : un nouveau son REMPLACE celui en
        # cours — deux alertes superposées seraient inintelligibles.
        _stop_player_proc()
        _player_proc = _spawn(_PLAYER_CMD + [sound_file])
    except Exception as exc:
        log.error("play_alert: %s", exc)


def stop_sound() -> None:
    # Arrête le son asynchrone en cours.
    if _HAS_WINSOUND:
        try:
            _winsound.PlaySound(None, _winsound.SND_ASYNC)
        except Exception as exc:
            log.error("stop_sound: %s", exc)
        return
    _stop_player_proc()


def _native_notification_cmd(title: str, message: str, timeout: int):
    # Construit la commande du notifieur natif. Pour osascript, json.dumps
    # produit exactement l'échappement des littéraux AppleScript (guillemets +
    # backslash) — un texte contenant « " » ne peut donc pas injecter de script.
    if _NOTIFIER_CMD[0] == "osascript":
        script = (
            f"display notification {json.dumps(message)} "
            f"with title {json.dumps(title)}"
        )
        return _NOTIFIER_CMD + ["-e", script]
    # notify-send : timeout en millisecondes, plancher 1 s comme plyer.
    return _NOTIFIER_CMD + ["-t", str(max(1, timeout) * 1000), title, message]


def send_notification(title: str, message: str, timeout: int = 1) -> None:
    # Envoie une notification toast bureau (best-effort) : plyer d'abord,
    # puis le notifieur natif si plyer est cassé (dépendance backend absente).
    global _plyer_broken
    if _HAS_NOTIFICATION and not _plyer_broken:
        try:
            _notification.notify(title=title, message=message, timeout=timeout)
            return
        except Exception as exc:
            if _NOTIFIER_CMD is None:
                # Comportement Windows historique : on logge et on retentera
                # plyer au prochain appel — aucun repli natif n'existe.
                log.error("send_notification: %s", exc)
                return
            # plyer ne remarchera pas tout seul (backend manquant) : bascule
            # définitive sur le natif, inutile de payer l'exception à chaque alerte.
            _plyer_broken = True
            log.warning("plyer indisponible (%s) — repli notifieur natif", exc)
    if _NOTIFIER_CMD is None:
        return
    try:
        _spawn(_native_notification_cmd(title, message, timeout))
    except Exception as exc:
        log.error("send_notification: %s", exc)

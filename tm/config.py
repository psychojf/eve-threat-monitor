# -*- coding: utf-8 -*-
# Gestion de la config — chemins, chargement/sauvegarde, valeurs par défaut.
# Centralise tout ce qui touche au filesystem pour garder les autres modules propres.
import os
import sys
import json
import math
import logging
import tempfile
from logging.handlers import RotatingFileHandler

# ── Résolution des chemins : exécutable PyInstaller ou mode dev ──
if getattr(sys, 'frozen', False):
    BUNDLE_DIR = sys._MEIPASS
    USER_DIR   = os.path.dirname(sys.executable)
else:
    # __file__ est tm/config.py — remonte d'un niveau pour atteindre TM/
    BUNDLE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    USER_DIR   = BUNDLE_DIR

SOUND_FILE  = os.path.join(BUNDLE_DIR, "alert_hostile.wav")
ICON_FILE   = os.path.join(BUNDLE_DIR, "threat_icon.ico")
CONFIG_FILE = os.path.join(USER_DIR,   "threat_config.json")
LOG_FILE    = os.path.join(USER_DIR,   "threat_monitor.log")

# Vérifié une seule fois au démarrage — évite os.path.exists dans les boucles chaudes
SOUND_FILE_EXISTS = os.path.exists(SOUND_FILE)

log = logging.getLogger(__name__)


def _finite_number(value: object) -> bool:
    # Vrai si value est un nombre JSON fini. bool est exclu explicitement car
    # en Python bool hérite d'int — sinon true/false passerait pour 1/0.
    # NaN/inf sont refusés : une fois clampés ils donneraient une opacité absurde.
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _integer(value: object) -> bool:
    # Vrai si value est un entier utilisable comme coordonnée (bool exclu,
    # même raison que _finite_number : bool est une sous-classe d'int).
    return isinstance(value, int) and not isinstance(value, bool)


def _point(value: object) -> dict | None:
    # Normalise une position {x, y} sauvegardée, ou rejette le point ENTIER :
    # une moitié de position valide ne sert à rien. Les valeurs négatives sont
    # permises (moniteur secondaire à gauche/au-dessus du principal).
    if not isinstance(value, dict):
        return None
    if not _integer(value.get("x")) or not _integer(value.get("y")):
        return None
    return {"x": value["x"], "y": value["y"]}


def _bbox(value: object, *, relative: bool = False) -> dict | None:
    # Normalise une bbox absolue (top/left) ou relative au miroir (offset_*).
    # Dimensions > 0 exigées ; offsets relatifs >= 0 car ils vivent DANS le
    # contenu du miroir. Une bbox invalide est jetée entière : une zone de
    # capture à moitié valide produirait des lectures fausses.
    if not isinstance(value, dict):
        return None

    position_keys = ("offset_left", "offset_top") if relative else ("top", "left")
    keys = position_keys + ("width", "height")
    if any(not _integer(value.get(key)) for key in keys):
        return None
    if value["width"] <= 0 or value["height"] <= 0:
        return None
    if relative and (value["offset_left"] < 0 or value["offset_top"] < 0):
        return None
    return {key: value[key] for key in keys}


def normalize_config(data: object) -> dict:
    # Ne conserve que les réglages individuellement valides et normalisés.
    #
    # Pourquoi champ par champ : un threat_config.json corrompu ou édité à la
    # main ne doit ni faire planter l'app ni injecter une valeur dangereuse
    # dans la capture. On jette le champ malade, on garde les autres — mieux
    # qu'un tout-ou-rien qui perdrait aussi le thème et les positions.
    if not isinstance(data, dict):
        return {}

    normalized: dict = {}

    if isinstance(data.get("theme"), str):
        normalized["theme"] = data["theme"]

    opacity = data.get("opacity")
    if _finite_number(opacity):
        normalized["opacity"] = float(max(0.2, min(1.0, opacity)))

    for key in ("win_geom", "mirror_position"):
        point = _point(data.get(key))
        if point is not None:
            normalized[key] = point

    for key in ("detection_bbox", "mirror_bbox"):
        bbox = _bbox(data.get(key))
        if bbox is not None:
            normalized[key] = bbox

    relative_bbox = _bbox(data.get("relative_bbox"), relative=True)
    if relative_bbox is not None:
        normalized["relative_bbox"] = relative_bbox

    coordinate_space_version = data.get("coordinate_space_version")
    if _integer(coordinate_space_version) and coordinate_space_version >= 1:
        normalized["coordinate_space_version"] = coordinate_space_version

    return normalized


def setup_logging() -> None:
    # Configure le logging applicatif : fichier (toujours) + stdout (mode dev).
    #
    # Dans le build PyInstaller windowed, sys.stdout est None et les print()
    # disparaissent. Un FileHandler garantit qu'on garde une trace des erreurs.
    # Ne lève jamais — si le fichier ne peut être ouvert, on continue sans.
    handlers: list[logging.Handler] = []
    try:
        # Rotation : évite que threat_monitor.log grossisse indéfiniment à côté
        # de l'exe (1 Mo × 3 fichiers max, soit ~3 Mo plafond).
        handlers.append(RotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8",
        ))
    except Exception:
        pass
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )


def load_config() -> dict:
    # Charge la config depuis le JSON ; retourne un dict vide en cas d'erreur.
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                normalized = normalize_config(data)
            # JSON valide mais pas un objet (fichier édité à la main) :
            # les appelants font .get() dessus dès le démarrage.
            if isinstance(data, dict):
                return normalized
            log.warning("config ignorée : JSON valide mais pas un objet (%s)",
                        type(data).__name__)
        except Exception as exc:
            log.warning("config load failed: %s", exc)
    return {}


def save_config(cfg: dict) -> None:
    # Sauvegarde la config en JSON de façon atomique ; logue l'erreur sans lever.
    #
    # Écrit dans un fichier temporaire du même dossier puis os.replace() —
    # atomique sur Windows pour un rename intra-volume. Un crash/kill/coupure
    # en plein écriture laisse donc l'ancien threat_config.json intact au lieu
    # d'un fichier tronqué qui effacerait silencieusement tous les réglages.
    try:
        dir_ = os.path.dirname(CONFIG_FILE) or "."
        fd, tmp = tempfile.mkstemp(dir=dir_, prefix=".threat_config_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
                f.flush()
            os.replace(tmp, CONFIG_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:
        log.error("config save failed: %s", exc)

# -*- coding: utf-8 -*-
# Logique zKillboard / ESI — SANS dépendance Qt.
#
# Résolution de nom, récupération des stats (avec cache), et calcul des valeurs
# affichables. Séparé de zkill_card.py (rendu QPainter) pour :
#   • être testable sans installer PyQt6 ;
#   • isoler l'accès réseau du rendu ;
#   • centraliser cache, backoff et gestion du rate-limit.
#
# fetch_pilot_stats prend des callbacks simples (on_ready / on_error) au lieu de
# signaux Qt : c'est zkill_card.py qui fait le pont vers le thread UI.
import time
import logging
import datetime
import threading
from email.utils import parsedate_to_datetime

import requests

ESI_URL   = "https://esi.evetech.net/latest/universe/ids/?datasource=tranquility"
ZKILL_URL = "https://zkillboard.com/api/stats/characterID/{}/"

CACHE_TTL      = 300    # succès zKill : 5 min
FAIL_CACHE_TTL = 60     # échec zKill : cache négatif 1 min (évite de marteler l'API)
NAME_CACHE_TTL = 3600   # nom → id : change très rarement
_MAX_CACHE     = 512    # plafond du cache stats (anti-croissance non bornée)
STALE_CACHE_TTL = 3600  # stale positif servi au plus 1 h lors d'une panne

# zKillboard ET ESI demandent un User-Agent descriptif identifiant l'app + un
# contact. Sans ça, requests envoie "python-requests/x.y" et peut être throttlé
# ou bloqué — cause la plus probable d'un futur "la carte zKill n'apparaît plus".
HEADERS = {"User-Agent": "EveThreatMonitor/1.0 (contact: jfgelinasg@gmail.com)"}

log = logging.getLogger(__name__)

# Une session par thread : requests.Session n'est pas garanti thread-safe et
# chaque lookup zKill tourne dans son propre thread worker. Le keep-alive reste
# effectif au sein d'un même lookup (ESI + zKill s'enchaînent dans le même thread).
_tls = threading.local()


def _get_session() -> requests.Session:
    # Retourne la session HTTP du thread courant (créée au premier appel).
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _tls.session = s
    return s

_cache: dict[int, tuple[float, dict]] = {}             # char_id -> (ts, data)
_name_cache: dict[str, tuple[float, int | None]] = {}  # name -> (ts, id|None)
_fail_cache: dict[int, tuple[float, str]] = {}         # id -> (expiry, code)
_name_fail_cache: dict[str, tuple[float, str]] = {}    # name -> (expiry, code)
_inflight: dict[int, threading.Event] = {}
_rate_limit_until = 0.0
_cache_lock = threading.Lock()


class LookupFailure(Exception):
    # Base des échecs ATTENDUS, affichables à l'utilisateur. Typer les échecs
    # permet de mapper chaque cause à un message précis (UNKNOWN PILOT ≠
    # NETWORK ERROR) au lieu d'un fourre-tout trompeur.
    pass


class PilotNotFound(LookupFailure):
    # ESI a répondu AVEC SUCCÈS qu'aucun personnage ne porte ce nom.
    # Distinct d'une panne réseau : ici la réponse est fiable.
    pass


class NoStats(LookupFailure):
    # zKill n'a renvoyé aucune statistique exploitable (payload d'erreur,
    # identité manquante). Ne veut PAS dire « pilote inoffensif ».
    pass


class NetworkFailure(LookupFailure):
    # Panne transitoire (requête, statut HTTP, JSON invalide). Affichée en
    # neutre : l'absence d'info ne doit jamais ressembler à un pilote sûr.
    pass


class RateLimited(LookupFailure):
    # zKillboard a répondu 429 sans données en cache à servir.
    def __init__(self, retry_after=None) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


_FAIL_NETWORK = "NETWORK"
_FAIL_NO_STATS = "NO_STATS"
_FAIL_RATE_LIMITED = "RATE_LIMITED"

# Au moins un de ces champs doit être PRÉSENT pour qu'un payload zKill soit
# considéré comme des statistiques (même à zéro) et non une simple identité.
_COMBAT_STAT_KEYS = (
    "shipsDestroyed", "shipsLost", "iskDestroyed", "iskLost",
    "soloKills", "soloRatio", "dangerRatio",
)


# ── API helpers ───────────────────────────────────────────────────────────────

def _cap_cache(cache: dict) -> None:
    # Éjecte les entrées les plus anciennes au-delà du plafond _MAX_CACHE.
    # Toujours appelé sous _cache_lock — jamais en accès direct.
    overflow = len(cache) - _MAX_CACHE
    if overflow <= 0:
        return
    oldest = sorted(
        cache.items(), key=lambda item: (item[1][0], str(item[0]))
    )[:overflow]
    for key, _ in oldest:
        cache.pop(key, None)


def _prune_caches(now: float) -> None:
    # Purge les entrées expirées et applique le plafond dur aux QUATRE caches.
    # Sous _cache_lock. Sans cette purge systématique, les caches nom/échec
    # grossiraient sans limite pendant une longue session de jeu.
    for key, (ts, _) in list(_cache.items()):
        if now - ts >= STALE_CACHE_TTL:
            _cache.pop(key, None)
    for key, (ts, _) in list(_name_cache.items()):
        if now - ts >= NAME_CACHE_TTL:
            _name_cache.pop(key, None)
    for cache in (_fail_cache, _name_fail_cache):
        for key, (expiry, _) in list(cache.items()):
            if expiry <= now:
                cache.pop(key, None)
    for cache in (_cache, _name_cache, _fail_cache, _name_fail_cache):
        _cap_cache(cache)


def _evict_if_needed() -> None:
    # Alias rétro-compatible du plafonnement du cache positif ; sous verrou.
    # Conservé car des tests/appelants historiques l'utilisent encore.
    _cap_cache(_cache)


def _parse_retry_after(value, *, now: float | None = None) -> float:
    # Convertit un header Retry-After (secondes OU date HTTP RFC) en échéance
    # absolue. Une valeur invalide retombe sur FAIL_CACHE_TTL — jamais
    # d'exception ici : un header mal formé ne doit pas casser le lookup.
    now = time.time() if now is None else now
    if value is not None:
        text = str(value).strip()
        try:
            seconds = float(text)
            if seconds >= 0:
                return now + seconds
        except (TypeError, ValueError):
            pass
        try:
            parsed = parsedate_to_datetime(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            return max(now, parsed.timestamp())
        except (TypeError, ValueError, OverflowError):
            pass
    return now + FAIL_CACHE_TTL


def _raise_failure(code: str, retry_after=None) -> None:
    # Rejoue un échec mis en cache négatif sous sa forme typée d'origine —
    # un RATE LIMITED caché doit rester RATE LIMITED, pas devenir NO DATA.
    if code == _FAIL_RATE_LIMITED:
        raise RateLimited(retry_after)
    if code == _FAIL_NO_STATS:
        raise NoStats("no usable statistics")
    raise NetworkFailure("network request failed")


def _validate_zkill_payload(data: object) -> dict:
    # N'accepte qu'un objet stats portant une identité de pilote exploitable.
    #
    # Pourquoi : un payload {"error": ...} passé à _compute_stats donnait
    # danger 0 → verdict vert « SNUGGLY » pour des données invalides. On
    # refuse ici, AVANT toute mise en cache positive.
    if not isinstance(data, dict) or data.get("error"):
        raise NoStats("zKill returned an error payload")
    info = data.get("info")
    if not isinstance(info, dict):
        raise NoStats("zKill response has no identity")
    name = info.get("name")
    if not isinstance(name, str) or not name.strip():
        raise NoStats("zKill response has no pilot name")
    # Une réponse portant SEULEMENT l'identité (aucun champ de combat) donnait
    # des zéros partout → verdict vert SNUGGLY inventé. Un zéro EXPLICITE
    # (champ présent) reste valide : un pilote neuf a réellement 0 kill.
    if not any(key in data for key in _COMBAT_STAT_KEYS):
        raise NoStats("zKill response carries no combat statistics")
    return data


def _resolve_name(name: str) -> int | None:
    # Résout nom → character_id via ESI en PRÉSERVANT la distinction entre
    # « inconnu » (réponse ESI fiable, cachée 1 h) et panne transitoire
    # (cache négatif court) — les deux s'affichent différemment.
    key = name.lower()
    now = time.time()
    with _cache_lock:
        _prune_caches(now)
        hit = _name_cache.get(key)
        if hit is not None and now - hit[0] < NAME_CACHE_TTL:
            return hit[1]
        failed = _name_fail_cache.get(key)
        if failed is not None and failed[0] > now:
            _raise_failure(failed[1], failed[0] - now)
    try:
        r = _get_session().post(ESI_URL, json=[name], timeout=8)
        if r.status_code == 429:
            deadline = _parse_retry_after(r.headers.get("Retry-After"), now=now)
            with _cache_lock:
                _name_fail_cache[key] = (deadline, _FAIL_RATE_LIMITED)
                _prune_caches(now)
            raise RateLimited(deadline - now)
        r.raise_for_status()
        payload = r.json()
        if not isinstance(payload, dict):
            raise ValueError("ESI response is not an object")
        chars = payload.get("characters", []) or []
        if not isinstance(chars, list):
            raise ValueError("ESI characters field is not a list")
        char_id = int(chars[0]["id"]) if chars else None
        with _cache_lock:
            _name_cache[key] = (now, char_id)
            _name_fail_cache.pop(key, None)
            _prune_caches(now)
        return char_id
    except LookupFailure:
        raise
    except Exception as exc:
        log.error("zkill ESI resolve '%s': %s", name, exc)
        with _cache_lock:
            _name_fail_cache[key] = (now + FAIL_CACHE_TTL, _FAIL_NETWORK)
            _prune_caches(now)
        raise NetworkFailure("ESI request failed") from exc


def _fetch_zkill(char_id: int) -> dict:
    # Récupère les stats zKillboard d'un pilote.
    #
    # Cache positif (5 min) ET négatif (1 min) : un échec récent renvoie une
    # entrée périmée si disponible, sinon une exception typée — sans re-taper
    # l'API. Un 429 est traité à part pour afficher RATE LIMITED.
    global _rate_limit_until

    now = time.time()
    with _cache_lock:
        _prune_caches(now)
        cached = _cache.get(char_id)
        if cached and now - cached[0] < CACHE_TTL:
            return cached[1]
        recent_fail = _fail_cache.get(char_id)
        if recent_fail is not None and recent_fail[0] > now:
            if cached:
                return cached[1]
            _raise_failure(recent_fail[1], recent_fail[0] - now)
        if _rate_limit_until > now:
            if cached:
                return cached[1]
            raise RateLimited(_rate_limit_until - now)

        flight = _inflight.get(char_id)
        owner = flight is None
        if owner:
            flight = threading.Event()
            _inflight[char_id] = flight

    if not owner:
        if not flight.wait(11):
            raise NetworkFailure("timed out waiting for in-flight lookup")
        now = time.time()
        with _cache_lock:
            cached = _cache.get(char_id)
            if cached:
                return cached[1]
            failed = _fail_cache.get(char_id)
            if failed is not None and failed[0] > now:
                _raise_failure(failed[1], failed[0] - now)
        raise NetworkFailure("in-flight lookup ended without a result")

    try:
        r = _get_session().get(
            ZKILL_URL.format(char_id), timeout=10,
            headers={"Accept-Encoding": "gzip"},
        )
        if r.status_code == 429:
            deadline = _parse_retry_after(r.headers.get("Retry-After"), now=now)
            with _cache_lock:
                _rate_limit_until = max(_rate_limit_until, deadline)
                _fail_cache[char_id] = (deadline, _FAIL_RATE_LIMITED)
                _prune_caches(now)
            if cached:
                return cached[1]
            raise RateLimited(deadline - now)
        r.raise_for_status()
        data = _validate_zkill_payload(r.json())
        with _cache_lock:
            _cache[char_id] = (now, data)
            _fail_cache.pop(char_id, None)
            _prune_caches(now)
        return data
    except RateLimited:
        raise
    except NoStats:
        with _cache_lock:
            _fail_cache[char_id] = (now + FAIL_CACHE_TTL, _FAIL_NO_STATS)
            _prune_caches(now)
        if cached:
            return cached[1]
        raise
    except Exception as exc:
        log.error("zkill fetch id=%s: %s", char_id, exc)
        with _cache_lock:
            _fail_cache[char_id] = (now + FAIL_CACHE_TTL, _FAIL_NETWORK)
            _prune_caches(now)
        if cached:
            return cached[1]
        raise NetworkFailure("zKill request failed") from exc
    finally:
        with _cache_lock:
            completed = _inflight.pop(char_id, None)
            if completed is not None:
                completed.set()


def _fmt_isk(v: float) -> str:
    # Formate une valeur ISK en t/b/m lisible.
    if v >= 1e12: return f"{v/1e12:.1f}t"
    if v >= 1e9:  return f"{v/1e9:.1f}b"
    if v >= 1e6:  return f"{v/1e6:.1f}m"
    return str(int(v))


def _as_dict(v) -> dict:
    # Coerce en dict — zKill (PHP) sérialise les tableaux associatifs vides en [].
    return v if isinstance(v, dict) else {}


def _compute_stats(raw: dict, char_id: int) -> dict:
    # Calcule toutes les statistiques affichables depuis les données brutes zKill.
    #
    # Défensif : chaque conteneur imbriqué est coercé (zKillboard renvoie parfois
    # une liste vide `[]` là où on attend un dict), pour ne jamais lever et tuer
    # le thread worker.
    raw        = _as_dict(raw)
    info       = _as_dict(raw.get("info"))
    name       = info.get("name", "Unknown")
    corp       = info.get("corpName", "")
    ships_d    = raw.get("shipsDestroyed", 0) or 0
    ships_l    = raw.get("shipsLost",      0) or 0
    isk_d      = raw.get("iskDestroyed",   0) or 0
    isk_l      = raw.get("iskLost",        0) or 0
    total      = isk_d + isk_l
    isk_eff    = round((isk_d / total * 100) if total else 0.0, 1)
    solo_kills = raw.get("soloKills",   0) or 0
    solo_ratio = raw.get("soloRatio",   0) or 0
    try:
        danger = int(raw.get("dangerRatio"))
    except (TypeError, ValueError):
        vol    = min(100, ships_d / 5)
        danger = int(isk_eff * 0.6 + vol * 0.4)
    danger    = max(0, min(100, danger))
    solo_pct  = max(0, min(100, int(solo_ratio)))
    # Complément RÉEL du ratio solo. L'ancien split gang/fleet (60/40 du
    # non-solo) était fabriqué de toutes pièces — aucune donnée zKill ne le
    # fournit ; on n'affiche que des chiffres vrais.
    group_pct = 100 - solo_pct

    if danger >= 75:   verdict, vcol = "DANGEROUS", "RED"
    elif danger >= 45: verdict, vcol = "MODERATE",  "YELLOW"
    else:              verdict, vcol = "SNUGGLY",   "GREEN"

    tags = [verdict]
    if solo_pct >= 30:  tags.append("SOLO HUNTER")
    else:               tags.append("GROUP PILOT")
    if isk_eff >= 90:   tags.append("HIGH EFF.")
    if solo_kills >= 5: tags.append("SOLO KILL")

    # ── Intel étendu ─────────────────────────────────────────────────────
    # Kills du mois courant — months est indexé par clés "YYYYMM" (ex: "202604").
    kills_30d = 0
    months = _as_dict(raw.get("months"))
    if months:
        now_dt  = datetime.datetime.now(datetime.timezone.utc)
        mo_key  = f"{now_dt.year}{now_dt.month:02d}"
        mo_data = _as_dict(months.get(mo_key))
        kills_30d = mo_data.get("shipsDestroyed", 0) or 0

    # Top ships et zone active viennent de topLists, indexés par "type".
    top_ships   = []
    active_zone = ""
    for entry in raw.get("topLists", []) or []:
        if not isinstance(entry, dict):
            continue
        list_type = entry.get("type", "")
        values    = entry.get("values", []) or []

        if list_type == "shipType" and not top_ships:
            for v in values[:4]:
                if not isinstance(v, dict):
                    continue
                ship_name = v.get("shipName") or v.get("name") or f"TypeID {v.get('shipTypeID', '?')}"
                count     = v.get("kills", 0)
                top_ships.append((ship_name, count))

        elif list_type == "solarSystem" and not active_zone and values:
            first = values[0] if isinstance(values[0], dict) else {}
            zone  = first.get("solarSystemName") or first.get("name") or ""
            active_zone = zone[:24]

    return {
        "char_id":    char_id, "name": name, "corp": corp,
        "ships_d":    ships_d, "ships_l": ships_l,
        "isk_d":      _fmt_isk(isk_d), "isk_l": _fmt_isk(isk_l),
        "isk_eff":    isk_eff, "solo_kills": solo_kills,
        "danger":     danger,  "verdict": verdict, "vcol": vcol,
        "solo_pct":   solo_pct, "group_pct": group_pct,
        "tags":       tags,
        # intel étendu
        "kills_30d":   kills_30d,
        "top_ships":   top_ships,
        "active_zone": active_zone,
    }


def fetch_pilot_stats(name: str, on_ready, on_error) -> None:
    # Résout le nom, récupère les stats et appelle on_ready(stats) ou on_error(msg).
    #
    # Tout le corps est encapsulé : une exception inattendue (payload zKill mal
    # formé, etc.) appelle on_error au lieu de tuer silencieusement le thread —
    # sinon la carte resterait bloquée sur "SCANNING..." indéfiniment.
    try:
        char_id = _resolve_name(name.strip())
        if not char_id:
            raise PilotNotFound(name)
        raw = _fetch_zkill(char_id)
        on_ready(_compute_stats(raw, char_id))
    except PilotNotFound:
        on_error("UNKNOWN PILOT")
    except NoStats:
        on_error("NO DATA")
    except RateLimited:
        on_error("RATE LIMITED")
    except NetworkFailure:
        on_error("NETWORK ERROR")
    except Exception as exc:
        log.error("fetch_pilot_stats '%s' failed: %s", name, exc)
        on_error("NETWORK ERROR")

# -*- coding: utf-8 -*-
# Tests de tm.zkill_stats — stats, formatage, cache et sessions. Aucun accès réseau.
import datetime
import threading
from email.utils import format_datetime

import pytest

import tm.zkill_stats as zk
from tm.zkill_stats import (
    _fmt_isk, _as_dict, _compute_stats, RateLimited,
)


# ── _fmt_isk ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (0,          "0"),
    (999,        "999"),
    (999_999,    "999999"),
    (1_000_000,  "1.0m"),
    (2_500_000,  "2.5m"),
    (1_000_000_000, "1.0b"),
    (1_200_000_000_000, "1.2t"),
])
def test_fmt_isk(value, expected):
    assert _fmt_isk(value) == expected


# ── _as_dict (coercion PHP []) ────────────────────────────────────────────────

def test_as_dict_passthrough():
    assert _as_dict({"a": 1}) == {"a": 1}

def test_as_dict_coerces_php_empty_array():
    assert _as_dict([]) == {}
    assert _as_dict(None) == {}


# ── _compute_stats ────────────────────────────────────────────────────────────

def test_compute_stats_empty_payload_is_safe():
    s = _compute_stats({}, 42)
    assert s["char_id"] == 42
    assert s["name"] == "Unknown"
    assert 0 <= s["danger"] <= 100
    assert s["verdict"] == "SNUGGLY"

def test_compute_stats_php_empty_arrays_do_not_crash():
    raw = {"info": [], "months": [], "topLists": []}
    s = _compute_stats(raw, 1)
    assert s["name"] == "Unknown"

def test_compute_stats_dangerous_pilot():
    raw = {
        "info": {"name": "Bad Guy", "corpName": "Evil Corp"},
        "shipsDestroyed": 500, "shipsLost": 10,
        "iskDestroyed": 9.5e9, "iskLost": 0.5e9,
        "dangerRatio": 90, "soloKills": 50, "soloRatio": 40,
    }
    s = _compute_stats(raw, 7)
    assert s["name"] == "Bad Guy"
    assert s["verdict"] == "DANGEROUS" and s["vcol"] == "RED"
    assert s["isk_eff"] == 95.0
    assert s["isk_d"] == "9.5b"
    assert "SOLO HUNTER" in s["tags"]
    assert "HIGH EFF." in s["tags"]
    assert "SOLO KILL" in s["tags"]
    assert s["solo_pct"] == 40
    assert s["solo_pct"] + s["group_pct"] == 100

def test_compute_stats_danger_clamped():
    assert _compute_stats({"dangerRatio": 250}, 1)["danger"] == 100

def test_compute_stats_solo_ratio_clamped_at_zero():
    assert _compute_stats({"soloRatio": -20}, 1)["solo_pct"] == 0

def test_compute_stats_kills_current_month():
    now = datetime.datetime.now(datetime.timezone.utc)
    key = f"{now.year}{now.month:02d}"
    raw = {"months": {key: {"shipsDestroyed": 17}}}
    assert _compute_stats(raw, 1)["kills_30d"] == 17

def test_compute_stats_top_ships_and_zone():
    raw = {"topLists": [
        {"type": "shipType", "values": [
            {"shipName": "Rifter", "kills": 12},
            {"shipName": "Sabre",  "kills": 9},
        ]},
        {"type": "solarSystem", "values": [{"solarSystemName": "Tama"}]},
    ]}
    s = _compute_stats(raw, 1)
    assert s["top_ships"] == [("Rifter", 12), ("Sabre", 9)]
    assert s["active_zone"] == "Tama"


# ── Sessions HTTP par thread ──────────────────────────────────────────────────

def test_sessions_are_per_thread():
    # requests.Session n'est pas garanti thread-safe : chaque thread worker doit
    # obtenir sa propre session (keep-alive conservé au sein d'un même lookup).
    s_main = zk._get_session()
    assert s_main is zk._get_session()          # stable dans un même thread
    assert s_main.headers["User-Agent"].startswith("EveThreatMonitor")

    from_thread = []
    t = threading.Thread(target=lambda: from_thread.append(zk._get_session()))
    t.start(); t.join()
    assert from_thread[0] is not s_main         # session distincte par thread


# ── Cache de _fetch_zkill (session factice, zéro réseau) ─────────────────────

class FakeResponse:
    def __init__(self, status=200, data=None, retry_after=None):
        self.status_code = status
        self._data = data or {}
        self.headers = {"Retry-After": retry_after} if retry_after else {}
    def json(self):
        return self._data
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = 0
        self.headers = dict(zk.HEADERS)
    def get(self, *a, **k):
        self.calls += 1
        return self.response
    def post(self, *a, **k):
        self.calls += 1
        return self.response


@pytest.fixture
def clean_caches(monkeypatch):
    # Vide les caches module entre les tests pour éviter tout couplage.
    monkeypatch.setattr(zk, "_cache", {})
    monkeypatch.setattr(zk, "_name_cache", {})
    monkeypatch.setattr(zk, "_fail_cache", {})
    monkeypatch.setattr(zk, "_name_fail_cache", {}, raising=False)
    monkeypatch.setattr(zk, "_inflight", {}, raising=False)
    monkeypatch.setattr(zk, "_rate_limit_until", 0.0, raising=False)

def _use_session(monkeypatch, fake):
    monkeypatch.setattr(zk, "_get_session", lambda: fake, raising=False)
    monkeypatch.setattr(zk, "_session", fake, raising=False)

def test_fetch_zkill_uses_cache_within_ttl(clean_caches, monkeypatch):
    fake = FakeSession(FakeResponse(data={
        "info": {"name": "Test Pilot"}, "shipsDestroyed": 3,
    }))
    _use_session(monkeypatch, fake)
    assert zk._fetch_zkill(1)["shipsDestroyed"] == 3
    assert zk._fetch_zkill(1)["shipsDestroyed"] == 3
    assert fake.calls == 1                       # 2e appel servi par le cache

def test_fetch_zkill_rate_limited_raises_without_cache(clean_caches, monkeypatch):
    _use_session(monkeypatch, FakeSession(FakeResponse(status=429, retry_after="30")))
    with pytest.raises(RateLimited):
        zk._fetch_zkill(2)

def test_fetch_zkill_failure_negative_cached(clean_caches, monkeypatch):
    fake = FakeSession(FakeResponse(status=500))
    _use_session(monkeypatch, fake)
    with pytest.raises(zk.NetworkFailure):
        zk._fetch_zkill(3)
    with pytest.raises(zk.NetworkFailure):
        zk._fetch_zkill(3)
    assert fake.calls == 1                       # échec récent → pas de re-hit


# ── Échecs typés et validation des réponses ──────────────────────────────────

def test_error_payload_is_never_computed_as_snuggly():
    with pytest.raises(zk.NoStats):
        zk._validate_zkill_payload({"error": "Invalid type or id"})


def test_missing_identity_is_rejected():
    with pytest.raises(zk.NoStats):
        zk._validate_zkill_payload({"shipsDestroyed": 5})


def test_identity_only_payload_is_no_data():
    # Une réponse portant SEULEMENT l'identité (aucun champ de combat) donnait
    # des zéros partout → verdict vert SNUGGLY inventé. C'est NO DATA.
    with pytest.raises(zk.NoStats):
        zk._validate_zkill_payload({"info": {"name": "Ghost Pilot"}})


def test_playstyle_group_share_is_real_complement():
    # Le split gang/fleet 60/40 était fabriqué (aucune donnée zKill derrière).
    # On n'affiche que des chiffres réels : solo (zKill) et son complément.
    s = _compute_stats({"soloRatio": 40}, 1)
    assert s["solo_pct"] == 40
    assert s["group_pct"] == 60
    assert "gang_pct" not in s and "fleet_pct" not in s
    assert "SOLO HUNTER" in s["tags"]


def test_playstyle_low_solo_tags_group_pilot():
    s = _compute_stats({"soloRatio": 5}, 1)
    assert s["group_pct"] == 95
    assert "GROUP PILOT" in s["tags"]


def test_valid_zero_stat_identity_is_accepted():
    payload = {"info": {"name": "New Pilot"}, "shipsDestroyed": 0}
    assert zk._validate_zkill_payload(payload) is payload


def test_fetch_pilot_stats_reports_typed_errors(monkeypatch):
    cases = [
        (zk.PilotNotFound(), "UNKNOWN PILOT"),
        (zk.NoStats(), "NO DATA"),
        (zk.RateLimited("30"), "RATE LIMITED"),
        (zk.NetworkFailure(), "NETWORK ERROR"),
    ]
    for error, expected in cases:
        ready = []
        errors = []
        monkeypatch.setattr(
            zk, "_resolve_name", lambda name, exc=error: (_ for _ in ()).throw(exc)
        )
        zk.fetch_pilot_stats("Test Pilot", ready.append, errors.append)
        assert ready == []
        assert errors == [expected]


def test_parse_retry_after_seconds_and_http_date():
    now = 1_700_000_000.0
    future = datetime.datetime.fromtimestamp(now + 120, datetime.timezone.utc)
    assert zk._parse_retry_after("300", now=now) == now + 300
    assert zk._parse_retry_after(format_datetime(future, usegmt=True), now=now) == now + 120
    assert zk._parse_retry_after("invalid", now=now) == now + zk.FAIL_CACHE_TTL


def test_429_blocks_other_character_until_global_deadline(
    clean_caches, monkeypatch,
):
    now = [1000.0]
    fake = FakeSession(FakeResponse(status=429, retry_after="300"))
    _use_session(monkeypatch, fake)
    monkeypatch.setattr(zk.time, "time", lambda: now[0])

    with pytest.raises(RateLimited):
        zk._fetch_zkill(1)
    fake.response = FakeResponse(data={"info": {"name": "Other Pilot"}})
    now[0] = 1001.0
    with pytest.raises(RateLimited):
        zk._fetch_zkill(2)
    assert fake.calls == 1


def test_rate_limit_failure_keeps_its_type_in_negative_cache(
    clean_caches, monkeypatch,
):
    now = [1000.0]
    fake = FakeSession(FakeResponse(status=429, retry_after="300"))
    _use_session(monkeypatch, fake)
    monkeypatch.setattr(zk.time, "time", lambda: now[0])

    with pytest.raises(RateLimited):
        zk._fetch_zkill(7)
    now[0] = 1001.0
    with pytest.raises(RateLimited):
        zk._fetch_zkill(7)


def test_all_caches_are_pruned_and_bounded(clean_caches):
    now = 10_000.0
    for value in range(zk._MAX_CACHE + 50):
        zk._cache[value] = (now - value, {"info": {"name": str(value)}})
        zk._name_cache[str(value)] = (now - value, value)
        zk._fail_cache[value] = (now + 60, "NETWORK")
        zk._name_fail_cache[str(value)] = (now + 60, "NETWORK")

    with zk._cache_lock:
        zk._prune_caches(now)

    assert len(zk._cache) <= zk._MAX_CACHE
    assert len(zk._name_cache) <= zk._MAX_CACHE
    assert len(zk._fail_cache) <= zk._MAX_CACHE
    assert len(zk._name_fail_cache) <= zk._MAX_CACHE


def test_same_character_concurrent_fetch_is_single_flight(
    clean_caches, monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()

    class BlockingSession(FakeSession):
        def get(self, *args, **kwargs):
            self.calls += 1
            entered.set()
            assert release.wait(2)
            return self.response

    fake = BlockingSession(FakeResponse(data={
        "info": {"name": "One Pilot"}, "shipsDestroyed": 3,
    }))
    _use_session(monkeypatch, fake)
    results = []
    errors = []

    def run():
        try:
            results.append(zk._fetch_zkill(42))
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    assert entered.wait(1)
    second.start()
    release.set()
    first.join(2)
    second.join(2)

    assert errors == []
    assert len(results) == 2
    assert fake.calls == 1

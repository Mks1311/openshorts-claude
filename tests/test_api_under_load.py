"""What keeps the API answering when the host is short of CPU (16-nov):
the owner check of a status poll skips the full user load, the DB pool has
room and a short wait."""
import asyncio
import time

import app as app_module
from cloud import database, owner_cache


class _User:
    def __init__(self, uid):
        self.id = uid


class _Req:
    def __init__(self, path, method="GET", auth="Bearer t1"):
        self.method = method
        self.headers = {"Authorization": auth} if auth else {}

        class _U:
            pass
        self.url = _U()
        self.url.path = path


def _counting_loader(monkeypatch, user):
    calls = {"n": 0}

    async def _from_request(request):
        calls["n"] += 1
        return user["u"]
    monkeypatch.setattr(app_module, "_user_from_request", _from_request)
    owner_cache.clear()
    return calls


def _owner(req):
    return asyncio.run(app_module._owner_id_from_request(req))


def test_repeated_polls_load_the_user_once(monkeypatch):
    user = {"u": _User("owner")}
    calls = _counting_loader(monkeypatch, user)
    for path in ("/api/status/j1", "/api/status/j2", "/api/status/j1"):
        assert _owner(_Req(path)) == "owner"
    assert calls["n"] == 1
    # Another credential is looked up on its own.
    _owner(_Req("/api/status/j1", auth="Bearer t2"))
    assert calls["n"] == 2


def test_writes_anonymous_and_unknown_callers_are_never_remembered(monkeypatch):
    user = {"u": None}
    calls = _counting_loader(monkeypatch, user)
    for _ in range(2):
        assert _owner(_Req("/api/status/j1")) is None
    user["u"] = _User("owner")
    for _ in range(2):
        _owner(_Req("/api/x", method="POST"))
        _owner(_Req("/api/status/j1", auth=None))
    assert calls["n"] == 6


def test_a_remembered_caller_expires(monkeypatch):
    user = {"u": _User("owner")}
    calls = _counting_loader(monkeypatch, user)
    _owner(_Req("/api/status/j1"))
    user["u"] = None
    for k, v in list(owner_cache._entries.items()):
        owner_cache._entries[k] = (0.0, v[1])
    assert _owner(_Req("/api/status/j1")) is None
    assert calls["n"] == 2


def test_a_ban_or_revoke_is_felt_at_once(monkeypatch):
    user = {"u": _User("owner")}
    calls = _counting_loader(monkeypatch, user)
    _owner(_Req("/api/status/j1"))
    user["u"] = None                      # banned or key revoked
    owner_cache.forget_user("owner")
    assert _owner(_Req("/api/status/j1")) is None
    assert calls["n"] == 2


def test_a_full_gate_answers_from_memory_or_asks_to_retry(monkeypatch):
    import pytest
    from fastapi import HTTPException
    user = {"u": _User("owner")}
    _counting_loader(monkeypatch, user)
    monkeypatch.setattr(owner_cache, "OWNER_LOOKUP_WAIT", 0.05)
    _owner(_Req("/api/status/j1"))                     # remembered...
    for k, v in list(owner_cache._entries.items()):
        owner_cache._entries[k] = (time.monotonic() - 1, v[1])   # ...past its TTL

    async def _with_gate_full(req):
        gate = owner_cache._gate_now()
        for _ in range(owner_cache.OWNER_LOOKUP_CONCURRENCY):
            await gate.acquire()
        return await app_module._owner_id_from_request(req)
    # A caller seen recently still gets its answer, with no DB lookup.
    assert asyncio.run(_with_gate_full(_Req("/api/status/j1"))) == "owner"
    owner_cache._gate = None
    # An unknown one is told to retry, instead of queueing for the pool.
    with pytest.raises(HTTPException) as e:
        asyncio.run(_with_gate_full(_Req("/api/status/j1", auth="Bearer new")))
    assert e.value.status_code == 503


def test_postgres_pool_has_room_and_a_short_wait(monkeypatch):
    for name in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    opts = database._pool_options("postgresql+asyncpg://u:p@db/x")
    assert opts == {"pool_size": 15, "max_overflow": 15, "pool_timeout": 10}
    monkeypatch.setenv("DB_POOL_SIZE", "8")
    assert database._pool_options("postgresql+asyncpg://u:p@db/x")["pool_size"] == 8
    assert database._pool_options("sqlite+aiosqlite:///:memory:") == {}


def test_a_child_probe_brings_back_what_it_learned(monkeypatch):
    from cloud import metering
    from yt_clients import NotASingleVideo

    def _probe(url, allow_paid=True):
        metering._probe_facts[url] = {"at": time.time(), "max_height": 1080, "duration": 60}
        metering._static_bot_verdicts.add(url)
        metering._paid_probe_events.append({"url": url})
        return 1.5
    monkeypatch.setattr(metering, "probe_url_minutes", _probe)
    out = metering._probe_in_child("https://v/1", True)
    # The child's module state is drained into the result...
    assert "https://v/1" not in metering._static_bot_verdicts
    assert metering.pop_paid_probe_events() == []
    metering._probe_facts.pop("https://v/1", None)
    # ...and lands in the parent's.
    assert metering._merge_child_probe("https://v/1", out) == 1.5
    assert metering.probe_facts("https://v/1")["max_height"] == 1080
    assert metering.pop_statics_bot_checked("https://v/1") is True
    assert metering.pop_paid_probe_events() == [{"url": "https://v/1"}]

    def _not_single(url, allow_paid=True):
        raise NotASingleVideo("This link is a playlist.")
    monkeypatch.setattr(metering, "probe_url_minutes", _not_single)
    import pytest
    with pytest.raises(NotASingleVideo):
        metering._merge_child_probe("u", metering._probe_in_child("u", True))


def test_the_probe_runs_in_a_pool_process(monkeypatch):
    from cloud import metering
    import pytest
    monkeypatch.setattr(metering, "PROBE_PROCESSES", 1)
    monkeypatch.setattr(metering, "_probe_pool", None)
    try:
        # Refused before any network: the scheme is not http(s).
        with pytest.raises(ValueError):
            asyncio.run(metering.run_probe("file:///etc/passwd"))
        assert metering._probe_pool is not None      # it ran there, not in a fallback
    finally:
        if metering._probe_pool is not None:
            metering._probe_pool.shutdown(wait=True)
        metering._probe_pool = None


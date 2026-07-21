import importlib
import json
import types

import pytest


def _client(monkeypatch):
    monkeypatch.setenv("ARCA_JWT_MODE", "local")
    monkeypatch.setenv("ARCA_ACCESS_SECRET", "test-secret-key-that-is-32-bytes!!")
    monkeypatch.setenv("ARCA_UID", "u1")
    monkeypatch.setenv("ARCA_BASE_URL", "https://arca.test")
    monkeypatch.setenv("ARCA_LANGUAGE_DEFAULT", "zh")
    import app.config as config
    importlib.reload(config)
    import app.arca_client as ac
    importlib.reload(ac)
    return ac


class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._p = payload
        self.status_code = status
        self.text = text or json.dumps(payload)

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


def test_create_character_polls_until_ready(monkeypatch):
    ac = _client(monkeypatch)
    calls = {"n": 0}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        if url.endswith("/character/create"):
            assert headers["Authorization"].startswith("Bearer ")
            assert headers["X-Language"] == "ja"
            assert json["source"] == "character"
            assert headers.get("Idempotency-Key") == "idem-1"
            return _Resp({"code": 0, "msg": "ok", "data": {"task_id": "t99"}})
        if url.endswith("/task/get_status"):
            calls["n"] += 1
            if calls["n"] < 2:
                return _Resp({"code": 0, "data": {"status": "processing"}})
            result = __import__("json").dumps({"character_id": "char-abc"})
            return _Resp({"code": 0, "data": {"status": "ready", "result": result}})
        raise AssertionError(url)

    monkeypatch.setattr(ac.requests, "post", fake_post)
    cid = ac.create_character({"name": "A"}, lang="ja",
                              idempotency_key="idem-1", poll_interval=0)
    assert cid == "char-abc"


def test_create_character_failed_status_raises(monkeypatch):
    ac = _client(monkeypatch)

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        if url.endswith("/character/create"):
            return _Resp({"code": 0, "data": {"task_id": "t1"}})
        return _Resp({"code": 0, "data": {"status": "failed", "error_message": "餘額不足", "error_code": 40402}})

    monkeypatch.setattr(ac.requests, "post", fake_post)
    with pytest.raises(ac.ArcaError) as e:
        ac.create_character({"name": "A"}, lang="zh", poll_interval=0)
    assert "餘額不足" in str(e.value)

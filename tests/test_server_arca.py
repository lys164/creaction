import importlib
import time

from fastapi.testclient import TestClient


def test_arca_sync_endpoint_runs_batch(monkeypatch):
    monkeypatch.setenv("ARCA_UID", "u1")
    import app.server as server
    importlib.reload(server)

    def fake_sync(char_id, **kw):
        return {"char_id": char_id, "arca_character_id": f"arca-{char_id}",
                "posts": [], "errors": [], "skipped": False, "landing_url": None}

    monkeypatch.setattr(server.arca_sync, "sync_character", fake_sync)
    client = TestClient(server.app)

    r = client.post("/api/arca/sync", json={"char_ids": ["c1", "c2"]})
    assert r.status_code == 200
    tid = r.json()["task_id"]

    for _ in range(50):
        t = client.get(f"/api/tasks/{tid}").json()
        if t["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert t["status"] == "done"
    ids = {row["arca_character_id"] for row in t["result"]}
    assert ids == {"arca-c1", "arca-c2"}


def test_arca_sync_endpoint_batch_resilient(monkeypatch):
    """一個角色同步拋異常 → 該角色成 error 行，其餘角色仍處理，整批不中斷。"""
    monkeypatch.setenv("ARCA_UID", "u1")
    import app.server as server
    importlib.reload(server)

    def fake_sync(char_id, **kw):
        if char_id == "bad":
            raise RuntimeError("boom")
        return {"char_id": char_id, "arca_character_id": f"arca-{char_id}",
                "posts": [], "errors": [], "skipped": False, "landing_url": None}

    monkeypatch.setattr(server.arca_sync, "sync_character", fake_sync)
    client = TestClient(server.app)

    r = client.post("/api/arca/sync", json={"char_ids": ["c1", "bad", "c2"]})
    assert r.status_code == 200
    tid = r.json()["task_id"]

    for _ in range(50):
        t = client.get(f"/api/tasks/{tid}").json()
        if t["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert t["status"] == "done"
    rows = {row["char_id"]: row for row in t["result"]}
    # 全部 3 個角色都有結果行，整批未中斷
    assert set(rows) == {"c1", "bad", "c2"}
    assert rows["c1"]["arca_character_id"] == "arca-c1"
    assert rows["c2"]["arca_character_id"] == "arca-c2"
    # 失敗角色成 error 行
    assert rows["bad"]["arca_character_id"] is None
    assert any("boom" in e for e in rows["bad"]["errors"])

import importlib
import json


def _mods(monkeypatch, key=""):
    monkeypatch.setenv("ARCA_BASE_URL", "https://arca.test")
    monkeypatch.setenv("ARCA_STORAGE_KEY", key)
    import app.config as config
    importlib.reload(config)
    import app.arca_storage as ash
    importlib.reload(ash)
    import app.storage as st
    importlib.reload(st)
    return ash, st


def test_local_only_when_key_missing(monkeypatch, tmp_path):
    ash, st = _mods(monkeypatch, key="")
    assert ash.enabled() is False
    local = tmp_path / "personas" / "c1.json"

    def _boom(*a, **k):
        raise AssertionError("未配 key 不應有遠端呼叫")
    monkeypatch.setattr(ash, "put_record", _boom)
    monkeypatch.setattr(ash, "get_record", _boom)
    monkeypatch.setattr(ash, "query_records", _boom)

    st.save_json("personas", "c1", {"a": 1}, local)
    assert json.loads(local.read_text()) == {"a": 1}
    assert st.load_json("personas", "c1", local) == {"a": 1}
    listed = st.list_json("personas", tmp_path / "personas")
    assert listed == {"c1": {"a": 1}}
    # 本地缺失 + 未啟用遠端 → None / False
    assert st.load_json("personas", "nope", tmp_path / "personas" / "nope.json") is None
    assert st.ensure_file(tmp_path / "img" / "x.png") is False


def test_save_writes_local_and_remote(monkeypatch, tmp_path):
    ash, st = _mods(monkeypatch, key="sk_test")
    calls = {}
    monkeypatch.setattr(ash, "ensure_collection", lambda name, description="": None)
    monkeypatch.setattr(ash, "put_record",
                        lambda coll, key, data: calls.update(coll=coll, key=key, data=data))
    local = tmp_path / "c1.json"
    st.save_json("personas", "c1", {"a": 1}, local)
    assert local.exists()
    assert calls == {"coll": "personas", "key": "c1", "data": {"a": 1}}


def test_save_remote_failure_does_not_block_local(monkeypatch, tmp_path):
    ash, st = _mods(monkeypatch, key="sk_test")
    monkeypatch.setattr(ash, "ensure_collection", lambda *a, **k: None)

    def _fail(*a, **k):
        raise ash.StorageError("boom")
    monkeypatch.setattr(ash, "put_record", _fail)
    local = tmp_path / "c1.json"
    st.save_json("personas", "c1", {"a": 1}, local)  # 不應 raise
    assert json.loads(local.read_text()) == {"a": 1}


def test_load_falls_back_to_remote_and_caches(monkeypatch, tmp_path):
    ash, st = _mods(monkeypatch, key="sk_test")
    monkeypatch.setattr(ash, "get_record", lambda coll, key: {"from": "remote"})
    local = tmp_path / "c2.json"
    obj = st.load_json("personas", "c2", local)
    assert obj == {"from": "remote"}
    # 回源命中已回寫本地快取
    assert json.loads(local.read_text()) == {"from": "remote"}


def test_list_merges_remote_and_caches(monkeypatch, tmp_path):
    ash, st = _mods(monkeypatch, key="sk_test")
    d = tmp_path / "personas"
    d.mkdir()
    (d / "c1.json").write_text('{"who": "local"}', encoding="utf-8")
    monkeypatch.setattr(ash, "query_records", lambda coll, **kw: [
        {"key": "c1", "data": {"who": "remote-dup"}},   # 本地已有 → 本地優先
        {"key": "c9", "data": {"who": "remote-only"}},  # 遠端獨有 → 合併 + 回寫
    ])
    listed = st.list_json("personas", d)
    assert listed["c1"] == {"who": "local"}
    assert listed["c9"] == {"who": "remote-only"}
    assert (d / "c9.json").exists()


def test_delete_removes_local_and_remote(monkeypatch, tmp_path):
    ash, st = _mods(monkeypatch, key="sk_test")
    deleted = {}
    monkeypatch.setattr(ash, "delete_record",
                        lambda coll, key: deleted.update(coll=coll, key=key))
    local = tmp_path / "c1.json"
    local.write_text("{}", encoding="utf-8")
    st.delete_json("personas", "c1", local)
    assert not local.exists()
    assert deleted == {"coll": "personas", "key": "c1"}


def test_arca_storage_get_404_returns_none(monkeypatch):
    ash, _ = _mods(monkeypatch, key="sk_test")

    class _R:
        status_code = 404
        text = "記錄不存在"
        class request:  # noqa: N801
            method = "GET"
        url = "u"

        def json(self):
            return {}

    monkeypatch.setattr(ash.requests, "get", lambda *a, **k: _R())
    assert ash.get_record("personas", "nope") is None


def test_save_json_oss_fields_split_and_restore(monkeypatch, tmp_path):
    ash, st = _mods(monkeypatch, key="sk_test")
    monkeypatch.setattr(ash, "ensure_collection", lambda *a, **k: None)

    oss_store = {}

    class _FakeBucket:
        def put_object(self, key, data, headers=None):
            oss_store[key] = data

        def get_object(self, key):
            import io
            if key not in oss_store:
                raise KeyError(key)
            return io.BytesIO(oss_store[key])

    monkeypatch.setattr(st, "_oss_bucket", lambda: _FakeBucket())
    put = {}
    monkeypatch.setattr(ash, "put_record", lambda coll, key, data: put.update(data=data))

    page = {"char_id": "c1", "html": "<html>大頁面</html>",
            "html_filled": "<html>filled</html>", "style_text": "ins"}
    local = tmp_path / "landing_latest.json"
    st.save_json("landings", "c1", page, local, oss_fields=["html", "html_filled"])

    # 本地快取仍是完整版
    import json as _j
    assert _j.loads(local.read_text())["html"] == "<html>大頁面</html>"
    # hub 記錄裡 html 變佔位，後設資料保留
    assert put["data"]["html"] == {"__oss_key__": "creaction-data/hubfields/landings/c1/html"}
    assert put["data"]["style_text"] == "ins"
    # OSS 收到了 HTML 本體
    assert oss_store["creaction-data/hubfields/landings/c1/html"] == "<html>大頁面</html>".encode()

    # 回源還原：本地缺失時 load_json 從 hub 拿佔位記錄並從 OSS 拉回原文
    monkeypatch.setattr(ash, "get_record", lambda coll, key: dict(put["data"]))
    local2 = tmp_path / "restored.json"
    obj = st.load_json("landings", "c1", local2)
    assert obj["html"] == "<html>大頁面</html>"
    assert obj["html_filled"] == "<html>filled</html>"
    assert _j.loads(local2.read_text())["html"] == "<html>大頁面</html>"  # 快取完整版

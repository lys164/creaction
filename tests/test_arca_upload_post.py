import importlib
import json

import pytest


def _client(monkeypatch):
    for k, v in {"ARCA_JWT_MODE": "local",
                 "ARCA_ACCESS_SECRET": "test-secret-key-that-is-32-bytes!!",
                 "ARCA_UID": "u1", "ARCA_BASE_URL": "https://arca.test"}.items():
        monkeypatch.setenv(k, v)
    import app.config as config
    importlib.reload(config)
    import app.arca_client as ac
    importlib.reload(ac)
    return ac


class _Resp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


def test_tos_upload_returns_storage_object(monkeypatch):
    ac = _client(monkeypatch)

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        assert url.endswith("/file/tos_credential")
        return _Resp({"code": 0, "data": {
            "access_key_id": "ak", "secret_access_key": "sk",
            "session_token": "st", "bucket": "bkt",
            "region": "ap-northeast-1",
            "endpoint": "https://oss-ap-northeast-1.aliyuncs.com",
            "expires_in": 3600}})

    monkeypatch.setattr(ac.requests, "post", fake_post)

    uploaded = {}

    def fake_put(endpoint, bucket, key, content, ak, sk, token, content_type):
        uploaded.update(dict(endpoint=endpoint, bucket=bucket, key=key,
                             ak=ak, sk=sk, token=token, content_type=content_type))

    monkeypatch.setattr(ac, "_oss_put_object", fake_put)

    obj = ac.tos_upload(b"\x89PNG", "images/x.png", "image/png", lang="zh")
    assert obj["bucket_name"] == "bkt"
    assert obj["object_key"] == "images/x.png"
    assert obj["object_type"] == "image"
    # 虚拟主机风格直链：bucket 前缀 + OSS 公网 host
    assert obj["url"] == "https://bkt.oss-ap-northeast-1.aliyuncs.com/images/x.png"
    # STS 三件套 + endpoint 正确透传给 oss2
    assert uploaded["endpoint"] == "https://oss-ap-northeast-1.aliyuncs.com"
    assert (uploaded["ak"], uploaded["sk"], uploaded["token"]) == ("ak", "sk", "st")
    assert uploaded["content_type"] == "image/png"


def test_create_post_returns_post_id(monkeypatch):
    ac = _client(monkeypatch)

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        assert url.endswith("/post/create")
        assert json["character_id"] == "c1"
        assert json["content"] == "今天天气真好"
        assert json["images"][0]["image_type"] == "aigc"
        assert json["images"][0]["media"]["object_key"] == "k"
        return _Resp({"code": 0, "data": {"post_id": "p777"}})

    monkeypatch.setattr(ac.requests, "post", fake_post)
    pid = ac.create_post("c1", "今天天气真好",
                         [{"bucket_name": "b", "object_key": "k", "object_type": "image"}],
                         lang="zh", visibility=1)
    assert pid == "p777"


def test_set_post_visibility(monkeypatch):
    ac = _client(monkeypatch)
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        assert url.endswith("/post/update_visibility")
        seen.update(json)
        return _Resp({"code": 0, "data": {}})

    monkeypatch.setattr(ac.requests, "post", fake_post)
    ac.set_post_visibility("p777", 3, lang="zh")
    assert seen == {"post_id": "p777", "visibility": 3}

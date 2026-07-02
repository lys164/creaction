import importlib
import pytest
import jwt as pyjwt

_SECRET = "s3cr3t-this-is-long-enough-for-hs256-32b"


def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import app.config as config
    importlib.reload(config)
    import app.arca_client as ac
    importlib.reload(ac)
    return ac


def test_local_sign_token_roundtrip(monkeypatch):
    ac = _reload(monkeypatch, ARCA_JWT_MODE="local",
                 ARCA_ACCESS_SECRET=_SECRET, ARCA_UID="u42",
                 ARCA_JWT_EXPIRES="3600")
    tok = ac.get_token()
    claims = pyjwt.decode(tok, _SECRET, algorithms=["HS256"])
    assert claims["uid"] == "u42"
    assert "exp" in claims


def test_auth_header_has_bearer(monkeypatch):
    ac = _reload(monkeypatch, ARCA_JWT_MODE="local",
                 ARCA_ACCESS_SECRET=_SECRET, ARCA_UID="u42")
    h = ac.auth_header()
    assert h["Authorization"].startswith("Bearer ")


def test_missing_secret_raises(monkeypatch):
    ac = _reload(monkeypatch, ARCA_JWT_MODE="local",
                 ARCA_ACCESS_SECRET="", ARCA_UID="u42")
    with pytest.raises(ac.ArcaError):
        ac.get_token()


def test_headers_region_follows_character_lang(monkeypatch):
    monkeypatch.delenv("ARCA_REGION_BY_LANG", raising=False)
    ac = _reload(monkeypatch, ARCA_JWT_MODE="local",
                 ARCA_ACCESS_SECRET=_SECRET, ARCA_UID="u42",
                 ARCA_REGION="KR")
    assert ac._headers("ja")["X-Language"] == "ja"
    assert ac._headers("ja")["X-Region"] == "JP"
    assert ac._headers("zh")["X-Region"] == "TW"  # CN 会被 RegionBlock 拒 403，映射到 TW
    assert ac._headers("en")["X-Region"] == "US"
    assert ac._headers("ko")["X-Region"] == "KR"
    # 未知语言回退全局 ARCA_REGION
    assert ac._headers("fr")["X-Region"] == "KR"


def test_headers_region_zh_hant_and_region_tags(monkeypatch):
    monkeypatch.delenv("ARCA_REGION_BY_LANG", raising=False)
    ac = _reload(monkeypatch, ARCA_JWT_MODE="local",
                 ARCA_ACCESS_SECRET=_SECRET, ARCA_UID="u42",
                 ARCA_REGION="KR")
    # zh-Hant 归属繁中地区
    assert ac._headers("zh-Hant")["X-Region"] == "TW"
    # 语言标签自带地区码 → 直接采用
    assert ac._headers("zh-HK")["X-Region"] == "HK"
    assert ac._headers("zh-Hant-TW")["X-Region"] == "TW"
    assert ac._headers("en-GB")["X-Region"] == "GB"
    # 未知带脚本标签的语言退语言主码（zh-Hans → zh → TW）
    assert ac._headers("zh-Hans")["X-Region"] == "TW"


def test_headers_region_env_override(monkeypatch):
    ac = _reload(monkeypatch, ARCA_JWT_MODE="local",
                 ARCA_ACCESS_SECRET=_SECRET, ARCA_UID="u42",
                 ARCA_REGION_BY_LANG='{"ja": "sg"}')
    assert ac._headers("ja")["X-Region"] == "SG"  # env 覆盖且强制大写

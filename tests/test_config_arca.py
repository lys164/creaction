import importlib


def test_arca_config_stable_defaults(monkeypatch):
    # 与部署无关的结构性默认(数值/布尔)应稳定；base_url 末尾斜杠被去除。
    # base_url/uid/jwt_mode 的字符串默认属部署配置(可能就地填真值)，不在此断言具体值，
    # env 覆盖机制由 test_arca_config_from_env 校验。
    for k in ("ARCA_JWT_EXPIRES", "ARCA_POST_VISIBILITY", "ARCA_SYNC_LANDING"):
        monkeypatch.delenv(k, raising=False)
    import app.config as config
    importlib.reload(config)
    assert config.ARCA_JWT_EXPIRES == 2592000
    assert config.ARCA_POST_VISIBILITY == 0  # 0=跟随角色可见性
    assert config.ARCA_SYNC_LANDING is True
    assert isinstance(config.ARCA_BASE_URL, str) and not config.ARCA_BASE_URL.endswith("/")
    assert isinstance(config.ARCA_UID, str)


def test_arca_config_from_env(monkeypatch):
    monkeypatch.setenv("ARCA_BASE_URL", "https://arca.example.com")
    monkeypatch.setenv("ARCA_UID", "u123")
    monkeypatch.setenv("ARCA_JWT_MODE", "endpoint")
    import app.config as config
    importlib.reload(config)
    assert config.ARCA_BASE_URL == "https://arca.example.com"
    assert config.ARCA_UID == "u123"
    assert config.ARCA_JWT_MODE == "endpoint"

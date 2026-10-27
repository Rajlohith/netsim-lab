from app.config import Settings


def test_defaults_are_safe():
    s = Settings(_env_file=None)
    assert s.api_host == "127.0.0.1"
    assert s.dry_run is True
    assert s.netns_prefix == "nsl-"


def test_env_override(monkeypatch):
    monkeypatch.setenv("NETSIM_API_PORT", "9001")
    assert Settings(_env_file=None).api_port == 9001

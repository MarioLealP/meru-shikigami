import pytest

from shikigami.config import Settings


def test_kubernetes_service_links_are_ignored(monkeypatch: pytest.MonkeyPatch):
    """The variables Kubernetes adds for a Service named shikigami share the settings' prefix (2026-10-30 crash)."""
    links = {
        "SHIKIGAMI_PORT": "tcp://10.43.23.251:80",
        "SHIKIGAMI_PORT_80_TCP": "tcp://10.43.23.251:80",
        "SHIKIGAMI_PORT_80_TCP_ADDR": "10.43.23.251",
        "SHIKIGAMI_PORT_80_TCP_PORT": "80",
        "SHIKIGAMI_PORT_80_TCP_PROTO": "tcp",
        "SHIKIGAMI_SERVICE_HOST": "10.43.23.251",
        "SHIKIGAMI_SERVICE_PORT": "80",
        "SHIKIGAMI_SERVICE_PORT_HTTP": "80",
        "SHIKIGAMI_AUTHENTIK_PASSWORD": "x",
        "SHIKIGAMI_GITEA_TOKEN": "x",
    }
    for name, value in links.items():
        monkeypatch.setenv(name, value)
    assert Settings().http_port == 8080

"""Input fetching rules: scheme, host allowlist, credentials, size limit, network errors."""

from __future__ import annotations

import httpx
import pytest

from fabrication_prep.inputs import InputError, check_url, fetch
from fabrication_prep.settings import Settings


def settings(**kw) -> Settings:
    base = {
        "fabrication_prep_env": "production",
        "jwks_path": "",
        "public_base_url": "https://api.example",
        "input_allowed_hosts": "api.yantra4d.example",
    }
    return Settings(**{**base, **kw})


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://api.yantra4d.example/a.stl", "scheme"),
        ("https://user:pw@api.yantra4d.example/a.stl", "credentials"),
        ("https:///a.stl", "no host"),
        ("https://evil.example/a.stl", "not an allowed"),
        ("https://[::1", "not a URL"),
    ],
)
def test_production_url_rules(url, reason):
    assert reason in (check_url(url, settings()) or "")


def test_allowed_production_url_and_empty_allowlist():
    assert check_url("https://API.yantra4d.example/static/a.stl", settings()) is None
    assert "empty" in check_url("https://api.yantra4d.example/a.stl", settings(input_allowed_hosts=""))
    local = settings(fabrication_prep_env="local", input_allowed_hosts="")
    assert check_url("http://localhost:5000/a.stl", local) is None


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_size_limit_and_network_errors(tmp_path):
    s = settings()
    big = client(lambda r: httpx.Response(200, content=b"x" * 2048))
    with pytest.raises(InputError) as exc:
        fetch(big, "https://api.yantra4d.example/a.stl", tmp_path / "a", "0" * 64, 1024, s)
    assert exc.value.code == "input_too_large" and not exc.value.transient

    def broken(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(InputError) as exc:
        fetch(client(broken), "https://api.yantra4d.example/a.stl", tmp_path / "a", "0" * 64, 1024, s)
    assert exc.value.code == "input_unavailable" and exc.value.transient
    with pytest.raises(InputError) as exc:
        fetch(big, "https://evil.example/a.stl", tmp_path / "a", "0" * 64, 1024, s)
    assert exc.value.code == "input_url_not_allowed"
    busy = client(lambda r: httpx.Response(429))
    with pytest.raises(InputError) as exc:
        fetch(busy, "https://api.yantra4d.example/a.stl", tmp_path / "a", "0" * 64, 1024, s)
    assert exc.value.transient

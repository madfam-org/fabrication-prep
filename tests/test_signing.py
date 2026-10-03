"""Signed artifact URLs: binding to the sha256, expiry, tampering, key rotation."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from fabrication_prep.signing import SignatureInvalid, sign, verify

SHA = "a" * 64
OTHER = "b" * 64
KEYS = [("k2", b"k" * 32), ("k1", b"o" * 32)]


def parts(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_sign_and_verify_roundtrip():
    signed = sign("https://api.example/", SHA, KEYS, 900, now=1000)
    assert signed.url.startswith(f"https://api.example/v1/artifacts/{SHA}?")
    assert signed.expires_at == 1900
    q = parts(signed.url)
    assert q["kid"] == "k2"
    verify(SHA, q["exp"], q["kid"], q["sig"], KEYS, 900, now=1500)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda q: {**q, "sig": "0" * 64}, "signature_invalid"),
        (lambda q: {**q, "exp": str(int(q["exp"]) + 60)}, "signature_invalid"),
        (lambda q: {**q, "kid": "unknown"}, "signature_invalid"),
        (lambda q: {**q, "exp": "12a"}, "signature_invalid"),
        (lambda q: {**q, "sig": ""}, "signature_missing"),
    ],
)
def test_tampering_is_refused(mutate, code):
    q = mutate(parts(sign("https://x", SHA, KEYS, 900, now=1000).url))
    with pytest.raises(SignatureInvalid) as exc:
        verify(SHA, q["exp"], q["kid"], q["sig"], KEYS, 900, now=1100)
    assert exc.value.code == code


def test_url_is_bound_to_its_artifact():
    q = parts(sign("https://x", SHA, KEYS, 900, now=1000).url)
    with pytest.raises(SignatureInvalid):
        verify(OTHER, q["exp"], q["kid"], q["sig"], KEYS, 900, now=1100)


def test_expired_url():
    q = parts(sign("https://x", SHA, KEYS, 900, now=1000).url)
    with pytest.raises(SignatureInvalid) as exc:
        verify(SHA, q["exp"], q["kid"], q["sig"], KEYS, 900, now=1901)
    assert exc.value.code == "signature_expired"


def test_far_future_expiry_is_refused_even_if_signed():
    q = parts(sign("https://x", SHA, KEYS, 86400, now=1000).url)  # signed with a longer TTL than allowed
    with pytest.raises(SignatureInvalid):
        verify(SHA, q["exp"], q["kid"], q["sig"], KEYS, 900, now=1000)


def test_rotation_old_key_still_verifies_until_removed():
    q = parts(sign("https://x", SHA, [KEYS[1]], 900, now=1000).url)  # issued before rotation
    verify(SHA, q["exp"], q["kid"], q["sig"], KEYS, 900, now=1100)
    with pytest.raises(SignatureInvalid):
        verify(SHA, q["exp"], q["kid"], q["sig"], [KEYS[0]], 900, now=1100)

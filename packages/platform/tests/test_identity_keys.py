"""citadel_platform.identity.keys -- the session-signing key lifecycle:
generate, load from the environment, and refuse (loudly, distinctly from
"unset") when the environment variable is set but unusable.
"""

from __future__ import annotations

import base64
import warnings

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from citadel_platform.identity.keys import (
    ENV_VAR,
    SessionSigningKeyError,
    generate_key_material,
    load_session_signing_key,
)


def _public_bytes(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes_raw()


def test_generate_key_material_decodes_to_32_bytes():
    material = generate_key_material()
    decoded = base64.b64decode(material, validate=True)
    assert len(decoded) == 32


def test_generate_key_material_is_fresh_every_call():
    assert generate_key_material() != generate_key_material()


def test_a_generated_key_round_trips_through_load():
    material = generate_key_material()
    loaded = load_session_signing_key({ENV_VAR: material})

    # Reconstructing from the same raw bytes must yield the same keypair --
    # compared via public key bytes, since Ed25519PrivateKey defines no
    # equality of its own.
    expected = Ed25519PrivateKey.from_private_bytes(base64.b64decode(material))
    assert _public_bytes(loaded) == _public_bytes(expected)


def test_missing_env_var_warns_and_returns_a_usable_random_key():
    with pytest.warns(UserWarning, match=ENV_VAR):
        key = load_session_signing_key({})

    assert isinstance(key, Ed25519PrivateKey)


def test_two_calls_with_no_env_var_generate_different_keys():
    """Each fallback is a fresh, unpredictable key -- never a fixed
    placeholder an attacker could rely on."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        first = load_session_signing_key({})
        second = load_session_signing_key({})

    assert _public_bytes(first) != _public_bytes(second)


def test_non_base64_value_is_refused_distinctly_from_unset():
    with pytest.raises(SessionSigningKeyError, match="base64"):
        load_session_signing_key({ENV_VAR: "not-valid-base64!!!"})


def test_wrong_length_value_is_refused():
    too_short = base64.b64encode(b"\x00" * 16).decode("ascii")
    with pytest.raises(SessionSigningKeyError, match="32"):
        load_session_signing_key({ENV_VAR: too_short})


def test_real_os_environ_is_used_when_env_argument_is_omitted(monkeypatch):
    material = generate_key_material()
    monkeypatch.setenv(ENV_VAR, material)

    key = load_session_signing_key()

    expected = Ed25519PrivateKey.from_private_bytes(base64.b64decode(material))
    assert _public_bytes(key) == _public_bytes(expected)

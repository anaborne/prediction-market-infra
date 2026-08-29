"""Tests for `KalshiRequestSigner`.

Kalshi's RSA-PSS signing scheme uses a randomized salt (`PSS.DIGEST_LENGTH`), so signing the same
message twice with the same key produces two different, both-valid signatures, so there is no fixed
expected-signature test vector to byte-match against (confirmed against `docs.kalshi.com`'s
authenticated-requests and websockets quick-start guides, matching `ENGINEERING.md`'s "Kalshi API v2
auth
spec"). These tests instead verify each produced signature against an independently constructed
RSA-PSS verifier using the exact published parameters (SHA-256, MGF1-SHA256, salt length = digest
length) over the exact published message construction. That fails just as surely as a fixed vector
would if the message format, hash, MGF, or salt length were wrong. It checks more than whether
`sign` is self-consistent with itself.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1
from cryptography.hazmat.primitives.asymmetric.ec import generate_private_key as generate_ec_key

from kalshi_bot.auth.signer import KalshiRequestSigner


def _write_rsa_key(path: Path) -> rsa.RSAPublicKey:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return private_key.public_key()


def _verify(public_key: rsa.RSAPublicKey, message: str, signature_b64: str) -> None:
    public_key.verify(
        base64.b64decode(signature_b64),
        message.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


@pytest.fixture
def signer_and_public_key(tmp_path: Path) -> tuple[KalshiRequestSigner, rsa.RSAPublicKey]:
    key_path = tmp_path / "test_key.pem"
    public_key = _write_rsa_key(key_path)
    return KalshiRequestSigner(key_path), public_key


def test_sign_produces_a_signature_valid_over_the_spec_rest_message(
    signer_and_public_key: tuple[KalshiRequestSigner, rsa.RSAPublicKey],
) -> None:
    signer, public_key = signer_and_public_key

    signature = signer.sign("1703123456789", "GET", "/trade-api/v2/portfolio/orders")

    _verify(public_key, "1703123456789GET/trade-api/v2/portfolio/orders", signature)


def test_sign_concatenates_with_no_separators_in_timestamp_method_path_order(
    signer_and_public_key: tuple[KalshiRequestSigner, rsa.RSAPublicKey],
) -> None:
    signer, public_key = signer_and_public_key

    signature = signer.sign("1703123456789", "POST", "/trade-api/v2/portfolio/orders")

    # Any other concatenation order or separator would fail this exact-message verification.
    _verify(public_key, "1703123456789POST/trade-api/v2/portfolio/orders", signature)


@pytest.mark.parametrize(
    "tampered_message",
    [
        "1703123456789POST/trade-api/v2/portfolio/orders",  # wrong method
        "1703123456789GET/trade-api/v2/portfolio/balance",  # wrong path
        "1703123456790GET/trade-api/v2/portfolio/orders",  # wrong timestamp
    ],
)
def test_sign_signature_does_not_verify_against_a_tampered_message(
    signer_and_public_key: tuple[KalshiRequestSigner, rsa.RSAPublicKey],
    tampered_message: str,
) -> None:
    signer, public_key = signer_and_public_key

    signature = signer.sign("1703123456789", "GET", "/trade-api/v2/portfolio/orders")

    with pytest.raises(InvalidSignature):
        _verify(public_key, tampered_message, signature)


def test_sign_websocket_auth_signs_the_fixed_ws_message(
    signer_and_public_key: tuple[KalshiRequestSigner, rsa.RSAPublicKey],
) -> None:
    signer, public_key = signer_and_public_key

    signature = signer.sign_websocket_auth("1703123456789")

    _verify(public_key, "1703123456789GET/trade-api/ws/v2", signature)


def test_sign_websocket_auth_signature_does_not_verify_against_a_rest_style_message(
    signer_and_public_key: tuple[KalshiRequestSigner, rsa.RSAPublicKey],
) -> None:
    signer, public_key = signer_and_public_key

    signature = signer.sign_websocket_auth("1703123456789")

    with pytest.raises(InvalidSignature):
        _verify(public_key, "1703123456789GET/trade-api/v2/portfolio/orders", signature)


def test_init_rejects_a_non_rsa_private_key(tmp_path: Path) -> None:
    key_path = tmp_path / "ec_key.pem"
    ec_key = generate_ec_key(SECP256R1())
    key_path.write_bytes(
        ec_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    with pytest.raises(TypeError):
        KalshiRequestSigner(key_path)

"""Kalshi request signing.

Kalshi authenticates each request using an RSA-PSS signature over a message constructed as:

    message = timestamp + method + path_without_query

Where:
    - `timestamp` is the millisecond-precision Unix timestamp string sent in the
      `KALSHI-ACCESS-TIMESTAMP` header, and must be the exact same string used to build the
      message as is sent in that header.
    - `method` is the HTTP method in uppercase (e.g. "GET", "POST"), with no surrounding
      whitespace.
    - `path_without_query` is the request path only. Scheme, host, and any `?query=string`
      component must NOT be included. It must start with `/`.

The three components are concatenated directly with no separator characters between them. The
resulting message is signed with RSA-PSS (MGF1, SHA-256) using the account's private key, and the
signature is base64-encoded for transport in the `KALSHI-ACCESS-SIGNATURE` header.

This exact construction (concatenation order, no separators, path excludes query string) is
intentionally documented here in full so that the implementation in `KalshiRequestSigner.sign`
can be written later against verified test vectors with zero ambiguity about message format. See
`docs/GUIDE.md` for why this is hand-written rather than imported from
Kalshi's official SDK.
"""

from __future__ import annotations

import base64
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

# Fixed WebSocket auth message components. See module docstring and `ENGINEERING.md`'s "Kalshi API
# v2
# auth spec". Distinct from any REST request path; never derived from a caller-supplied path.
_WS_AUTH_METHOD = "GET"
_WS_AUTH_PATH = "/trade-api/ws/v2"


class KalshiRequestSigner:
    """Produces Kalshi API request signatures using an RSA-PSS private key.

    Instances are constructed from a private key location and expose `sign` (REST requests) and
    `sign_websocket_auth` (the WebSocket connection handshake), both implementing the message
    construction rules documented at module level.
    """

    def __init__(self, private_key_path: Path) -> None:
        """Load the RSA private key for later use by `sign`/`sign_websocket_auth`.

        Args:
            private_key_path: Filesystem path to a PEM-encoded RSA private key.

        Raises:
            TypeError: If the key at `private_key_path` is not an RSA private key.
        """
        private_key = serialization.load_pem_private_key(
            private_key_path.read_bytes(), password=None
        )
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise TypeError(f"{private_key_path} does not contain an RSA private key")
        self._private_key: rsa.RSAPrivateKey = private_key

    def sign(self, timestamp: str, method: str, path: str) -> str:
        """Sign a request and return the base64-encoded RSA-PSS signature.

        Args:
            timestamp: Millisecond-precision Unix timestamp string, identical to the value that
                will be sent in the `KALSHI-ACCESS-TIMESTAMP` header.
            method: HTTP method in uppercase (e.g. "GET", "POST").
            path: Request path only, excluding scheme, host, and query string.

        Returns:
            Base64-encoded RSA-PSS signature of `timestamp + method + path`.
        """
        return self._sign_message(f"{timestamp}{method}{path}")

    def sign_websocket_auth(self, timestamp: str) -> str:
        """Sign the fixed WebSocket auth message and return the base64-encoded signature.

        This signs `timestamp + "GET" + "/trade-api/ws/v2"`, a distinct, fixed message from any
        REST request path. It is exposed as its own method (instead of a caller-supplied `path`
        argument to `sign`) so callers cannot accidentally sign the wrong message for a WebSocket
        handshake.

        Args:
            timestamp: Millisecond-precision Unix timestamp string, identical to the value that
                will be sent in the `KALSHI-ACCESS-TIMESTAMP` header.

        Returns:
            Base64-encoded RSA-PSS signature of `timestamp + "GET" + "/trade-api/ws/v2"`.
        """
        return self._sign_message(f"{timestamp}{_WS_AUTH_METHOD}{_WS_AUTH_PATH}")

    def _sign_message(self, message: str) -> str:
        signature = self._private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

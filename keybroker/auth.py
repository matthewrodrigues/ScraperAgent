"""Broker token authentication.

Tokens carry 256 bits of entropy, so SHA-256 plus an indexed lookup is both
faster and safer than the constant-time compare api/auth.py needs: hashing
destroys any prefix relationship, so there is no timing oracle to exploit.
Storing only the hash also means a leaked broker.db holds nothing spendable.
"""

import hashlib
import secrets
from typing import Any, Mapping

from keybroker import db


TOKEN_PREFIX = "sa_"


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def extract_token(headers: Mapping[str, str], vendor: str) -> str | None:
    """Pull the friend's token from whichever header that vendor's SDK uses.

    The Anthropic SDK sends `X-Api-Key`; the Apify SDK sends
    `Authorization: Bearer`. Reading the wrong one would let a caller
    authenticate with a header the real client never sets.
    """
    if vendor == "anthropic":
        return headers.get("x-api-key") or None
    authorization = headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value
    return None


def friend_for_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    return db.get_friend_by_token_hash(hash_token(token))

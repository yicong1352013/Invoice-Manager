"""Tests for authentication: password hashing, JWT lifecycle, and authorization guards.

Business invariants protected here:
- A password must only verify against its own hash.
- Tokens must expire and must reject tampering.
- The system must never end up without an enabled admin.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt as pyjwt
import pytest

from app.auth import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
    would_remove_last_admin,
)
from app.models.user import UserRole


def test_password_hash_roundtrip():
    hashed = hash_password("s3cret-pass")
    assert hashed != "s3cret-pass"
    assert verify_password("s3cret-pass", hashed)


def test_password_hash_rejects_wrong_password():
    hashed = hash_password("s3cret-pass")
    assert not verify_password("wrong", hashed)
    assert not verify_password("", hashed)


def test_password_hash_is_salted():
    # Same password must produce different hashes (salted)
    assert hash_password("s3cret-pass") != hash_password("s3cret-pass")


def test_jwt_roundtrip():
    token = create_access_token("alice", "user")
    payload = decode_access_token(token)
    assert payload["sub"] == "alice"
    assert payload["role"] == "user"


def test_jwt_expiry_enforced(monkeypatch):
    monkeypatch.setattr("app.auth._get_secret_key", lambda: "test-secret")

    # Sanity: fresh token decodes
    assert decode_access_token(create_access_token("alice", "user"))["sub"] == "alice"

    expired_token = pyjwt.encode(
        {
            "sub": "alice",
            "role": "user",
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        },
        "test-secret",
        algorithm="HS256",
    )
    with pytest.raises(pyjwt.PyJWTError):
        decode_access_token(expired_token)


def test_jwt_tampering_rejected():
    token = create_access_token("alice", "user")
    tampered = token[:-4] + ("AAAA" if token[-4:] != "AAAA" else "BBBB")
    with pytest.raises(pyjwt.PyJWTError):
        decode_access_token(tampered)


# --- last-admin guard -------------------------------------------------------


def _admin_user(enabled=True):
    return SimpleNamespace(role=UserRole.ADMIN, enabled=enabled)


def test_last_admin_cannot_be_removed():
    assert would_remove_last_admin(
        target_role=UserRole.ADMIN,
        target_enabled=True,
        action_removes_admin=True,
        other_enabled_admins=0,
    )


def test_admin_with_peers_can_be_removed():
    assert not would_remove_last_admin(
        target_role=UserRole.ADMIN,
        target_enabled=True,
        action_removes_admin=True,
        other_enabled_admins=1,
    )


def test_regular_user_unrestricted_by_admin_guard():
    assert not would_remove_last_admin(
        target_role=UserRole.USER,
        target_enabled=True,
        action_removes_admin=True,
        other_enabled_admins=0,
    )


def test_already_disabled_admin_not_blocking():
    # The target is not an enabled admin, so removing them changes nothing
    assert not would_remove_last_admin(
        target_role=UserRole.ADMIN,
        target_enabled=False,
        action_removes_admin=True,
        other_enabled_admins=0,
    )


def test_non_admin_action_not_blocking():
    assert not would_remove_last_admin(
        target_role=UserRole.ADMIN,
        target_enabled=True,
        action_removes_admin=False,
        other_enabled_admins=0,
    )

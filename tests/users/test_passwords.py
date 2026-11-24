"""Tests for stdlib password hashing and session-token helpers."""

from __future__ import annotations

import pytest

from olano.users.passwords import (
    hash_password,
    hash_token,
    new_session_token,
    verify_password,
)


def test_hash_verify_round_trip():
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)


def test_wrong_password_rejected():
    stored = hash_password("s3cret")
    assert not verify_password("s3cre7", stored)
    assert not verify_password("", stored)


def test_verify_against_garbage_is_false():
    assert not verify_password("x", "not-a-valid-hash")
    assert not verify_password("x", "")
    assert not verify_password("x", "pbkdf2_sha256$abc$def")  # malformed


def test_hash_is_salted_and_self_describing():
    a = hash_password("same")
    b = hash_password("same")
    assert a != b  # random per-password salt
    assert a.startswith("pbkdf2_sha256$")
    assert verify_password("same", a)
    assert verify_password("same", b)


def test_empty_password_rejected_on_hash():
    with pytest.raises(ValueError):
        hash_password("")


def test_token_mint_and_hash():
    t1 = new_session_token()
    t2 = new_session_token()
    assert t1 != t2
    assert hash_token(t1) == hash_token(t1)  # deterministic
    assert hash_token(t1) != hash_token(t2)
    assert hash_token(t1) != t1  # stored value is not the raw token

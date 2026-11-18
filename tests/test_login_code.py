"""Google sign-in code exchange helpers.

The Google callback used to put the 30-day session JWT in the redirect's URL
hash. It now hands a single-use code bound to a challenge the dashboard made
in the same tab; the database round trip is exercised against production
after deploy, the rules a request can trip over are pinned here.
"""
import base64
import hashlib
import secrets

import pytest


class TestLoginCodeHelpers:
    @pytest.fixture(autouse=True)
    def _auth(self):
        pytest.importorskip("jwt")
        from cloud import auth
        self.auth = auth

    def test_challenge_is_base64url_sha256_of_the_verifier(self):
        verifier = secrets.token_urlsafe(32)
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        assert self.auth.challenge_for(verifier) == expected
        assert self.auth.valid_challenge(expected)

    def test_challenge_shape_is_checked(self):
        for bad in (None, "", "short", "x" * 44, "a" * 42 + "=", "a" * 42 + "/"):
            assert not self.auth.valid_challenge(bad), bad

    def test_exchange_rejects_malformed_input_before_the_database(self):
        from fastapi import HTTPException
        import asyncio
        for code, verifier in (("", "v" * 43), ("c" * 201, "v" * 43), ("abc", "short")):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(self.auth.exchange_login_code(
                    self.auth.LoginExchangeRequest(code=code, verifier=verifier)))
            assert exc.value.status_code == 400


class TestLoginCodeTable:
    def test_is_erased_with_the_account(self):
        from cloud import account
        from cloud.models import LoginCode
        assert LoginCode in account.USER_OWNED_TABLES


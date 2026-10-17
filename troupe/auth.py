"""Owner login: a password (scrypt-hashed) that exchanges for 30-day session tokens.

The admin token still works (the desktop app and scripts use it), but people normally sign in with
the password set by `troupe password` on the server. Failed logins are rate-limited per client
address, and every attempt is written to the audit log.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time

from .db import DB

SESSION_DAYS = 30
MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
MIN_PASSWORD_LENGTH = 10
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, db: DB, admin_token: str):
        self.db = db
        self.admin_token = admin_token
        self.failures: dict[str, list[float]] = {}

    # --- password ---------------------------------------------------------------------------------
    @property
    def password_set(self) -> bool:
        return bool(self.db.kv_get("auth:password"))

    def set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"use at least {MIN_PASSWORD_LENGTH} characters")
        salt = secrets.token_bytes(16)
        rec = {**_SCRYPT, "salt": salt.hex(), "hash": _derive(password, salt, **_SCRYPT).hex()}
        self.db.kv_set("auth:password", json.dumps(rec))
        self.revoke_all_sessions()
        self.db.audit("auth", "password_set")

    def check_password(self, password: str) -> bool:
        raw = self.db.kv_get("auth:password")
        if not raw:
            return False
        rec = json.loads(raw)
        got = _derive(password, bytes.fromhex(rec["salt"]), rec["n"], rec["r"], rec["p"])
        return hmac.compare_digest(got.hex(), rec["hash"])

    # --- rate limiting ------------------------------------------------------------------------------
    def locked_for(self, client: str) -> int:
        now = time.time()
        recent = [t for t in self.failures.get(client, []) if now - t < LOCKOUT_SECONDS]
        self.failures[client] = recent
        if len(recent) >= MAX_FAILURES:
            return int(LOCKOUT_SECONDS - (now - recent[0])) + 1
        return 0

    def login(self, password: str, client: str) -> str:
        """Returns a session token. Raises PermissionError (bad password) or TimeoutError (locked)."""
        wait = self.locked_for(client)
        if wait:
            self.db.audit("auth", "login_locked", {"client": client})
            raise TimeoutError(wait)
        if not self.check_password(password):
            self.failures.setdefault(client, []).append(time.time())
            self.db.audit("auth", "login_failed", {"client": client})
            raise PermissionError("wrong password")
        self.failures.pop(client, None)
        token = secrets.token_urlsafe(32)
        self.db.kv_set(f"session:{_sha(token)}", json.dumps({"exp": time.time() + SESSION_DAYS * 86400,
                                                             "created": time.time(), "client": client}))
        self.db.audit("auth", "login_ok", {"client": client})
        return token

    # --- sessions -------------------------------------------------------------------------------------
    def valid(self, token: str) -> bool:
        if not token:
            return False
        if hmac.compare_digest(token, self.admin_token):
            return True
        raw = self.db.kv_get(f"session:{_sha(token)}")
        if not raw:
            return False
        if json.loads(raw)["exp"] < time.time():
            self.db.execute("DELETE FROM kv WHERE key = ?", (f"session:{_sha(token)}",))
            return False
        return True

    def logout(self, token: str) -> None:
        self.db.execute("DELETE FROM kv WHERE key = ?", (f"session:{_sha(token)}",))

    def revoke_all_sessions(self) -> None:
        self.db.execute("DELETE FROM kv WHERE key LIKE 'session:%'")

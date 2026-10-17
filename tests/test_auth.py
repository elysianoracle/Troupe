"""Password login, sessions and lockout, through the real HTTP API."""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from troupe.app import App
from troupe.config import load_settings
from troupe.llm import MockProvider
from troupe.surfaces.web import build_api

PW = "correct horse battery"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TROUPE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TROUPE_SANDBOX", "local")
    monkeypatch.setenv("TROUPE_LLM_PROVIDER", "mock")
    monkeypatch.setenv("TROUPE_ENV_FILE", str(tmp_path / "none.env"))
    app = App(load_settings(), llm=MockProvider())
    with TestClient(build_api(app)) as c:
        yield app, c


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


def test_no_password_yet(env):
    app, c = env
    assert c.get("/api/auth-info").json() == {"password_set": False}
    r = c.post("/api/login", json={"password": "anything at all"})
    assert r.status_code == 400 and "troupe-password" in r.json()["detail"]
    assert c.get("/api/state").status_code == 401
    assert c.get("/api/docs").status_code == 404 and c.get("/openapi.json").status_code == 404


def test_password_login_session_and_logout(env):
    app, c = env
    with pytest.raises(ValueError):
        app.auth.set_password("short")
    app.auth.set_password(PW)
    assert c.get("/api/auth-info").json() == {"password_set": True}
    assert c.post("/api/login", json={"password": "wrong password"}).status_code == 401
    r = c.post("/api/login", json={"password": PW})
    assert r.status_code == 200
    tok = r.json()["token"]
    assert tok != app.settings.admin_token
    assert c.get("/api/state", headers=bearer(tok)).status_code == 200
    with c.websocket_connect(f"/ws?token={tok}") as ws:
        assert ws  # accepted
    assert c.post("/api/logout", headers=bearer(tok)).status_code == 200
    assert c.get("/api/state", headers=bearer(tok)).status_code == 401
    # The stored password is a salted scrypt hash, never the password itself.
    raw = app.db.kv_get("auth:password")
    assert PW not in raw and json.loads(raw)["n"] >= 2 ** 14


def test_admin_token_still_works(env):
    app, c = env
    assert c.get("/api/state", headers=bearer(app.settings.admin_token)).status_code == 200


def test_lockout_after_five_wrong_passwords(env):
    app, c = env
    app.auth.set_password(PW)
    for _ in range(5):
        assert c.post("/api/login", json={"password": "guess guess guess"}).status_code == 401
    r = c.post("/api/login", json={"password": PW})  # even the right password is refused while locked
    assert r.status_code == 429 and "Try again" in r.json()["detail"]
    kinds = [a["name"] for a in app.db.all("SELECT name FROM audit WHERE kind = 'auth'")]
    assert kinds.count("login_failed") == 5 and "login_locked" in kinds
    # Lockout expires.
    app.auth.failures = {k: [t - 16 * 60 for t in v] for k, v in app.auth.failures.items()}
    assert c.post("/api/login", json={"password": PW}).status_code == 200


def test_spoofed_forwarded_for_cannot_dodge_lockout(env):
    app, c = env
    app.auth.set_password(PW)
    for i in range(5):  # the test client is not localhost, so X-Forwarded-For must be ignored
        c.post("/api/login", json={"password": "nope nope nope"}, headers={"X-Forwarded-For": f"10.0.0.{i}"})
    assert c.post("/api/login", json={"password": PW}, headers={"X-Forwarded-For": "10.9.9.9"}).status_code == 429


def test_password_change_signs_everyone_out_and_sessions_expire(env):
    app, c = env
    app.auth.set_password(PW)
    tok = c.post("/api/login", json={"password": PW}).json()["token"]
    app.auth.set_password("another long password")
    assert c.get("/api/state", headers=bearer(tok)).status_code == 401
    tok = c.post("/api/login", json={"password": "another long password"}).json()["token"]
    key = next(r["key"] for r in app.db.all("SELECT key FROM kv WHERE key LIKE 'session:%'"))
    rec = json.loads(app.db.kv_get(key)); rec["exp"] = time.time() - 1
    app.db.kv_set(key, json.dumps(rec))
    assert c.get("/api/state", headers=bearer(tok)).status_code == 401

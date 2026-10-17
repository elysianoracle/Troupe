"""First-run setup wizard API: password, model choice (checked before saving), teams, channels."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from troupe import models as model_cfg
from troupe.app import App
from troupe.config import load_settings
from troupe.llm import OpenAICompatProvider
from troupe.surfaces.web import build_api
from troupe.teams import TEAMS


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TROUPE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TROUPE_SANDBOX", "local")
    monkeypatch.setenv("TROUPE_LLM_PROVIDER", "mock")
    monkeypatch.setenv("TROUPE_ENV_FILE", str(tmp_path / "none.env"))

    async def fake_list(preset, base_url, api_key):
        if api_key != "sk-good":
            raise ValueError("the provider did not accept that key")
        return ["qwen3.7-plus", "qwen3.8-max"]
    monkeypatch.setattr(model_cfg, "list_models", fake_list)
    app = App(load_settings())
    with TestClient(build_api(app)) as c:
        yield app, c, {"Authorization": f"Bearer {app.settings.admin_token}"}


def test_fresh_install_needs_setup(env):
    app, c, h = env
    st = c.get("/api/setup", headers=h).json()
    assert st["done"] is False and st["agents"] == 0 and st["password_set"] is False
    assert {"anthropic", "qwen", "deepseek", "kimi", "openrouter", "ollama", "custom"} <= set(st["presets"])
    assert set(st["teams"]) == set(TEAMS)
    assert c.get("/api/state", headers=h).json()["setup_done"] is False
    assert c.get("/api/setup").status_code == 401  # the wizard needs the admin token


def test_password_step(env):
    app, c, h = env
    assert c.post("/api/setup/password", headers=h, json={"password": "short"}).status_code == 400
    assert c.post("/api/setup/password", headers=h, json={"password": "a long enough phrase"}).status_code == 200
    assert c.post("/api/login", json={"password": "a long enough phrase"}).status_code == 200


def test_model_is_checked_before_saving_and_switches_without_restart(env):
    app, c, h = env
    bad = c.post("/api/setup/models", headers=h, json={"preset": "qwen", "api_key": "sk-bad"})
    assert bad.status_code == 400 and "did not accept" in bad.json()["detail"]
    ok = c.post("/api/setup/models", headers=h, json={"preset": "qwen", "api_key": "sk-good"}).json()
    assert ok["models"] == ["qwen3.7-plus", "qwen3.8-max"]
    r = c.post("/api/setup/model", headers=h, json={"preset": "qwen", "api_key": "sk-good", "model": "not-listed"})
    assert r.status_code == 400
    r = c.post("/api/setup/model", headers=h, json={"preset": "qwen", "api_key": "sk-good", "model": "qwen3.8-max"})
    assert r.status_code == 200 and r.json()["model"]["model"] == "qwen3.8-max"
    # Live: the provider and default model changed, the key is in the encrypted vault, not the database config.
    assert isinstance(app.llm, OpenAICompatProvider) and app.settings.default_model == "qwen3.8-max"
    assert app.llm.base_url == model_cfg.PRESETS["qwen"]["base_url"]
    assert app.vault.get("llm_api_key") == "sk-good" and "sk-good" not in app.db.kv_get("config:llm")
    # Re-saving with the saved key works without typing it again.
    r = c.post("/api/setup/model", headers=h, json={"preset": "qwen", "use_saved_key": True, "model": "qwen3.7-plus"})
    assert r.status_code == 200 and app.settings.default_model == "qwen3.7-plus"


def test_team_presets_and_finish(env):
    app, c, h = env
    r = c.post("/api/setup/team", headers=h, json={"preset": "founder"}).json()
    names = sorted(a["name"] for a in app.db.all("SELECT name FROM agents"))
    assert names == ["Analyst", "Chief", "Courier", "Scribe"]
    room = app.db.one("SELECT * FROM rooms WHERE id = ?", (r["room_id"],))
    assert room["name"] == "The Office" and room["lead_agent_id"] == r["agents"][0]
    c.post("/api/setup/team", headers=h, json={"preset": "founder"})  # idempotent
    assert len(app.db.all("SELECT id FROM agents")) == 4
    assert c.post("/api/setup/team", headers=h, json={"preset": "nope"}).status_code == 400
    assert c.post("/api/setup/finish", headers=h).status_code == 200
    assert c.get("/api/setup", headers=h).json()["done"] is True


def test_finish_without_choosing_creates_the_personal_team(env):
    app, c, h = env
    c.post("/api/setup/finish", headers=h)
    assert sorted(a["name"] for a in app.db.all("SELECT name FROM agents")) == ["Chief", "Courier", "Scout"]


def test_channels_store_keys_in_the_vault(env):
    app, c, h = env
    r = c.post("/api/setup/channels", headers=h, json={"agentmail_api_key": "am_test"}).json()
    assert r == {"agentmail": True} and app.vault.get("agentmail_api_key") == "am_test"
    assert app.email.agentmail.enabled


def test_custom_prices_feed_budgets(env):
    app, c, h = env
    assert c.post("/api/settings/prices", headers=h, json={"qwen3.8-max": ["x", 1]}).status_code == 400
    c.post("/api/settings/prices", headers=h, json={"qwen3.8-max": [1.2, 6]})
    from troupe.llm import estimate_cost
    assert round(estimate_cost("qwen3.8-max", 1_000_000, 1_000_000, 0, 0), 2) == 7.2

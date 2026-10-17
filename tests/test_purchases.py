"""Manual purchases: the agent asks, the owner approves within budget, then pays themselves."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from troupe.app import App
from troupe.config import load_settings
from troupe.db import DB
from troupe.llm import MockProvider


def text_of(m):
    c = m["content"]
    return c if isinstance(c, str) else "\n".join(b.get("text", "") if b.get("type") == "text" else json.dumps(b.get("content", "")) for b in c)


@pytest.fixture
async def app(tmp_path, monkeypatch):
    monkeypatch.setenv("TROUPE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TROUPE_SANDBOX", "local")
    monkeypatch.setenv("TROUPE_LLM_PROVIDER", "mock")
    monkeypatch.setenv("TROUPE_ENV_FILE", str(tmp_path / "none.env"))
    a = App(load_settings(), llm=MockProvider())
    d = asyncio.create_task(a.orch.dispatcher())
    yield a
    d.cancel()


async def settle(app):
    for _ in range(200):
        await asyncio.sleep(0.05)
        if not app.db.one("SELECT COUNT(*) n FROM tasks WHERE status IN ('queued','running')")["n"] and not app.orch.running:
            return


def buyer(amount):
    return lambda s, m, t: ({"content": [{"type": "tool_use", "id": "p1", "name": "request_purchase", "input": {
        "amount_usd": amount, "merchant": "Example Shop", "description": "USB-C cable",
        "url": "https://shop.example.com/cable"}}], "stop_reason": "tool_use"}
        if "Owner says" in text_of(m[-1]) else {"content": [{"type": "text", "text": "Here's the link."}], "stop_reason": "end_turn"})


async def test_no_budget_no_purchase_tool(app):
    agent = app.create_agent("Courier")
    assert "request_purchase" not in {t.name for t in app.tools_for(agent)}
    assert not {"pay_with_card", "check_purchase"} & {t.name for t in app.tools_for(app.create_agent("Scout", spend_budget=50))}


async def test_manual_purchase_needs_approval_and_counts_against_budget(app):
    app.llm.handler = buyer(19.99)
    agent = app.create_agent("Courier", spend_budget=50)
    await app.orch.user_message(app.dm_room(agent["id"])["id"], "buy a cable")
    await settle(app)
    assert app.db.one("SELECT COUNT(*) n FROM ledger")["n"] == 0  # nothing recorded before approval
    app.decide_approval(app.db.one("SELECT id FROM approvals")["id"], True)
    await settle(app)
    row = app.db.one("SELECT * FROM ledger")
    assert row["status"] == "approved" and row["url"] == "https://shop.example.com/cable"
    assert round(app.guard.money_spent_this_week(agent["id"]), 2) == 19.99
    assert "Payment is manual" in json.dumps(app.llm.calls[-1]["messages"][-1])


async def test_over_budget_refused_without_asking(app):
    app.llm.handler = buyer(80)
    agent = app.create_agent("Courier", spend_budget=50)
    await app.orch.user_message(app.dm_room(agent["id"])["id"], "buy headphones")
    await settle(app)
    assert app.db.one("SELECT COUNT(*) n FROM approvals")["n"] == 0
    assert "weekly spending budget" in json.dumps(app.llm.calls[-1]["messages"][-1])


def test_card_columns_are_dropped_from_older_databases(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, agent_id TEXT NOT NULL, "
              "task_id TEXT NOT NULL, amount_usd REAL NOT NULL, merchant TEXT NOT NULL, description TEXT NOT NULL, "
              "status TEXT NOT NULL, url TEXT NOT NULL DEFAULT '', domain TEXT NOT NULL DEFAULT '', "
              "card_token TEXT NOT NULL DEFAULT '', last_four TEXT NOT NULL DEFAULT '', "
              "limit_cents INTEGER NOT NULL DEFAULT 0, charged_usd REAL NOT NULL DEFAULT 0)")
    c.execute("INSERT INTO ledger (ts, agent_id, task_id, amount_usd, merchant, description, status, card_token, last_four) "
              "VALUES (1, 'a', 't', 5, 'm', 'd', 'approved', 'tok', '1234')")
    c.commit(); c.close()
    db = DB(path)
    cols = {r["name"] for r in db.all("PRAGMA table_info(ledger)")}
    assert not cols & {"domain", "card_token", "last_four", "limit_cents", "charged_usd"}
    assert db.one("SELECT merchant, amount_usd FROM ledger") == {"merchant": "m", "amount_usd": 5}

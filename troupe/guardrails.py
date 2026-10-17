"""The single approval gateway every tool call goes through.

Policy values per permission key: "auto" (just do it), "ask" (owner approves first), "deny".
Special values: "contacts_auto" (auto for recipients already in the agent's contacts, ask otherwise)
and "reads_auto" for connectors (read-looking tools auto, everything else ask).

Taint: once a task has read untrusted content (inbound email, SMS, web pages, webhooks), every
external action in that task needs approval regardless of policy. This blunts prompt injection.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .db import DB, loads

DEFAULT_PERMISSIONS = {
    "network": True,            # sandbox gets internet access
    "shell": "auto",
    "files": "auto",
    "browser": "auto",
    "type_secret": "ask",
    "email_send": "contacts_auto",
    "sms_send": "ask",
    "purchase": "ask",
    "handoff": "auto",
    "schedule": "auto",
    "connectors": {},           # {"server_name": "auto" | "reads_auto" | "ask" | "deny"}; absent = no access
}

READ_PREFIXES = ("get", "list", "search", "read", "fetch", "find", "query", "describe", "lookup")


@dataclass
class Decision:
    verdict: str  # allow | ask | deny
    reason: str = ""


def week_start(ts: float | None = None) -> float:
    """Start of the current ISO week (Monday 00:00 UTC)."""
    ts = ts or time.time()
    days = time.gmtime(ts).tm_wday
    day_start = ts - (ts % 86400)
    return day_start - days * 86400


def agent_permissions(agent: dict) -> dict:
    perms = dict(DEFAULT_PERMISSIONS)
    perms.update(loads(agent.get("permissions"), {}))
    return perms


class Guardrails:
    def __init__(self, db: DB):
        self.db = db

    # --- global and per-agent pause ------------------------------------------
    def paused(self) -> bool:
        return self.db.kv_get("paused", "0") == "1"

    def set_paused(self, value: bool) -> None:
        self.db.kv_set("paused", "1" if value else "0")
        self.db.audit("system", "pause" if value else "resume")

    # --- budgets --------------------------------------------------------------
    def llm_spend_this_week(self, agent_id: str) -> float:
        row = self.db.one("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM audit WHERE agent_id = ? AND kind = 'llm' "
                          "AND ts >= ?", (agent_id, week_start()))
        return row["s"] if row else 0.0

    def money_spent_this_week(self, agent_id: str) -> float:
        row = self.db.one("SELECT COALESCE(SUM(amount_usd), 0) AS s FROM ledger WHERE agent_id = ? AND "
                          "status = 'approved' AND ts >= ?", (agent_id, week_start()))
        return row["s"] if row else 0.0

    def llm_budget_ok(self, agent: dict) -> bool:
        budget = agent.get("llm_budget_usd_week") or 0
        return budget <= 0 or self.llm_spend_this_week(agent["id"]) < budget

    def actions_last_day(self, agent_id: str, tool: str) -> int:
        row = self.db.one("SELECT COUNT(*) AS n FROM audit WHERE agent_id = ? AND kind = 'tool' AND name = ? "
                          "AND ts >= ? AND detail NOT LIKE '%\"denied\"%'", (agent_id, tool, time.time() - 86400))
        return row["n"] if row else 0

    def team_addresses(self) -> set[str]:
        """Teammates' AgentMail inboxes: emailing each other needs no approval (taint still applies)."""
        out = set()
        for row in self.db.all("SELECT value FROM kv WHERE key LIKE 'agentmail:%'"):
            try:
                out.add(json.loads(row["value"])["email"].lower())
            except (ValueError, KeyError):
                pass
        return out

    # --- the decision -----------------------------------------------------------
    def decide(self, agent: dict, task: dict, tool, args: dict) -> Decision:
        perms = agent_permissions(agent)
        if tool.permission == "connector":
            server = tool.name.split("__")[1]
            policy = perms.get("connectors", {}).get(server)
            if policy is None:
                return Decision("deny", f"this agent has no access to connector '{server}'")
            if policy == "reads_auto":
                short = tool.name.split("__")[-1].lower()
                policy = "auto" if short.startswith(READ_PREFIXES) else "ask"
        else:
            policy = perms.get(tool.permission, "auto") if tool.permission else "auto"

        if policy == "deny":
            return Decision("deny", f"'{tool.permission}' is disabled for this agent")

        if tool.name == "request_purchase":
            amount = float(args.get("amount_usd", 0))
            cap = agent.get("spend_budget_usd_week") or 0
            if amount <= 0:
                return Decision("deny", "amount must be positive")
            if self.money_spent_this_week(agent["id"]) + amount > cap:
                return Decision("deny", f"would exceed this agent's weekly spending budget (${cap:.2f})")
            return Decision("ask", "every purchase needs the owner's approval")

        if tool.external and task.get("tainted"):
            return Decision("ask", "this task has read untrusted content (email/web/webhook), so external "
                                   "actions need approval")

        if policy == "contacts_auto":
            contacts = {c.lower() for c in loads(agent.get("contacts"), [])} | self.team_addresses()
            recipients = [r.strip().lower() for r in _as_list(args.get("to"))]
            unknown = [r for r in recipients if r not in contacts]
            if unknown:
                return Decision("ask", f"new recipient(s): {', '.join(unknown)}")
            return Decision("allow")

        if policy == "ask":
            return Decision("ask", f"'{tool.permission or tool.name}' is set to ask first")
        return Decision("allow")


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x for x in v.replace(";", ",").split(",") if x.strip()]
    return [str(x) for x in v]

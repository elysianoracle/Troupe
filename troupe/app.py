"""Service container: wires storage, LLM, sandboxes, guardrails, orchestrator, and identities."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .agent import AgentRunner
from .agentmail import AgentMail
from .auth import Auth
from .skills import SkillStore
from .comms import SMS, Email, Mailer
from .config import Settings
from .db import DB, loads, new_id, now
from .guardrails import Guardrails, agent_permissions
from .llm import make_provider
from .mcp_connectors import MCPManager
from .orchestrator import Orchestrator
from .sandbox import SandboxManager
from .tools import Tool, builtin_tools

log = logging.getLogger("troupe")


class Events:
    """Tiny in-process pub/sub feeding the WebSocket UI and Telegram."""

    def __init__(self):
        self.subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)

    def publish(self, kind: str, data: dict) -> None:
        for q in list(self.subs):
            try:
                q.put_nowait({"type": kind, "data": data})
            except asyncio.QueueFull:
                pass


class App:
    def __init__(self, settings: Settings, llm: Any = None, sandbox: Any = None):
        from .vault import Vault

        self.settings = settings
        self.db = DB(settings.db_path)
        self.vault = Vault(self.db, settings.data_dir)
        self.auth = Auth(self.db, settings.admin_token)
        self.skills = SkillStore(settings.data_dir, self.db)
        self.guard = Guardrails(self.db)
        self.llm = llm or make_provider(settings)
        self.sandbox = sandbox or SandboxManager(settings)
        self.events = Events()
        self.email = Mailer(Email(settings), AgentMail(settings, self.db, self.vault))
        self.sms = SMS(settings)
        self.mcp = MCPManager(settings.data_dir, self.vault)
        self.orch = Orchestrator(self)
        self.runner = AgentRunner(self)
        self._builtin = builtin_tools()
        self._bg: list[asyncio.Task] = []

    async def start(self) -> None:
        from . import triggers

        self.prepare_skills()
        await self.mcp.start_all()
        self._bg += [asyncio.create_task(self.orch.dispatcher()),
                     asyncio.create_task(triggers.scheduler(self)),
                     asyncio.create_task(self.sandbox.reaper())]
        # Always running: AgentMail can be switched on later by adding its key in the Vault.
        self._bg.append(asyncio.create_task(triggers.email_poller(self)))
        if self.settings.telegram_token:
            from .surfaces.telegram import TelegramBot
            self._bg.append(asyncio.create_task(TelegramBot(self).run()))
        log.info("Troupe started (provider=%s, model=%s, sandbox=%s)", self.settings.llm_provider,
                 self.settings.default_model, self.settings.sandbox_backend)

    async def stop(self) -> None:
        for t in self._bg:
            t.cancel()

    # --- helpers used by tools and the runner -----------------------------------------------------
    def agent_perms(self, agent: dict) -> dict:
        return agent_permissions(agent)

    def tools_for(self, agent: dict) -> list[Tool]:
        perms = self.agent_perms(agent)
        tools = [t for t in self._builtin if not t.permission or perms.get(t.permission, "auto") != "deny"]
        if not self.email.enabled:
            tools = [t for t in tools if t.name != "send_email"]
        if not self.sms.enabled:
            tools = [t for t in tools if t.name != "send_sms"]
        if (agent.get("spend_budget_usd_week") or 0) <= 0:
            tools = [t for t in tools if t.name != "request_purchase"]
        return tools + self.mcp.tools(perms.get("connectors", {}))

    def state_set(self, task: dict, **kv) -> None:
        row = self.db.one("SELECT state FROM tasks WHERE id = ?", (task["id"],))
        state = loads(row["state"], {})
        state.update(kv)
        self.db.update("tasks", "id", task["id"], {"state": json.dumps(state)})

    def task_cancelled(self, task_id: str) -> bool:
        row = self.db.one("SELECT status FROM tasks WHERE id = ?", (task_id,))
        return bool(row and row["status"] == "cancelled")

    def finish(self, task: dict, status: str, result: str) -> None:
        current = self.db.one("SELECT status FROM tasks WHERE id = ?", (task["id"],))
        if current and current["status"] == "cancelled":
            status = "cancelled"
        self.db.update("tasks", "id", task["id"], {"status": status, "result": result[:20_000], "updated_at": now()})
        self.events.publish("task", {"id": task["id"], "status": status, "agent_id": task["agent_id"],
                                     "room_id": task["room_id"]})

    def request_approval(self, task: dict, agent: dict, room: dict, tool_use: dict, reason: str) -> str:
        aid = new_id("apr")
        self.db.insert("approvals", {"id": aid, "task_id": task["id"], "agent_id": agent["id"],
                                     "tool_use_id": tool_use["id"], "tool": tool_use["name"],
                                     "args": json.dumps(tool_use.get("input") or {}), "reason": reason,
                                     "created_at": now()})
        self.db.audit("approval", "requested", {"tool": tool_use["name"], "reason": reason}, agent["id"], task["id"])
        payload = {"id": aid, "task_id": task["id"], "agent_id": agent["id"], "agent_name": agent["name"],
                   "room_id": room["id"], "tool": tool_use["name"], "args": tool_use.get("input") or {},
                   "reason": reason}
        self.events.publish("approval", payload)
        return aid

    def decide_approval(self, approval_id: str, approve: bool, note: str = "") -> dict:
        a = self.db.one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if not a:
            raise KeyError("no such approval")
        if a["status"] != "pending":
            return a
        status = "approved" if approve else "denied"
        self.db.update("approvals", "id", approval_id, {"status": status, "note": note, "decided_at": now()})
        self.db.audit("approval", status, {"tool": a["tool"], "note": note}, a["agent_id"], a["task_id"])
        still = self.db.one("SELECT COUNT(*) AS n FROM approvals WHERE task_id = ? AND status = 'pending'",
                            (a["task_id"],))["n"]
        task = self.db.one("SELECT status FROM tasks WHERE id = ?", (a["task_id"],))
        if not still and task and task["status"] == "waiting_approval":
            self.db.update("tasks", "id", a["task_id"], {"status": "queued", "updated_at": now()})
            self.orch.wake()
        self.events.publish("approval_decided", {"id": approval_id, "status": status})
        return self.db.one("SELECT * FROM approvals WHERE id = ?", (approval_id,))

    def prepare_skills(self) -> None:
        """First run installs the curated library; older databases' playbooks move to SKILL.md folders."""
        if self.db.kv_get("skills:library_seeded") != "1":
            self.skills.install_all_library()
            self.db.kv_set("skills:library_seeded", "1")
        self.skills.migrate_db_skills()

    def save_skill(self, agent_id: str, name: str, description: str, params: list, playbook: str,
                   trace: str = "") -> None:
        agent = self.db.one("SELECT * FROM agents WHERE id = ?", (agent_id,))
        self.skills.save_learned(agent, name, description, params, playbook, trace)

    # --- agents and rooms ---------------------------------------------------------------------------
    def create_agent(self, name: str, role: str = "", instructions: str = "", model: str = "",
                     permissions: dict | None = None, llm_budget: float = 10.0, spend_budget: float = 0.0,
                     contacts: list[str] | None = None) -> dict:
        import re
        if not re.fullmatch(r"[A-Za-z][\w\-]{0,30}", name):
            raise ValueError("agent name: letters, digits, - and _, starting with a letter, max 31 chars")
        aid = new_id("agt")
        self.db.insert("agents", {"id": aid, "name": name, "role": role, "instructions": instructions,
                                  "model": model, "permissions": json.dumps(permissions or {}),
                                  "llm_budget_usd_week": llm_budget, "spend_budget_usd_week": spend_budget,
                                  "contacts": json.dumps(contacts or []), "created_at": now()})
        self.dm_room(aid)
        agent = self.db.one("SELECT * FROM agents WHERE id = ?", (aid,))
        if self.email.agentmail.enabled:
            try:  # give the new agent its inbox right away
                asyncio.get_running_loop().create_task(self.email.agentmail.ensure_inbox(agent))
            except RuntimeError:
                pass  # no event loop (CLI seeding); the poller creates it on its first pass
        return agent

    def dm_room(self, agent_id: str) -> dict:
        r = self.db.one("SELECT r.* FROM rooms r JOIN room_members m ON m.room_id = r.id WHERE r.kind = 'dm' "
                        "AND m.agent_id = ?", (agent_id,))
        if r:
            return r
        agent = self.db.one("SELECT name FROM agents WHERE id = ?", (agent_id,))
        return self.create_room(agent["name"], "dm", [agent_id])

    def create_room(self, name: str, kind: str, agent_ids: list[str], lead: str | None = None) -> dict:
        rid = new_id("room")
        self.db.insert("rooms", {"id": rid, "name": name, "kind": kind,
                                 "lead_agent_id": lead or (agent_ids[0] if agent_ids else None), "created_at": now()})
        for a in agent_ids:
            self.db.execute("INSERT OR IGNORE INTO room_members(room_id, agent_id) VALUES(?, ?)", (rid, a))
        return self.db.one("SELECT * FROM rooms WHERE id = ?", (rid,))

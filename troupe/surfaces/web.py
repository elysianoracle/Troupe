"""HTTP API, WebSocket events, inbound webhooks, and the web/PWA UI."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import secrets
from pathlib import Path
from typing import Any

from croniter import croniter
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..db import loads, new_id, now
from ..guardrails import DEFAULT_PERMISSIONS

STATIC = Path(__file__).resolve().parent.parent / "static"


class AgentIn(BaseModel):
    name: str
    role: str = ""
    instructions: str = ""
    model: str = ""
    permissions: dict = {}
    llm_budget_usd_week: float = 10.0
    spend_budget_usd_week: float = 0.0
    contacts: list[str] = []


class AgentPatch(BaseModel):
    role: str | None = None
    instructions: str | None = None
    model: str | None = None
    status: str | None = None
    permissions: dict | None = None
    llm_budget_usd_week: float | None = None
    spend_budget_usd_week: float | None = None
    contacts: list[str] | None = None


class RoomIn(BaseModel):
    name: str
    agent_ids: list[str]
    lead_agent_id: str | None = None


class MessageIn(BaseModel):
    text: str


class DecisionIn(BaseModel):
    approve: bool
    note: str = ""


class ScheduleIn(BaseModel):
    agent_id: str
    room_id: str | None = None
    cron: str
    prompt: str


class MemoryIn(BaseModel):
    content: str
    tags: str = ""
    pinned: bool = False


class SecretIn(BaseModel):
    name: str
    value: str
    domains: list[str] = []


def build_api(app) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_):
        await app.start()
        yield
        await app.stop()

    api = FastAPI(title="Troupe", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    db = app.db

    def request_token(request: Request) -> str:
        return request.headers.get("authorization", "").removeprefix("Bearer ").strip() \
            or request.query_params.get("token", "")

    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else ""
        # Behind Caddy on the same machine, trust its forwarded address; otherwise use the peer.
        if peer in {"127.0.0.1", "::1"}:
            fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            return fwd or peer
        return peer

    def auth(request: Request) -> None:
        if not app.auth.valid(request_token(request)):
            raise HTTPException(401, "please sign in")

    A = Depends(auth)

    def agent_out(a: dict) -> dict:
        a = dict(a)
        a["permissions"] = {**DEFAULT_PERMISSIONS, **loads(a["permissions"], {})}
        a["contacts"] = loads(a["contacts"], [])
        a["email"] = app.email.address_for(a)
        a["llm_spend_week"] = round(app.guard.llm_spend_this_week(a["id"]), 4)
        a["money_spend_week"] = round(app.guard.money_spent_this_week(a["id"]), 2)
        return a

    # --- UI ---------------------------------------------------------------------------------
    @api.get("/", response_class=HTMLResponse)
    async def index():
        return FileResponse(STATIC / "index.html")

    api.mount("/static", StaticFiles(directory=STATIC), name="static")

    @api.get("/manifest.webmanifest")
    async def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @api.get("/api/auth-info")
    async def auth_info():
        return {"password_set": app.auth.password_set}

    @api.post("/api/login")
    async def login(body: dict, request: Request):
        if not app.auth.password_set:
            raise HTTPException(400, "No password is set yet. On the server run: sudo troupe-password")
        try:
            token = await asyncio.to_thread(app.auth.login, str(body.get("password", "")), client_ip(request))
        except TimeoutError as e:
            raise HTTPException(429, f"Too many wrong passwords. Try again in {int(e.args[0]) // 60 + 1} min.")
        except PermissionError:
            raise HTTPException(401, "Wrong password")
        return {"token": token}

    @api.post("/api/logout")
    async def logout(request: Request):
        app.auth.logout(request_token(request))
        return {"ok": True}

    @api.get("/healthz")
    async def health():
        return {"ok": True}

    # --- state --------------------------------------------------------------------------------
    @api.get("/api/state", dependencies=[A])
    async def state():
        rooms = db.all("SELECT * FROM rooms ORDER BY created_at")
        for r in rooms:
            r["members"] = [m["agent_id"] for m in db.all("SELECT agent_id FROM room_members WHERE room_id = ?",
                                                           (r["id"],))]
            r["teach"] = loads(r["teach"], None)
            last = db.one("SELECT id, content, created_at FROM messages WHERE room_id = ? ORDER BY id DESC LIMIT 1",
                          (r["id"],))
            r["last_message"] = last
        return {
            "agents": [agent_out(a) for a in db.all("SELECT * FROM agents ORDER BY created_at")],
            "rooms": rooms,
            "approvals": pending_approvals(),
            "paused": app.guard.paused(),
            "provider": app.settings.llm_provider,
            "default_model": app.settings.default_model,
            "email_enabled": app.email.enabled,
            "email_provider": app.email.provider,
            "sms_enabled": app.sms.enabled,
            "connectors": {n: [t.name for t in s.tools] for n, s in app.mcp.servers.items()},
        }

    def pending_approvals() -> list[dict]:
        rows = db.all("SELECT ap.*, a.name AS agent_name, t.room_id FROM approvals ap JOIN agents a ON a.id = ap.agent_id "
                      "JOIN tasks t ON t.id = ap.task_id WHERE ap.status = 'pending' ORDER BY ap.created_at")
        for r in rows:
            r["args"] = loads(r["args"], {})
        return rows

    @api.post("/api/pause", dependencies=[A])
    async def pause(body: dict):
        app.guard.set_paused(bool(body.get("paused", True)))
        app.orch.wake()
        app.events.publish("paused", {"paused": app.guard.paused()})
        return {"paused": app.guard.paused()}

    @api.get("/api/system", dependencies=[A])
    async def system():
        mem = {}
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, v = line.split(":", 1)
                if k in {"MemTotal", "MemAvailable"}:
                    mem[k] = int(v.strip().split()[0]) // 1024
        except OSError:
            pass
        return {"memory_mb": mem, "sandboxes": await app.sandbox.status(),
                "running_tasks": len(app.orch.running), "max_parallel": app.settings.max_parallel_tasks,
                "max_sandboxes": app.settings.sandbox_max_active}

    # --- agents --------------------------------------------------------------------------------
    @api.post("/api/agents", dependencies=[A])
    async def create_agent(body: AgentIn):
        try:
            a = app.create_agent(body.name, body.role, body.instructions, body.model, body.permissions,
                                 body.llm_budget_usd_week, body.spend_budget_usd_week, body.contacts)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(400, f"could not create agent: {e}")
        return agent_out(a)

    @api.patch("/api/agents/{agent_id}", dependencies=[A])
    async def patch_agent(agent_id: str, body: AgentPatch):
        fields: dict[str, Any] = {}
        for k, v in body.model_dump(exclude_none=True).items():
            fields[k] = json.dumps(v) if k in {"permissions", "contacts"} else v
        if fields.get("status") not in (None, "active", "paused"):
            raise HTTPException(400, "status must be active or paused")
        db.update("agents", "id", agent_id, fields)
        if "permissions" in fields:
            await app.sandbox.stop(agent_id)  # network policy may have changed
        app.orch.wake()
        a = db.one("SELECT * FROM agents WHERE id = ?", (agent_id,))
        if not a:
            raise HTTPException(404)
        return agent_out(a)

    @api.delete("/api/agents/{agent_id}", dependencies=[A])
    async def delete_agent(agent_id: str):
        for t in db.all("SELECT id FROM tasks WHERE agent_id = ? AND status IN ('queued','running','waiting_approval')",
                        (agent_id,)):
            app.orch.cancel(t["id"])
        await app.sandbox.stop(agent_id)
        for table in ("room_members", "threads", "memories", "skills", "schedules"):
            db.execute(f"DELETE FROM {table} WHERE agent_id = ?", (agent_id,))
        db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        return {"ok": True, "note": "workspace files are kept on disk under data/workspaces"}

    @api.get("/api/agents/{agent_id}/hook", dependencies=[A])
    async def hook_url(agent_id: str):
        key = f"hook:{agent_id}"
        token = db.kv_get(key) or secrets.token_urlsafe(18)
        db.kv_set(key, token)
        return {"url": f"{app.settings.public_url}/hooks/{agent_id}/{token}"}

    # --- rooms and messages ---------------------------------------------------------------------
    @api.post("/api/rooms", dependencies=[A])
    async def create_room(body: RoomIn):
        if not body.agent_ids:
            raise HTTPException(400, "a room needs at least one agent")
        return app.create_room(body.name, "group", body.agent_ids, body.lead_agent_id)

    @api.post("/api/rooms/{room_id}/members", dependencies=[A])
    async def add_member(room_id: str, body: dict):
        db.execute("INSERT OR IGNORE INTO room_members(room_id, agent_id) VALUES(?, ?)", (room_id, body["agent_id"]))
        return {"ok": True}

    @api.delete("/api/rooms/{room_id}", dependencies=[A])
    async def delete_room(room_id: str):
        app.orch.cancel_room_tasks(room_id)
        for table in ("room_members", "messages", "threads", "schedules"):
            db.execute(f"DELETE FROM {table} WHERE room_id = ?", (room_id,))
        db.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
        return {"ok": True}

    @api.get("/api/rooms/{room_id}/messages", dependencies=[A])
    async def messages(room_id: str, before: int | None = None, limit: int = 60):
        q = "SELECT m.*, a.name AS agent_name FROM messages m LEFT JOIN agents a ON a.id = m.sender_id WHERE m.room_id = ?"
        params: list = [room_id]
        if before:
            q += " AND m.id < ?"
            params.append(before)
        rows = db.all(q + " ORDER BY m.id DESC LIMIT ?", params + [min(limit, 200)])
        for r in rows:
            r["meta"] = loads(r["meta"], {})
        room = db.one("SELECT plan FROM rooms WHERE id = ?", (room_id,))
        tasks = db.all("SELECT t.id, t.status, t.agent_id, a.name AS agent_name, t.steps FROM tasks t JOIN agents a "
                       "ON a.id = t.agent_id WHERE t.room_id = ? AND t.status IN ('queued','running','waiting_approval')",
                       (room_id,))
        return {"messages": list(reversed(rows)), "plan": room["plan"] if room else "", "active_tasks": tasks}

    @api.post("/api/rooms/{room_id}/messages", dependencies=[A])
    async def send(room_id: str, body: MessageIn):
        try:
            return await app.orch.user_message(room_id, body.text)
        except ValueError as e:
            raise HTTPException(404, str(e))

    # --- approvals and tasks ------------------------------------------------------------------------
    @api.get("/api/approvals", dependencies=[A])
    async def approvals():
        return pending_approvals()

    @api.post("/api/approvals/{approval_id}", dependencies=[A])
    async def decide(approval_id: str, body: DecisionIn):
        try:
            return app.decide_approval(approval_id, body.approve, body.note)
        except KeyError:
            raise HTTPException(404)

    @api.get("/api/tasks", dependencies=[A])
    async def tasks(room_id: str | None = None, limit: int = 50):
        if room_id:
            return db.all("SELECT * FROM tasks WHERE room_id = ? ORDER BY created_at DESC LIMIT ?", (room_id, limit))
        return db.all("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))

    @api.post("/api/tasks/{task_id}/cancel", dependencies=[A])
    async def cancel(task_id: str):
        app.orch.cancel(task_id)
        return {"ok": True}

    @api.get("/api/audit", dependencies=[A])
    async def audit(agent_id: str | None = None, task_id: str | None = None, kind: str | None = None, limit: int = 200):
        q, params = "SELECT au.*, a.name AS agent_name FROM audit au LEFT JOIN agents a ON a.id = au.agent_id WHERE 1=1", []
        for col, val in (("au.agent_id", agent_id), ("au.task_id", task_id), ("au.kind", kind)):
            if val:
                q += f" AND {col} = ?"
                params.append(val)
        return db.all(q + " ORDER BY au.id DESC LIMIT ?", params + [min(limit, 1000)])

    # --- memory, skills, schedules ------------------------------------------------------------------
    @api.get("/api/agents/{agent_id}/memories", dependencies=[A])
    async def memories(agent_id: str):
        return db.all("SELECT * FROM memories WHERE agent_id = ? ORDER BY pinned DESC, id DESC LIMIT 500", (agent_id,))

    @api.post("/api/agents/{agent_id}/memories", dependencies=[A])
    async def add_memory(agent_id: str, body: MemoryIn):
        mid = db.insert("memories", {"agent_id": agent_id, "content": body.content, "tags": body.tags,
                                     "pinned": int(body.pinned), "created_at": now()})
        return {"id": mid}

    @api.delete("/api/memories/{memory_id}", dependencies=[A])
    async def del_memory(memory_id: int):
        db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return {"ok": True}

    @api.get("/api/skills", dependencies=[A])
    async def skills():
        rows = db.all("SELECT s.id, s.agent_id, a.name AS agent_name, s.name, s.description, s.params, s.playbook, "
                      "s.created_at FROM skills s JOIN agents a ON a.id = s.agent_id ORDER BY s.name")
        for r in rows:
            r["params"] = loads(r["params"], [])
        return rows

    @api.patch("/api/skills/{skill_id}", dependencies=[A])
    async def edit_skill(skill_id: str, body: dict):
        db.update("skills", "id", skill_id, {k: v for k, v in body.items() if k in {"description", "playbook"}})
        return {"ok": True}

    @api.delete("/api/skills/{skill_id}", dependencies=[A])
    async def del_skill(skill_id: str):
        db.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
        return {"ok": True}

    @api.get("/api/schedules", dependencies=[A])
    async def schedules():
        return db.all("SELECT s.*, a.name AS agent_name FROM schedules s JOIN agents a ON a.id = s.agent_id "
                      "ORDER BY s.next_run")

    @api.post("/api/schedules", dependencies=[A])
    async def add_schedule(body: ScheduleIn):
        if not croniter.is_valid(body.cron):
            raise HTTPException(400, "invalid cron expression")
        room_id = body.room_id or app.dm_room(body.agent_id)["id"]
        sid = new_id("sch")
        db.insert("schedules", {"id": sid, "agent_id": body.agent_id, "room_id": room_id, "cron": body.cron,
                                "prompt": body.prompt, "enabled": 1,
                                "next_run": croniter(body.cron, now()).get_next(float), "created_at": now()})
        return db.one("SELECT * FROM schedules WHERE id = ?", (sid,))

    @api.patch("/api/schedules/{sid}", dependencies=[A])
    async def toggle_schedule(sid: str, body: dict):
        db.update("schedules", "id", sid, {"enabled": 1 if body.get("enabled") else 0})
        return {"ok": True}

    @api.delete("/api/schedules/{sid}", dependencies=[A])
    async def del_schedule(sid: str):
        db.execute("DELETE FROM schedules WHERE id = ?", (sid,))
        return {"ok": True}

    # --- purchases (manual: agents ask, the owner approves and pays) -----------------------------------------
    @api.get("/api/purchases", dependencies=[A])
    async def purchases(limit: int = 50):
        return db.all("SELECT l.id, l.ts, a.name AS agent_name, l.amount_usd, l.merchant, l.url, l.description, "
                      "l.status FROM ledger l JOIN agents a ON a.id = l.agent_id ORDER BY l.id DESC LIMIT ?",
                      (min(limit, 200),))

    # --- secrets (values are write-only) ---------------------------------------------------------------
    @api.get("/api/secrets", dependencies=[A])
    async def list_secrets():
        return [{"name": r["name"], "domains": loads(r["domains"], [])} for r in app.vault.names()]

    @api.post("/api/secrets", dependencies=[A])
    async def set_secret(body: SecretIn):
        app.vault.set(body.name, body.value, body.domains)
        db.audit("system", "secret_set", {"name": body.name})
        return {"ok": True}

    @api.delete("/api/secrets/{name}", dependencies=[A])
    async def del_secret(name: str):
        app.vault.delete(name)
        return {"ok": True}

    # --- workspace files (screenshots, reports the agents made) ----------------------------------------
    @api.get("/api/agents/{agent_id}/files", dependencies=[A])
    async def files(agent_id: str, path: str = "."):
        root = app.sandbox.workspace(agent_id).resolve()
        p = (root / path.lstrip("/")).resolve()
        if p != root and not p.is_relative_to(root):
            raise HTTPException(400, "bad path")
        if p.is_file():
            return FileResponse(p)
        if not p.is_dir():
            raise HTTPException(404)
        return [{"name": c.name, "dir": c.is_dir(), "size": c.stat().st_size if c.is_file() else 0}
                for c in sorted(p.iterdir()) if not c.name.startswith(".")]

    # --- inbound webhooks (untrusted) ---------------------------------------------------------------------
    @api.post("/hooks/{agent_id}/{token}")
    async def webhook(agent_id: str, token: str, request: Request):
        expected = db.kv_get(f"hook:{agent_id}")
        if not expected or not hmac.compare_digest(token, expected):
            raise HTTPException(404)
        raw = (await request.body())[:50_000].decode(errors="replace")
        room = app.dm_room(agent_id)
        db.audit("trigger", "webhook", {"bytes": len(raw)}, agent_id=agent_id)
        tid = app.orch.create_task(agent_id, room["id"], "webhook", raw, tainted=True)
        return {"task_id": tid}

    @api.post("/hooks/twilio/sms")
    async def twilio_sms(request: Request):
        form = {k: str(v) for k, v in (await request.form()).items()}
        url = f"{app.settings.public_url}/hooks/twilio/sms"
        if not app.sms.enabled or not app.sms.valid_signature(url, form, request.headers.get("X-Twilio-Signature", "")):
            raise HTTPException(403)
        agents = db.all("SELECT * FROM agents ORDER BY created_at")
        agent = next((a for a in agents if app.agent_perms(a).get("sms_send") != "deny"), None)
        if agent:
            room = app.dm_room(agent["id"])
            app.orch.post(room["id"], "system", "", f"New text message from {form.get('From', '?')}")
            app.orch.create_task(agent["id"], room["id"], "sms", f"From: {form.get('From')}\n\n{form.get('Body', '')}",
                                 tainted=True)
        return Response("<Response></Response>", media_type="application/xml")

    # --- live events ----------------------------------------------------------------------------------------
    @api.websocket("/ws")
    async def ws(sock: WebSocket):
        if not app.auth.valid(sock.query_params.get("token", "")):
            await sock.close(code=4401)
            return
        await sock.accept()
        q = app.events.subscribe()
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=25)
                except asyncio.TimeoutError:
                    ev = {"type": "ping", "data": {}}
                await sock.send_text(json.dumps(ev, default=str))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            app.events.unsubscribe(q)

    @api.exception_handler(404)
    async def not_found(request, exc):
        return PlainTextResponse(getattr(exc, "detail", "not found"), status_code=404)

    return api

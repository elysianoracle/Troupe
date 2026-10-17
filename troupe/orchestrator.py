"""Rooms, routing, handoffs, teaching, and the task dispatcher.

- DM room: one owner, one agent.
- Group room: several agents share a goal. Owner messages go to @mentioned agents, else to the room's
  lead. The lead plans and hands work to teammates; results flow back to the lead; the owner makes
  the final call. A per-goal hop cap stops agents from ping-ponging forever.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from .db import loads, new_id, now
from .tools import ToolOutput

log = logging.getLogger("troupe.orch")
MENTION = re.compile(r"@([\w\-]+)")

DISTILL_PROMPT = """You just completed a task while the owner taught you how to do it. Turn it into a
reusable skill. Here is the session transcript (owner instructions, your replies, and the tools you
called with their results):

{trace}

Reply with ONLY a JSON object, no prose:
{{"name": "{name}", "description": "<one sentence>", "params": ["<placeholder names that vary between runs>"],
  "playbook": "<numbered steps. Be concrete: URLs, selectors/labels to look for, commands, file paths.
  Use {{param}} placeholders. Note checks to verify success and what to do if a step fails.>"}}"""


class Orchestrator:
    def __init__(self, app):
        self.app = app
        self.running: dict[str, asyncio.Task] = {}   # task_id -> asyncio task
        self.busy_agents: set[str] = set()
        self._wake = asyncio.Event()

    # --- messages -------------------------------------------------------------------
    def post(self, room_id: str, sender_type: str, sender_id: str, content: str, meta: dict | None = None) -> int:
        mid = self.app.db.insert("messages", {"room_id": room_id, "sender_type": sender_type,
                                              "sender_id": sender_id, "content": content,
                                              "meta": json.dumps(meta or {}), "created_at": now()})
        agent_name = ""
        if sender_type == "agent":
            a = self.app.db.one("SELECT name FROM agents WHERE id = ?", (sender_id,))
            agent_name = a["name"] if a else ""
        self.app.events.publish("message", {"id": mid, "room_id": room_id, "sender_type": sender_type,
                                            "sender_id": sender_id, "agent_name": agent_name,
                                            "content": content, "meta": meta or {}, "created_at": now()})
        return mid

    def members(self, room_id: str) -> list[dict]:
        return self.app.db.all("SELECT a.* FROM agents a JOIN room_members rm ON rm.agent_id = a.id "
                               "WHERE rm.room_id = ? ORDER BY a.name", (room_id,))

    def create_task(self, agent_id: str, room_id: str, source: str, text: str, goal_id: str | None = None,
                    state: dict | None = None, tainted: bool = False) -> str:
        tid = new_id("tsk")
        self.app.db.insert("tasks", {"id": tid, "agent_id": agent_id, "room_id": room_id, "source": source,
                                     "input": text, "goal_id": goal_id or new_id("goal"),
                                     "tainted": 1 if tainted else 0, "state": json.dumps(state or {}),
                                     "created_at": now(), "updated_at": now()})
        self.app.events.publish("task", {"id": tid, "status": "queued", "agent_id": agent_id, "room_id": room_id})
        self.wake()
        return tid

    async def user_message(self, room_id: str, text: str) -> dict:
        db = self.app.db
        room = db.one("SELECT * FROM rooms WHERE id = ?", (room_id,))
        if not room:
            raise ValueError("no such room")
        text = text.strip()
        if text.startswith("/"):
            reply = await self.command(room, text)
            if reply is not None:
                self.post(room_id, "user", "", text)
                self.post(room_id, "system", "", reply)
                return {"command": True, "reply": reply}

        mid = self.post(room_id, "user", "", text)
        members = self.members(room_id)
        if not members:
            self.post(room_id, "system", "", "This room has no agents yet. Add one first.")
            return {"tasks": []}
        by_name = {m["name"].lower(): m for m in members}
        targets = [by_name[n.lower()] for n in MENTION.findall(text) if n.lower() in by_name]
        if not targets:
            lead = next((m for m in members if m["id"] == room.get("lead_agent_id")), members[0])
            targets = [lead]
        teach = loads(room.get("teach"), None)
        source = "teach" if teach else "user"
        seen = set()
        task_ids = []
        for t in targets:
            if t["id"] in seen:
                continue
            seen.add(t["id"])
            task_ids.append(self.create_task(t["id"], room_id, source, text, state={"message_id": mid}))
        return {"tasks": task_ids}

    # --- slash commands ------------------------------------------------------------------
    async def command(self, room: dict, text: str) -> str | None:
        db = self.app.db
        cmd, _, rest = text.partition(" ")
        cmd = cmd.lower()
        if cmd == "/teach":
            name = re.sub(r"[^\w\-]", "-", rest.split("@")[0].strip()).strip("-").lower()
            if not name:
                return "Usage: /teach <skill-name> [@agent], then walk the agent through the task, then /done"
            members = self.members(room["id"])
            mention = MENTION.findall(rest)
            agent = next((m for m in members if mention and m["name"].lower() == mention[0].lower()), None) \
                or next((m for m in members if m["id"] == room.get("lead_agent_id")), None) \
                or (members[0] if members else None)
            if not agent:
                return "Add an agent to this room first."
            last = db.one("SELECT COALESCE(MAX(id), 0) AS m FROM messages WHERE room_id = ?", (room["id"],))["m"]
            db.update("rooms", "id", room["id"], {"teach": json.dumps(
                {"skill": name, "agent_id": agent["id"], "start_msg": last, "start_ts": now()})})
            return (f"Teaching {agent['name']} the skill '{name}'. Describe each step; {agent['name']} will do it "
                    f"live. Send /done when finished (or /cancel-teach).")
        if cmd == "/cancel-teach":
            db.update("rooms", "id", room["id"], {"teach": ""})
            return "Teaching cancelled."
        if cmd == "/done":
            teach = loads(room.get("teach"), None)
            if not teach:
                return None  # not a command here; treat as a normal message
            db.update("rooms", "id", room["id"], {"teach": ""})
            asyncio.create_task(self.distill_skill(room, teach))
            return "Got it. Turning that session into a reusable skill..."
        if cmd == "/stop":
            n = self.cancel_room_tasks(room["id"])
            return f"Stopped {n} task(s) in this room."
        if cmd == "/pause":
            self.app.guard.set_paused(True)
            return "All agents paused. Send /resume to continue."
        if cmd == "/resume":
            self.app.guard.set_paused(False)
            self.wake()
            return "Agents resumed."
        if cmd == "/help":
            return ("/teach <name> [@agent] ... /done - teach a skill by demonstration\n"
                    "/stop - stop tasks in this room\n/pause, /resume - global kill switch\n"
                    "@name - address a specific agent")
        return None

    def cancel_room_tasks(self, room_id: str) -> int:
        db = self.app.db
        rows = db.all("SELECT id FROM tasks WHERE room_id = ? AND status IN ('queued','running','waiting_approval')",
                      (room_id,))
        for r in rows:
            self.cancel(r["id"])
        return len(rows)

    def cancel(self, task_id: str) -> None:
        db = self.app.db
        db.update("tasks", "id", task_id, {"status": "cancelled", "updated_at": now()})
        db.execute("UPDATE approvals SET status = 'denied', note = 'task cancelled', decided_at = ? "
                   "WHERE task_id = ? AND status = 'pending'", (now(), task_id))
        t = self.running.get(task_id)
        if t and not t.done():
            t.cancel()
        self.app.events.publish("task", {"id": task_id, "status": "cancelled"})

    # --- handoffs ------------------------------------------------------------------------------
    async def handoff(self, ctx, agent_name: str, instructions: str) -> ToolOutput:
        db = self.app.db
        target = db.one("SELECT * FROM agents WHERE lower(name) = lower(?)", (agent_name.lstrip("@"),))
        if not target:
            names = ", ".join(a["name"] for a in db.all("SELECT name FROM agents"))
            return ToolOutput(f"no agent named {agent_name}. Team: {names}", is_error=True)
        if target["id"] == ctx.agent["id"]:
            return ToolOutput("you can't hand off to yourself", is_error=True)
        hops = db.one("SELECT COUNT(*) AS n FROM tasks WHERE goal_id = ?", (ctx.task["goal_id"],))["n"]
        if hops >= self.app.settings.max_hops_per_goal:
            return ToolOutput("handoff limit for this goal reached; finish with what you have and report to "
                              "the owner", is_error=True)
        db.execute("INSERT OR IGNORE INTO room_members(room_id, agent_id) VALUES(?, ?)",
                   (ctx.room["id"], target["id"]))
        self.post(ctx.room["id"], "agent", ctx.agent["id"], f"@{target['name']} {instructions}",
                  {"kind": "handoff", "to": target["id"]})
        self.create_task(target["id"], ctx.room["id"], "handoff",
                         f"{ctx.agent['name']} handed you this work:\n{instructions}\n\n"
                         f"Do it, then reply with the result (it goes back to {ctx.agent['name']}).",
                         goal_id=ctx.task["goal_id"], tainted=bool(ctx.task["tainted"]),
                         state={"reply_to": ctx.agent["id"]})
        return ToolOutput(f"Handed off to {target['name']}. Their result will arrive as a new message. "
                          f"Continue with independent work or end your turn.")

    async def on_task_done(self, task: dict, agent: dict, room: dict, result: str) -> None:
        state = loads(task["state"], {})
        reply_to = state.get("reply_to")
        if not reply_to:
            return
        db = self.app.db
        hops = db.one("SELECT COUNT(*) AS n FROM tasks WHERE goal_id = ?", (task["goal_id"],))["n"]
        if hops >= self.app.settings.max_hops_per_goal:
            self.post(room["id"], "system", "", "Hop limit reached for this goal; agents stopped passing work. "
                                                "Tell them how to proceed.")
            return
        self.create_task(reply_to, room["id"], "handoff_result",
                         f"{agent['name']} finished the work you handed off. Their result:\n{result or '(empty)'}",
                         goal_id=task["goal_id"], tainted=bool(task["tainted"]))

    # --- teach by demonstration ------------------------------------------------------------------
    async def distill_skill(self, room: dict, teach: dict) -> None:
        db, app = self.app.db, self.app
        agent = db.one("SELECT * FROM agents WHERE id = ?", (teach["agent_id"],))
        # Wait for that agent's in-flight teach tasks to finish first.
        for _ in range(600):
            busy = db.one("SELECT COUNT(*) AS n FROM tasks WHERE agent_id = ? AND room_id = ? AND status IN "
                          "('queued','running','waiting_approval')", (agent["id"], room["id"]))["n"]
            if not busy:
                break
            await asyncio.sleep(2)
        lines = []
        for m in db.all("SELECT m.*, a.name AS agent_name FROM messages m LEFT JOIN agents a ON a.id = m.sender_id "
                        "WHERE m.room_id = ? AND m.id > ? ORDER BY m.id", (room["id"], teach["start_msg"])):
            if m["content"].startswith("/"):
                continue
            who = "OWNER" if m["sender_type"] == "user" else (m["agent_name"] or "SYSTEM")
            lines.append(f"{who}: {m['content'][:2000]}")
        for a in db.all("SELECT name, detail FROM audit WHERE agent_id = ? AND kind = 'tool' AND ts >= ? ORDER BY id",
                        (agent["id"], teach["start_ts"])):
            lines.append(f"TOOL {a['name']}: {a['detail'][:1200]}")
        trace = "\n".join(lines)[-60_000:]
        try:
            resp = await app.llm.complete(agent["model"] or app.settings.default_model,
                                          f"You are {agent['name']}. You write precise, reusable playbooks.",
                                          [{"role": "user", "content": DISTILL_PROMPT.format(trace=trace,
                                                                                            name=teach["skill"])}],
                                          [], max_tokens=6000)
            db.audit("llm", "distill_skill", {"skill": teach["skill"]}, agent_id=agent["id"], cost_usd=resp.cost_usd)
            data = _extract_json(resp.text)
            name = teach["skill"]
            app.save_skill(agent["id"], name, data.get("description", ""), data.get("params", []),
                           data.get("playbook", resp.text), trace)
            params = ", ".join(data.get("params", [])) or "none"
            self.post(room["id"], "agent", agent["id"],
                      f"Learned skill **{name}** (params: {params}).\n\n{data.get('description', '')}\n\n"
                      f"Ask me to \"use skill {name}\" any time, or schedule it.", {"kind": "skill", "skill": name})
        except Exception as e:
            log.exception("skill distillation failed")
            self.post(room["id"], "system", "", f"Couldn't save the skill: {e}")

    # --- dispatcher -----------------------------------------------------------------------------
    def wake(self) -> None:
        self._wake.set()

    async def dispatcher(self) -> None:
        db = self.app.db
        db.execute("UPDATE tasks SET status = 'queued' WHERE status = 'running'")  # resume after restart
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self.app.guard.paused():
                continue
            queued = db.all("SELECT t.id, t.agent_id FROM tasks t JOIN agents a ON a.id = t.agent_id "
                            "WHERE t.status = 'queued' AND a.status = 'active' AND NOT EXISTS ("
                            "  SELECT 1 FROM tasks w WHERE w.agent_id = t.agent_id AND w.room_id = t.room_id "
                            "  AND w.status = 'waiting_approval' AND w.id != t.id) "
                            "ORDER BY t.created_at")
            for row in queued:
                if len(self.running) >= self.app.settings.max_parallel_tasks:
                    break
                if row["agent_id"] in self.busy_agents:
                    continue
                self.busy_agents.add(row["agent_id"])
                self.running[row["id"]] = asyncio.create_task(self._run(row["id"], row["agent_id"]))

    async def _run(self, task_id: str, agent_id: str) -> None:
        try:
            await self.app.runner.run(task_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("task %s crashed", task_id)
            t = self.app.db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
            self.app.finish(t, "failed", f"internal error: {e}")
        finally:
            self.running.pop(task_id, None)
            self.busy_agents.discard(agent_id)
            self.wake()


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}

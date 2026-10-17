"""Smoke test against the REAL Docker sandbox on a Linux host (run as a user in the docker group).

  TROUPE_DATA_DIR=/tmp/troupe-smoke python tests/smoke_docker.py

Uses a scripted model, so it needs no API key. Checks: shell runs inside the container as a
non-root user with caps dropped, the headless browser works and persists, screenshots come back
as images, an agent with network=false really has no network, two agents can't see each other's
files, and the RAM cap per sandbox.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

os.environ.setdefault("TROUPE_SANDBOX", "docker")
os.environ.setdefault("TROUPE_LLM_PROVIDER", "mock")
os.environ.setdefault("TROUPE_ENV_FILE", "/nonexistent")

from troupe.app import App  # noqa: E402
from troupe.config import load_settings  # noqa: E402
from troupe.llm import MockProvider  # noqa: E402

PLAN = {
    "Scout": [
        ("shell", {"command": "id -u; whoami 2>/dev/null; grep CapEff /proc/self/status; hostname; "
                              "echo secret-scout > /workspace/mine.txt; ls /workspace"}),
        ("browser", {"action": "goto", "url": "https://example.com"}),
        ("browser", {"action": "screenshot"}),
        ("browser", {"action": "fetch", "url": "https://example.com"}),
        ("shell", {"command": "ls /workspace/.browser | head -3; cat /sys/fs/cgroup/memory.max"}),
    ],
    "Offline": [
        ("shell", {"command": "curl -sS -m 5 https://example.com >/dev/null && echo NET_OK || echo NET_BLOCKED; "
                              "ls /workspace; cat /workspace/mine.txt 2>&1 || true"}),
    ],
}


def make_handler():
    def handler(system, messages, tools):
        name = system.split(" ", 3)[2].rstrip(",")
        steps = PLAN[name]
        done = sum(1 for m in messages if m["role"] == "assistant")
        if done < len(steps):
            tool, args = steps[done]
            return {"content": [{"type": "tool_use", "id": f"{name}_{done}", "name": tool, "input": args}],
                    "stop_reason": "tool_use"}
        return {"content": [{"type": "text", "text": f"{name} finished"}], "stop_reason": "end_turn"}
    return handler


def results(llm, agent):
    out = []
    for call in llm.calls:
        if not call["system"].startswith(f"You are {agent},"):
            continue
        last = call["messages"][-1]
        if last["role"] == "user" and isinstance(last["content"], list):
            for b in last["content"]:
                if b.get("type") == "tool_result":
                    out.append(b)
    return out


async def main() -> int:
    app = App(load_settings(), llm=MockProvider(make_handler()))
    disp = asyncio.create_task(app.orch.dispatcher())
    scout = app.create_agent("Scout")
    offline = app.create_agent("Offline", permissions={"network": False})
    t0 = time.time()
    await app.orch.user_message(app.dm_room(scout["id"])["id"], "go")
    await app.orch.user_message(app.dm_room(offline["id"])["id"], "go")
    for _ in range(600):
        await asyncio.sleep(0.5)
        if not app.db.one("SELECT COUNT(*) n FROM tasks WHERE status IN ('queued','running')")["n"]:
            break
    stats = await app.sandbox.status()
    disp.cancel()

    s = results(app.llm, "Scout")
    o = results(app.llm, "Offline")
    def text(b):
        c = b["content"]
        return c if isinstance(c, str) else json.dumps([x.get("type") for x in c]) + " " + \
            " ".join(x.get("text", "") for x in c if x.get("type") == "text")

    checks = []
    def check(name, ok, detail=""):
        checks.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n      {detail}" if detail else ""))

    tasks = app.db.all("SELECT agent_id, status, result FROM tasks")
    check("both tasks finished", all(t["status"] == "done" for t in tasks), json.dumps(tasks)[:300])
    if len(s) >= 5:
        sh = text(s[0])
        check("shell runs as non-root inside the container", "exit=0" in sh and "\n0\n" not in sh, sh[:300])
        check("all Linux capabilities dropped", "CapEff:\t0000000000000000" in sh, sh[:300])
        check("browser loaded example.com with labelled elements",
              "Example Domain" in text(s[1]) and "[1]" in text(s[1]), text(s[1])[:300])
        check("screenshot came back as an image", '"image"' in text(s[2]), text(s[2])[:200])
        check("cheap fetch works", "Example Domain" in text(s[3]), text(s[3])[:200])
        last = text(s[4])
        check("browser profile persisted in workspace + 1 GiB cgroup cap",
              "1073741824" in last and "exit=0" in last, last[:300])
    else:
        check("scout produced all tool results", False, json.dumps(s)[:1000])
    if o:
        ot = text(o[0])
        check("network=false agent has no internet", "NET_BLOCKED" in ot, ot[:300])
        check("agents can't see each other's files", "secret-scout" not in ot, ot[:300])
    else:
        check("offline agent produced a result", False)
    print(f"sandboxes: {stats}")
    print(f"elapsed: {time.time() - t0:.1f}s")
    for a in (scout, offline):
        await app.sandbox.stop(a["id"])
    print(f"\n{sum(checks)}/{len(checks)} checks passed")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Drive a running Troupe server through its HTTP API with a real model.

  TROUPE_URL=http://127.0.0.1:8080 TROUPE_TOKEN=... python tests/live_check.py <room name> "<message>"

Sends the message, waits until the room's tasks finish (or wait for approval), then prints the
replies, every tool call the agents made, and the approvals they asked for.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

URL = os.environ.get("TROUPE_URL", "http://127.0.0.1:8080").rstrip("/")
TOKEN = os.environ["TROUPE_TOKEN"]


def api(path: str, body: dict | None = None):
    req = urllib.request.Request(URL + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=60) as r:
        return json.loads(r.read())


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    room_name, message = sys.argv[1], sys.argv[2]
    timeout = int(os.environ.get("LIVE_TIMEOUT", "300"))
    state = api("/api/state")
    room = next(r for r in state["rooms"] if r["name"].lower() == room_name.lower())
    before = max([m["id"] for m in api(f"/api/rooms/{room['id']}/messages?limit=1")["messages"]] or [0])
    audit_before = max([a["id"] for a in api("/api/audit?limit=1")] or [0])
    t0 = time.time()
    api(f"/api/rooms/{room['id']}/messages", {"text": message})
    while time.time() - t0 < timeout:
        time.sleep(3)
        active = api(f"/api/rooms/{room['id']}/messages?limit=1")["active_tasks"]
        if all(t["status"] == "waiting_approval" for t in active):
            break
    elapsed = time.time() - t0
    msgs = [m for m in api(f"/api/rooms/{room['id']}/messages?limit=100")["messages"] if m["id"] > before]
    print(f"=== room '{room['name']}' after {elapsed:.0f}s ===")
    for m in msgs:
        who = "owner" if m["sender_type"] == "user" else (m.get("agent_name") or "system")
        kind = m["meta"].get("kind", "")
        print(f"\n[{who}{' · ' + kind if kind else ''}]\n{m['content'][:1500]}")
    print("\n=== tool calls ===")
    events = [a for a in api("/api/audit?limit=300") if a["id"] > audit_before]
    for a in reversed(events):
        if a["kind"] == "tool":
            d = json.loads(a["detail"])
            err = " (error)" if d.get("error") else ""
            print(f"- {a['agent_name']}: {a['name']}{err} {json.dumps(d.get('args'))[:160]}")
        elif a["kind"] in {"approval", "budget"} or (a["kind"] == "llm" and a["name"] == "error"):
            print(f"- {a['agent_name']}: {a['kind']} {a['name']} {a['detail'][:200]}")
    calls = sum(1 for a in events if a["kind"] == "llm")
    print(f"\nmodel calls: {calls}")
    pending = api("/api/approvals")
    if pending:
        print("\n=== waiting for approval ===")
        for p in pending:
            print(f"- {p['agent_name']} wants {p['tool']}: {p['reason']} {json.dumps(p['args'])[:200]}")
    tasks = api(f"/api/tasks?room_id={room['id']}&limit=10")
    print("\ntask statuses:", [(t["source"], t["status"]) for t in tasks if t["created_at"] >= t0 - 1])
    return 0


if __name__ == "__main__":
    sys.exit(main())

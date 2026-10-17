"""Per-agent isolated workspaces.

docker backend (default): every agent gets its own container (own filesystem, browser profile,
process space, optional no-network), started lazily, stopped when idle, and capped by
TROUPE_SANDBOX_MAX_ACTIVE so a small VPS never runs out of RAM. Set TROUPE_SANDBOX_RUNTIME=runsc
to run them under gVisor (stronger isolation, works on VPSes without KVM).

local backend: plain subprocesses in the agent's workspace folder. For development only; no isolation.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("troupe.sandbox")
MAX_OUTPUT = 20_000
HELPER_DIR = Path(__file__).resolve().parent.parent / "sandbox"


@dataclass
class ExecResult:
    code: int
    stdout: str
    stderr: str

    def summary(self) -> str:
        out = self.stdout
        if self.stderr.strip():
            out += ("\n" if out else "") + "[stderr]\n" + self.stderr
        if len(out) > MAX_OUTPUT:
            out = out[:MAX_OUTPUT // 2] + "\n...[truncated]...\n" + out[-MAX_OUTPUT // 2:]
        return f"exit={self.code}\n{out}".strip()


async def _run(argv: list[str], timeout: float, stdin: bytes | None = None, cwd: str | None = None,
               env: dict | None = None) -> ExecResult:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd, env=env)
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ExecResult(124, "", f"timed out after {timeout:.0f}s")
    return ExecResult(proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))


class SandboxManager:
    def __init__(self, settings):
        self.s = settings
        self.backend = settings.sandbox_backend
        self.last_used: dict[str, float] = {}
        self.in_use: dict[str, int] = {}
        self.network: dict[str, bool] = {}
        self._lock = asyncio.Lock()
        self._slot_free = asyncio.Condition()
        if self.backend == "docker" and not shutil.which("docker"):
            raise RuntimeError("TROUPE_SANDBOX=docker but the docker CLI was not found. "
                               "Install Docker or set TROUPE_SANDBOX=local for development.")
        if self.backend == "local":
            log.warning("Sandbox backend is 'local': agent commands run directly on this machine "
                        "without isolation. Use docker in production.")

    def workspace(self, agent_id: str) -> Path:
        p = self.s.workspaces_dir / agent_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def container(agent_id: str) -> str:
        return f"troupe-sbx-{agent_id}"

    # --- lifecycle ----------------------------------------------------------
    async def _running(self) -> set[str]:
        r = await _run(["docker", "ps", "--filter", "label=troupe=1", "--format", "{{.Names}}"], 30)
        return {n.strip() for n in r.stdout.splitlines() if n.strip()}

    async def _ensure(self, agent_id: str, network: bool) -> None:
        name = self.container(agent_id)
        async with self._lock:
            running = await self._running()
            if name in running and self.network.get(agent_id, network) == network:
                return
            if name in running:  # network policy changed -> recreate
                await _run(["docker", "rm", "-f", name], 60)
                running.discard(name)
            # Respect the RAM cap: evict the least recently used idle sandbox.
            while len(running) >= self.s.sandbox_max_active:
                idle = [n for n in running if self.in_use.get(n.removeprefix("troupe-sbx-"), 0) == 0]
                if not idle:
                    self._lock.release()
                    try:
                        async with self._slot_free:
                            await asyncio.wait_for(self._slot_free.wait(), timeout=300)
                    finally:
                        await self._lock.acquire()
                    running = await self._running()
                    continue
                victim = min(idle, key=lambda n: self.last_used.get(n.removeprefix("troupe-sbx-"), 0))
                log.info("evicting idle sandbox %s", victim)
                await _run(["docker", "stop", "-t", "5", victim], 60)
                running.discard(victim)

            await _run(["docker", "rm", "-f", name], 30)
            ws = self.workspace(agent_id)
            argv = ["docker", "run", "-d", "--rm", "--name", name, "--label", "troupe=1",
                    "--hostname", agent_id[:30],
                    "--memory", self.s.sandbox_memory, "--memory-swap", self.s.sandbox_memory,
                    "--cpus", self.s.sandbox_cpus, "--pids-limit", "512", "--shm-size", "256m",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "-v", f"{ws}:/workspace", "-w", "/workspace",
                    "-e", "HOME=/workspace", "-e", f"TROUPE_UA={self.s.agent_user_agent}",
                    "-e", f"TROUPE_BLOCKED_DOMAINS={','.join(self.s.blocked_domains)}"]
            if hasattr(os, "getuid"):
                argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
            if self.s.sandbox_runtime:
                argv += ["--runtime", self.s.sandbox_runtime]
            if not network:
                argv += ["--network", "none"]
            argv += [self.s.sandbox_image, "sleep", "infinity"]
            r = await _run(argv, 120)
            if r.code != 0:
                raise RuntimeError(f"could not start sandbox: {r.stderr.strip()}")
            self.network[agent_id] = network

    async def stop(self, agent_id: str) -> None:
        if self.backend == "docker":
            await _run(["docker", "rm", "-f", self.container(agent_id)], 60)

    async def reaper(self) -> None:
        """Stop sandboxes that have been idle for a while, to give RAM back."""
        while True:
            await asyncio.sleep(60)
            if self.backend != "docker":
                continue
            try:
                cutoff = time.time() - self.s.sandbox_idle_minutes * 60
                for name in await self._running():
                    aid = name.removeprefix("troupe-sbx-")
                    if self.in_use.get(aid, 0) == 0 and self.last_used.get(aid, 0) < cutoff:
                        log.info("stopping idle sandbox %s", name)
                        await _run(["docker", "stop", "-t", "5", name], 60)
            except Exception:  # never let the reaper die
                log.exception("sandbox reaper error")

    # --- execution ----------------------------------------------------------
    async def exec(self, agent_id: str, argv: list[str], timeout: float = 120, stdin: str | None = None,
                   network: bool = True) -> ExecResult:
        self.in_use[agent_id] = self.in_use.get(agent_id, 0) + 1
        try:
            data = stdin.encode() if stdin is not None else None
            if self.backend == "local":
                ws = self.workspace(agent_id)
                env = {**os.environ, "HOME": str(ws), "TROUPE_UA": self.s.agent_user_agent,
                       "TROUPE_BLOCKED_DOMAINS": ",".join(self.s.blocked_domains),
                       "TROUPE_BROWSER_PORT": str(9333 + (hash(agent_id) % 500))}
                if argv and argv[0] == "troupe-browser":
                    argv = [sys.executable, str(HELPER_DIR / "agent_browser.py"), *argv[1:]]
                elif argv[:2] == ["bash", "-lc"] and os.name == "nt":
                    argv = ["cmd", "/c", argv[2]]
                return await _run(argv, timeout, data, cwd=str(ws), env=env)
            await self._ensure(agent_id, network)
            name = self.container(agent_id)
            full = ["docker", "exec", *(["-i"] if data is not None else []), name,
                    "timeout", str(int(timeout)), *argv]
            return await _run(full, timeout + 15, data)
        finally:
            self.in_use[agent_id] -= 1
            self.last_used[agent_id] = time.time()
            async with self._slot_free:
                self._slot_free.notify_all()

    async def status(self) -> list[dict]:
        if self.backend != "docker":
            return []
        names = sorted(await self._running())
        if not names:
            return []
        r = await _run(["docker", "stats", "--no-stream", "--format",
                        "{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}", *names], 30)
        rows = []
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0].startswith("troupe-sbx-"):
                rows.append({"agent_id": parts[0].removeprefix("troupe-sbx-"), "memory": parts[1], "cpu": parts[2]})
        return rows

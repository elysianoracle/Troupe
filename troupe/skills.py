"""Skills, in the open Agent Skills format: a folder with a SKILL.md (YAML front matter + instructions)
and optional scripts/ and references/ files. Skills written for Hermes Agent, OpenClaw/ClawHub or
agentskills.io install unchanged.

Where skills come from:
  library   - curated first-party skills shipped with Troupe (troupe/skills_library/)
  learned   - written by an agent after teach-by-demonstration or a task done well
  community - installed by the owner from a URL or an uploaded zip

Safety model (the part other agent platforms skip):
  - Every install is unpacked to a staging area, size-limited, path-checked, and scanned for risky
    patterns (pipe-to-shell, secret harvesting, exfiltration endpoints, hidden Unicode, prompt
    injection). High-risk findings block the install unless the owner explicitly overrides.
  - A skill declares what it needs (metadata.troupe.requires); undeclared needs are inferred and
    shown to the owner before install.
  - Skills never gain permissions: the agent's own policy still applies to every tool call.
  - Community skills are untrusted by default: a task that loads one is tainted, so every outbound
    action in it (email, SMS, purchases, connectors) needs the owner's approval.
  - Scripts run only inside the agent's own sandbox (the skill folder is copied to /workspace/.skills).
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

LIBRARY = Path(__file__).resolve().parent / "skills_library"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_SKILL_BYTES = 5 * 1024 * 1024
MAX_FILES = 200
TEXT_SUFFIXES = {".md", ".txt", ".py", ".sh", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".csv", ".html", ".css", ""}
KNOWN_NEEDS = {"network", "shell", "browser", "files", "email_send", "sms_send", "purchase", "schedule", "handoff"}

# (severity, label, regex). "high" blocks an install unless the owner overrides.
RISK_PATTERNS = [
    ("high", "downloads and runs code (pipe to shell)", r"(curl|wget)[^\n|]*\|\s*(sudo\s+)?(ba|z)?sh\b"),
    ("high", "runs decoded data", r"base64\s+(-d|--decode)[^\n]*\|\s*(ba|z)?sh|eval\s*\(\s*(atob|base64|Buffer\.from)"),
    ("high", "reads SSH keys or cloud credentials", r"\.ssh/(id_|authorized_keys)|\.aws/credentials|\.config/gcloud|/etc/shadow|\.kube/config"),
    ("high", "touches browser or keychain secrets", r"Login Data|Cookies\.sqlite|keychain|security\s+find-generic-password|\.gnupg"),
    ("high", "crypto wallet secrets", r"seed\s+phrase|mnemonic\s+phrase|wallet\.dat|private\s+key\s+(of|for)\s+(the\s+)?wallet"),
    ("high", "sends data to a collection endpoint", r"webhook\.site|requestbin|pipedream\.net|ngrok\.io|discord(app)?\.com/api/webhooks|pastebin\.com/api|transfer\.sh"),
    ("high", "reverse shell", r"(nc|ncat|netcat)\s+-e\s|/dev/tcp/\d|bash\s+-i\s+>&|socat\s+exec"),
    ("high", "hides instructions from the owner", r"(do\s+not|don't|never)\s+(tell|inform|show|mention)\s+(the\s+)?(user|owner|human)"),
    ("high", "tries to override the agent's rules", r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|rules)|disregard\s+your\s+(instructions|guidelines)"),
    ("medium", "dumps environment variables", r"\bprintenv\b|os\.environ(?!\.get)|\benv\s*>|process\.env\b(?!\.)"),
    ("medium", "destructive file commands", r"rm\s+-rf\s+(/|~|\$HOME)(\s|$)|mkfs\.|dd\s+if=.*of=/dev/"),
    ("medium", "installs software", r"\b(pip|pip3|npm|apt|apt-get|brew)\s+install\b"),
    ("low", "makes web requests", r"https?://|requests\.(get|post)|urllib|fetch\(|curl\s|wget\s"),
]
HIDDEN_UNICODE = re.compile("[​-‏‪-‮⁦-⁩﻿]")


class SkillError(ValueError):
    pass


@dataclass
class Finding:
    severity: str
    label: str
    file: str
    line: int
    excerpt: str


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    source: str                      # library | learned | community
    origin: str = ""                 # URL, "library", or the agent that learned it
    version: str = ""
    requires: list[str] = field(default_factory=list)
    params: list[str] = field(default_factory=list)
    trusted: bool = False
    sha256: str = ""
    installed_at: float = 0.0
    files: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {"name": self.name, "description": self.description, "source": self.source, "origin": self.origin,
                "version": self.version, "requires": self.requires, "params": self.params, "trusted": self.trusted,
                "sha256": self.sha256[:12], "installed_at": self.installed_at, "files": self.files}


# --- parsing -------------------------------------------------------------------------------------
def parse_skill_md(text: str) -> tuple[dict, str]:
    text = text.lstrip("﻿")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        raise SkillError("SKILL.md must start with YAML front matter between --- lines")
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise SkillError(f"invalid YAML front matter: {e}") from e
    if not isinstance(meta, dict):
        raise SkillError("front matter must be a mapping")
    return meta, m.group(2).strip()


def troupe_meta(meta: dict) -> dict:
    md = meta.get("metadata") or {}
    t = md.get("troupe") if isinstance(md, dict) else None
    return t if isinstance(t, dict) else {}


def validate(meta: dict) -> tuple[str, str]:
    name = str(meta.get("name", "")).strip()
    desc = str(meta.get("description", "")).strip()
    if not NAME_RE.match(name):
        raise SkillError("name must be lowercase letters, digits and hyphens (max 64), e.g. inbox-triage")
    if not desc or len(desc) > 1024:
        raise SkillError("description is required (max 1024 characters)")
    return name, desc


def write_skill_md(meta: dict, body: str) -> str:
    return "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip() + "\n---\n\n" + body.strip() + "\n"


def folder_hash(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(folder).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


# --- scanning ------------------------------------------------------------------------------------
def scan(folder: Path) -> tuple[list[Finding], list[str]]:
    """Returns (findings, inferred needs)."""
    findings: list[Finding] = []
    needs: set[str] = set()
    for p in sorted(folder.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(folder).as_posix()
        if p.suffix.lower() not in TEXT_SUFFIXES:
            findings.append(Finding("medium", "contains a binary or unknown file type", rel, 0, p.suffix))
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if rel.startswith("scripts/") or p.suffix in {".py", ".sh", ".js", ".ts"}:
            needs.add("shell")
        for i, line in enumerate(text.splitlines(), 1):
            if HIDDEN_UNICODE.search(line):
                findings.append(Finding("high", "hidden Unicode characters (can conceal instructions)", rel, i, ""))
            for sev, label, rx in RISK_PATTERNS:
                if re.search(rx, line, re.I):
                    if sev == "low":
                        needs.add("network")
                        continue
                    findings.append(Finding(sev, label, rel, i, line.strip()[:160]))
        low = text.lower()
        if "send_email" in low or "email" in low and "send" in low:
            needs.add("email_send")
        if "browser" in low:
            needs.add("browser")
        if "request_purchase" in low or "buy " in low:
            needs.add("purchase")
        if "schedule_task" in low:
            needs.add("schedule")
    # One finding per (label, file) is enough to show the owner.
    seen, unique = set(), []
    for f in findings:
        if (f.label, f.file) not in seen:
            seen.add((f.label, f.file))
            unique.append(f)
    return unique, sorted(needs)


# --- the store -----------------------------------------------------------------------------------
class SkillStore:
    def __init__(self, data_dir: Path, db):
        self.root = data_dir / "skills"
        self.staging = data_dir / "skills-staging"
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        self.db = db

    # registry: one kv record per installed skill
    def _rec(self, name: str) -> dict | None:
        raw = self.db.kv_get(f"skill:{name}")
        return json.loads(raw) if raw else None

    def _save_rec(self, name: str, rec: dict) -> None:
        self.db.kv_set(f"skill:{name}", json.dumps(rec))

    def get(self, name: str) -> Skill | None:
        rec = self._rec(name)
        folder = self.root / name
        if not rec or not (folder / "SKILL.md").is_file():
            return None
        meta, _ = parse_skill_md((folder / "SKILL.md").read_text(encoding="utf-8"))
        t = troupe_meta(meta)
        return Skill(name=name, description=str(meta.get("description", "")), path=folder, source=rec["source"],
                     origin=rec.get("origin", ""), version=str(t.get("version", meta.get("version", "")) or ""),
                     requires=list(rec.get("requires", [])), params=list(t.get("params", []) or []),
                     trusted=bool(rec.get("trusted")), sha256=rec.get("sha256", ""),
                     installed_at=rec.get("installed_at", 0),
                     files=sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()))

    def installed(self) -> list[Skill]:
        out = []
        for row in self.db.all("SELECT key FROM kv WHERE key LIKE 'skill:%' ORDER BY key"):
            s = self.get(row["key"][6:])
            if s:
                out.append(s)
        return out

    def body(self, name: str) -> str:
        s = self.get(name)
        if not s:
            raise SkillError(f"no skill named {name}")
        return parse_skill_md((s.path / "SKILL.md").read_text(encoding="utf-8"))[1]

    def read_file(self, name: str, rel: str) -> str:
        s = self.get(name)
        if not s:
            raise SkillError(f"no skill named {name}")
        p = (s.path / rel).resolve()
        if not p.is_relative_to(s.path.resolve()) or not p.is_file():
            raise SkillError("no such file in that skill")
        return p.read_text(encoding="utf-8", errors="replace")[:60_000]

    # assignment to agents ("*" = every agent)
    def agents_for(self, name: str) -> list[str]:
        rec = self._rec(name) or {}
        return list(rec.get("agents", ["*"]))

    def set_agents(self, name: str, agents: list[str]) -> None:
        rec = self._rec(name)
        if not rec:
            raise SkillError(f"no skill named {name}")
        rec["agents"] = agents or []
        self._save_rec(name, rec)

    def set_trusted(self, name: str, trusted: bool) -> None:
        rec = self._rec(name)
        if not rec:
            raise SkillError(f"no skill named {name}")
        rec["trusted"] = bool(trusted)
        self._save_rec(name, rec)
        self.db.audit("skills", "trusted" if trusted else "untrusted", {"skill": name})

    def for_agent(self, agent_id: str) -> list[Skill]:
        return [s for s in self.installed() if "*" in self.agents_for(s.name) or agent_id in self.agents_for(s.name)]

    # --- the library ----------------------------------------------------------------------------
    def library(self) -> list[dict]:
        out = []
        for folder in sorted(LIBRARY.iterdir()) if LIBRARY.exists() else []:
            md = folder / "SKILL.md"
            if not md.is_file():
                continue
            meta, _ = parse_skill_md(md.read_text(encoding="utf-8"))
            t = troupe_meta(meta)
            out.append({"name": meta.get("name"), "description": meta.get("description"),
                        "requires": t.get("requires", []), "category": t.get("category", "general"),
                        "version": str(t.get("version", "")), "installed": bool(self._rec(str(meta.get("name"))))})
        return out

    def install_library(self, name: str, agents: list[str] | None = None) -> Skill:
        folder = LIBRARY / name
        if not (folder / "SKILL.md").is_file():
            raise SkillError(f"no library skill named {name}")
        return self._install_folder(folder, source="library", origin="library", trusted=True, agents=agents or ["*"])

    def install_all_library(self) -> int:
        n = 0
        for item in self.library():
            if not item["installed"]:
                self.install_library(item["name"])
                n += 1
        return n

    # --- community installs: inspect first, then install ----------------------------------------
    async def fetch(self, url: str) -> tuple[bytes, str]:
        """Download a skill. Returns (bytes, subfolder within the archive or '')."""
        url = url.strip()
        p = urlparse(url)
        if p.scheme != "https":
            raise SkillError("use an https:// link")
        m = re.match(r"^/([^/]+)/([^/]+)/tree/([^/]+)/(.+)$", p.path) if p.netloc == "github.com" else None
        if m:  # a folder in a GitHub repository
            url = f"https://codeload.github.com/{m[1]}/{m[2]}/zip/refs/heads/{m[3]}"
            subdir = m[4].strip("/")
        elif p.netloc == "github.com" and re.match(r"^/[^/]+/[^/]+/?$", p.path):
            owner, repo = p.path.strip("/").split("/")[:2]
            url = f"https://codeload.github.com/{owner}/{repo}/zip/HEAD"
            subdir = ""
        else:
            subdir = ""
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as http:
            async with http.stream("GET", url, headers={"User-Agent": "Troupe-skill-installer"}) as r:
                if r.status_code != 200:
                    raise SkillError(f"download failed: HTTP {r.status_code}")
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > 40 * 1024 * 1024:
                        raise SkillError("download is larger than 40 MB")
        return bytes(buf), subdir

    def stage(self, data: bytes, origin: str, subdir: str = "") -> dict:
        """Unpack a zip (or a lone SKILL.md) safely and report what it contains. Nothing is installed."""
        sid = hashlib.sha256(data).hexdigest()[:16]
        dest = self.staging / sid
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        if data[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                total = 0
                infos = [i for i in z.infolist() if not i.is_dir()]
                if len(infos) > 5000:
                    raise SkillError("archive has too many files")
                for info in infos:
                    if (info.external_attr >> 16) & 0o170000 == 0o120000:
                        continue  # never extract symlinks
                    target = (dest / info.filename).resolve()
                    if not target.is_relative_to(dest.resolve()):
                        raise SkillError("archive tries to write outside its folder")
                    total += info.file_size
                    if total > 40 * 1024 * 1024:
                        raise SkillError("archive unpacks to more than 40 MB")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(z.read(info))
        else:
            (dest / "SKILL.md").write_bytes(data[:MAX_SKILL_BYTES])
        # Find the skill folder(s): honour a GitHub /tree/<path>, else every folder with a SKILL.md.
        candidates = [p.parent for p in dest.rglob("SKILL.md")]
        if subdir:
            candidates = [c for c in candidates if c.as_posix().endswith("/" + subdir) or
                          c.as_posix().endswith(subdir.split("/")[-1])] or candidates
        if not candidates:
            raise SkillError("no SKILL.md found")
        if len(candidates) > 50:
            raise SkillError("too many skills in one archive (max 50)")
        reports = [self.inspect_folder(c, origin) for c in candidates]
        rec = {"id": sid, "origin": origin, "skills": reports, "created": time.time()}
        (dest / ".staging.json").write_text(json.dumps(rec), encoding="utf-8")
        return rec

    def inspect_folder(self, folder: Path, origin: str) -> dict:
        files = [p for p in folder.rglob("*") if p.is_file() and p.name != ".staging.json"]
        size = sum(p.stat().st_size for p in files)
        meta, body = parse_skill_md((folder / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
        name, desc = validate(meta)
        problems = []
        if size > MAX_SKILL_BYTES:
            problems.append(f"skill is {size // 1024} KB (max {MAX_SKILL_BYTES // 1024} KB)")
        if len(files) > MAX_FILES:
            problems.append(f"skill has {len(files)} files (max {MAX_FILES})")
        findings, inferred = scan(folder)
        declared = [str(x) for x in (troupe_meta(meta).get("requires") or []) if str(x) in KNOWN_NEEDS]
        undeclared = sorted(set(inferred) - set(declared))
        high = [f for f in findings if f.severity == "high"]
        return {"name": name, "description": desc, "folder": folder.relative_to(self.staging).as_posix(),
                "files": sorted(p.relative_to(folder).as_posix() for p in files), "size": size,
                "declared": declared, "undeclared": undeclared, "requires": sorted(set(declared) | set(inferred)),
                "findings": [f.__dict__ for f in findings], "blocked": bool(high or problems), "problems": problems,
                "exists": bool(self._rec(name)), "preview": body[:1200]}

    def install_staged(self, staging_id: str, names: list[str] | None = None, agents: list[str] | None = None,
                       override: bool = False) -> list[Skill]:
        if not re.fullmatch(r"[0-9a-f]{16}", staging_id or ""):
            raise SkillError("bad staging id")
        meta_path = self.staging / staging_id / ".staging.json"
        if not meta_path.is_file():
            raise SkillError("that inspection expired; inspect the skill again")
        rec = json.loads(meta_path.read_text(encoding="utf-8"))
        out = []
        for rep in rec["skills"]:
            if names and rep["name"] not in names:
                continue
            if rep["blocked"] and not override:
                raise SkillError(f"{rep['name']} was blocked by the safety scan; review the findings first")
            folder = self.staging / rep["folder"]
            # Re-scan right before installing: the staged files must still match the report.
            fresh = self.inspect_folder(folder, rec["origin"])
            if fresh["blocked"] and not override:
                raise SkillError(f"{rep['name']} failed the safety scan")
            out.append(self._install_folder(folder, source="community", origin=rec["origin"], trusted=False,
                                            agents=agents or ["*"], requires=fresh["requires"],
                                            findings=len(fresh["findings"]), override=override))
        shutil.rmtree(self.staging / staging_id, ignore_errors=True)
        return out

    def _install_folder(self, folder: Path, source: str, origin: str, trusted: bool, agents: list[str],
                        requires: list[str] | None = None, findings: int = 0, override: bool = False) -> Skill:
        meta, _ = parse_skill_md((folder / "SKILL.md").read_text(encoding="utf-8"))
        name, _ = validate(meta)
        if requires is None:
            requires = sorted(set(str(x) for x in troupe_meta(meta).get("requires") or []) & KNOWN_NEEDS)
        dest = self.root / name
        prev = self._rec(name)
        if prev and prev["source"] == "library" and source == "community":
            raise SkillError(f"{name} is a built-in skill; rename the community skill to install it alongside")
        tmp = self.root / f".{name}.new"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(folder, tmp, ignore=shutil.ignore_patterns(".staging.json", ".git", "__pycache__"),
                        symlinks=False)
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)
        rec = {"source": source, "origin": origin, "trusted": trusted, "agents": agents, "requires": requires,
               "sha256": folder_hash(dest), "installed_at": time.time(), "findings": findings}
        if prev:  # keep the owner's assignment and trust choices on reinstall/update
            rec["agents"] = prev.get("agents", agents)
            rec["trusted"] = prev.get("trusted", trusted) if source == prev["source"] else trusted
        self._save_rec(name, rec)
        self.db.audit("skills", "installed", {"skill": name, "source": source, "origin": origin,
                                              "requires": requires, "override": override})
        return self.get(name)

    def uninstall(self, name: str) -> None:
        if not NAME_RE.match(name or ""):
            raise SkillError("bad skill name")
        shutil.rmtree(self.root / name, ignore_errors=True)
        self.db.execute("DELETE FROM kv WHERE key = ?", (f"skill:{name}",))
        self.db.audit("skills", "uninstalled", {"skill": name})

    # --- learned skills (written by agents) ---------------------------------------------------------
    def save_learned(self, agent: dict, name: str, description: str, params: list, playbook: str,
                     trace: str = "") -> Skill:
        slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:64] or "skill"
        existing = self._rec(slug)
        if existing and existing["source"] != "learned":
            slug = f"{slug}-{agent['name'].lower()}"[:64]  # never overwrite a library or community skill
        meta = {"name": slug, "description": (description or f"Learned by {agent['name']}")[:1024],
                "metadata": {"troupe": {"version": time.strftime("%Y.%m.%d"), "params": [str(p) for p in params or []],
                                        "learned_by": agent["name"]}}}
        folder = self.staging / f"learned-{slug}"
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(write_skill_md(meta, playbook), encoding="utf-8")
        if trace:
            (folder / "references").mkdir()
            (folder / "references" / "demonstration.md").write_text(trace[:60_000], encoding="utf-8")
        skill = self._install_folder(folder, source="learned", origin=agent["name"], trusted=True,
                                     agents=[agent["id"]], requires=scan(folder)[1])
        shutil.rmtree(folder, ignore_errors=True)
        return skill

    def migrate_db_skills(self) -> int:
        """One-time: move playbooks from the old `skills` table into SKILL.md folders."""
        if self.db.kv_get("skills:migrated") == "1":
            return 0
        n = 0
        for row in self.db.all("SELECT s.*, a.name AS agent_name FROM skills s JOIN agents a ON a.id = s.agent_id"):
            try:
                self.save_learned({"id": row["agent_id"], "name": row["agent_name"]}, row["name"],
                                  row["description"], json.loads(row["params"] or "[]"), row["playbook"],
                                  row["source_trace"] or "")
                n += 1
            except SkillError:
                pass
        self.db.kv_set("skills:migrated", "1")
        return n

    def sync_to_workspace(self, name: str, workspace: Path) -> Path:
        """Copy a skill into the agent's sandbox workspace so its scripts run in isolation."""
        s = self.get(name)
        if not s:
            raise SkillError(f"no skill named {name}")
        dest = workspace / ".skills" / name
        stamp = dest / ".sha256"
        if not (stamp.is_file() and stamp.read_text() == s.sha256):
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(s.path, dest, symlinks=False)
            stamp.write_text(s.sha256)
        return dest

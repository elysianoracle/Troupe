"""Settings, read from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _strip_inline_comments() -> None:
    """systemd's EnvironmentFile keeps 'VALUE   # comment' verbatim; drop the comment part."""
    for key in [k for k in os.environ if k.startswith(("TROUPE_", "OPENAI_", "TWILIO_", "ANTHROPIC_", "AGENTMAIL_"))]:
        cleaned = re.sub(r"\s+#.*$", "", os.environ[key]).strip()
        if cleaned != os.environ[key]:
            os.environ[key] = cleaned


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass
class Settings:
    data_dir: Path
    host: str
    port: int
    admin_token: str
    public_url: str

    # LLM
    llm_provider: str            # anthropic | openai_compat | mock
    default_model: str
    effort: str
    enable_fallbacks: bool
    enable_web_search: bool
    openai_base_url: str
    openai_api_key: str

    # Sandbox
    sandbox_backend: str         # docker | local
    sandbox_image: str
    sandbox_runtime: str         # "" (runc) or "runsc" for gVisor
    sandbox_memory: str
    sandbox_cpus: str
    sandbox_max_active: int
    sandbox_idle_minutes: int

    # Concurrency
    max_parallel_tasks: int
    max_steps_per_task: int
    max_hops_per_goal: int

    # Email (self-hosted IMAP/SMTP; plus-addressing routes mail to agents)
    email_address: str
    imap_host: str
    imap_user: str
    imap_password: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    email_poll_seconds: int
    email_daily_limit: int

    # Telegram
    telegram_token: str
    telegram_owner_id: str

    # Twilio SMS (optional, off by default)
    twilio_sid: str
    twilio_token: str
    twilio_from: str

    agent_user_agent: str
    agentmail_api_key: str = ""
    agentmail_base_url: str = "https://api.agentmail.to/v0"
    agentmail_domain: str = ""
    agentmail_prefix: str = ""
    blocked_domains: list[str] = field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "troupe.db"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"


def load_settings() -> Settings:
    _load_dotenv(Path(os.environ.get("TROUPE_ENV_FILE", ".env")))
    _strip_inline_comments()
    data_dir = Path(os.environ.get("TROUPE_DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    token = os.environ.get("TROUPE_ADMIN_TOKEN", "")
    if not token:
        token_file = data_dir / "admin_token"
        if token_file.exists():
            token = token_file.read_text().strip()
        else:
            token = secrets.token_urlsafe(24)
            token_file.write_text(token)
            token_file.chmod(0o600)

    provider = os.environ.get("TROUPE_LLM_PROVIDER", "anthropic")
    default_model = os.environ.get(
        "TROUPE_MODEL", "claude-opus-5-5" if provider == "anthropic" else "qwen3:4b"
    )
    return Settings(
        data_dir=data_dir,
        host=os.environ.get("TROUPE_HOST", "127.0.0.1"),
        port=_int("TROUPE_PORT", 8080),
        admin_token=token,
        public_url=os.environ.get("TROUPE_PUBLIC_URL", "http://localhost:8080").rstrip("/"),
        llm_provider=provider,
        default_model=default_model,
        effort=os.environ.get("TROUPE_EFFORT", "medium"),
        enable_fallbacks=_bool("TROUPE_FALLBACKS", True),
        enable_web_search=_bool("TROUPE_WEB_SEARCH", True),
        openai_base_url=os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1"),
        openai_api_key=os.environ.get("OPENAI_API_KEY", "ollama"),
        sandbox_backend=os.environ.get("TROUPE_SANDBOX", "docker"),
        sandbox_image=os.environ.get("TROUPE_SANDBOX_IMAGE", "troupe-sandbox:latest"),
        sandbox_runtime=os.environ.get("TROUPE_SANDBOX_RUNTIME", ""),
        sandbox_memory=os.environ.get("TROUPE_SANDBOX_MEMORY", "1g"),
        sandbox_cpus=os.environ.get("TROUPE_SANDBOX_CPUS", "1.0"),
        sandbox_max_active=_int("TROUPE_SANDBOX_MAX_ACTIVE", 4),
        sandbox_idle_minutes=_int("TROUPE_SANDBOX_IDLE_MINUTES", 10),
        max_parallel_tasks=_int("TROUPE_MAX_PARALLEL_TASKS", 3),
        max_steps_per_task=_int("TROUPE_MAX_STEPS", 40),
        max_hops_per_goal=_int("TROUPE_MAX_HOPS", 12),
        email_address=os.environ.get("TROUPE_EMAIL_ADDRESS", ""),
        imap_host=os.environ.get("TROUPE_IMAP_HOST", ""),
        imap_user=os.environ.get("TROUPE_IMAP_USER", ""),
        imap_password=os.environ.get("TROUPE_IMAP_PASSWORD", ""),
        smtp_host=os.environ.get("TROUPE_SMTP_HOST", ""),
        smtp_port=_int("TROUPE_SMTP_PORT", 587),
        smtp_user=os.environ.get("TROUPE_SMTP_USER", ""),
        smtp_password=os.environ.get("TROUPE_SMTP_PASSWORD", ""),
        email_poll_seconds=_int("TROUPE_EMAIL_POLL_SECONDS", 120),
        email_daily_limit=_int("TROUPE_EMAIL_DAILY_LIMIT", 30),
        telegram_token=os.environ.get("TROUPE_TELEGRAM_TOKEN", ""),
        telegram_owner_id=os.environ.get("TROUPE_TELEGRAM_OWNER_ID", ""),
        twilio_sid=os.environ.get("TWILIO_ACCOUNT_SID", ""),
        twilio_token=os.environ.get("TWILIO_AUTH_TOKEN", ""),
        twilio_from=os.environ.get("TWILIO_FROM_NUMBER", ""),
        agent_user_agent=os.environ.get(
            "TROUPE_AGENT_UA",
            "Mozilla/5.0 (X11; Linux x86_64) TroupeAgent/0.1 (automated AI assistant acting for its owner)",
        ),
        agentmail_api_key=os.environ.get("AGENTMAIL_API_KEY", ""),
        agentmail_base_url=os.environ.get("AGENTMAIL_BASE_URL", "https://api.agentmail.to/v0"),
        agentmail_domain=os.environ.get("TROUPE_AGENTMAIL_DOMAIN", ""),
        agentmail_prefix=os.environ.get("TROUPE_AGENTMAIL_PREFIX", ""),
        blocked_domains=[d.strip() for d in os.environ.get("TROUPE_BLOCKED_DOMAINS", "").split(",") if d.strip()],
    )

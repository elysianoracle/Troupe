"""Encrypted secrets. The backend uses these on an agent's behalf; the model never sees the values.

Values are only ever handed to core-side code (SMTP, MCP server env, type_secret into a password
field on an allowed domain). Nothing returns a secret value into an LLM context.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet

from .db import DB, now


class Vault:
    def __init__(self, db: DB, data_dir: Path):
        self.db = db
        key = os.environ.get("TROUPE_VAULT_KEY", "").encode()
        if not key:
            key_file = data_dir / "vault.key"
            if key_file.exists():
                key = key_file.read_bytes().strip()
            else:
                key = Fernet.generate_key()
                key_file.write_bytes(key)
                key_file.chmod(0o600)
        self._f = Fernet(key)

    def set(self, name: str, value: str, domains: list[str] | None = None) -> None:
        ct = self._f.encrypt(value.encode())
        self.db.execute(
            "INSERT INTO secrets(name, ciphertext, domains, created_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET ciphertext = excluded.ciphertext, domains = excluded.domains",
            (name, ct, json.dumps(domains or []), now()),
        )

    def get(self, name: str) -> str | None:
        row = self.db.one("SELECT ciphertext FROM secrets WHERE name = ?", (name,))
        return self._f.decrypt(row["ciphertext"]).decode() if row else None

    def domains(self, name: str) -> list[str]:
        row = self.db.one("SELECT domains FROM secrets WHERE name = ?", (name,))
        return json.loads(row["domains"]) if row else []

    def delete(self, name: str) -> None:
        self.db.execute("DELETE FROM secrets WHERE name = ?", (name,))

    def names(self) -> list[dict]:
        return self.db.all("SELECT name, domains, created_at FROM secrets ORDER BY name")

    def resolve_env(self, env: dict[str, str]) -> dict[str, str]:
        """Replace 'vault:NAME' values (used in connectors.json) with the secret."""
        out = {}
        for k, v in env.items():
            if isinstance(v, str) and v.startswith("vault:"):
                out[k] = self.get(v[6:]) or ""
            else:
                out[k] = v
        return out

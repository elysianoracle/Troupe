"""Entry point: `python -m troupe` (or the `troupe` console script)."""
from __future__ import annotations

import argparse
import asyncio
import logging

import uvicorn

from .app import App
from .config import load_settings
from .surfaces.web import build_api


def main() -> None:
    parser = argparse.ArgumentParser(prog="troupe")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "token", "seed", "password"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()

    if args.command == "token":
        print(settings.admin_token)
        return
    if args.command == "password":
        set_password(settings)
        return
    app = App(settings)
    if args.command == "seed":
        seed(app)
        return

    if app.setup_done:
        seed(app)
    api = build_api(app)
    if app.setup_done:
        print(f"\n  Troupe: {settings.public_url}\n")
    else:
        print(f"\n  Finish setting up Troupe: {settings.public_url}/?token={settings.admin_token}\n")
    uvicorn.run(api, host=settings.host, port=settings.port, log_level="warning")


def set_password(settings) -> None:
    """Set the owner's web login password (asked twice, never echoed). Signs out every session."""
    import getpass

    from .auth import MIN_PASSWORD_LENGTH, Auth
    from .db import DB

    pw = getpass.getpass(f"New Troupe password (at least {MIN_PASSWORD_LENGTH} characters): ")
    if len(pw) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"Too short. Use at least {MIN_PASSWORD_LENGTH} characters; nothing changed.")
    if getpass.getpass("Type it again: ") != pw:
        raise SystemExit("The two passwords didn't match; nothing changed.")
    Auth(DB(settings.db_path), settings.admin_token).set_password(pw)
    print(f"Password saved. Sign in at {settings.public_url} (all other sessions were signed out).")


def seed(app: App) -> None:
    """The personal starter team (Chief, Scout, Courier). The setup wizard offers other teams."""
    if app.db.one("SELECT id FROM agents LIMIT 1"):
        return
    from .teams import create_team
    create_team(app, "personal")
    logging.getLogger("troupe").info("created the personal team: Chief, Scout, Courier")


if __name__ == "__main__":
    main()

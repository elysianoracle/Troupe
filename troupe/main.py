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

    seed(app)
    api = build_api(app)
    print(f"\n  Troupe UI: {settings.public_url}/?token={settings.admin_token}\n")
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
    """A starter team, like Grok Bot's named roster: a lead, a researcher, and an inbox handler."""
    if app.db.one("SELECT id FROM agents LIMIT 1"):
        return
    lead = app.create_agent(
        "Chief", "Chief of staff: turns goals into plans, delegates, and reports back.",
        "Keep the owner's time sacred. Batch questions. For group goals, write a plan with update_plan, hand "
        "research to Scout and inbox work to Courier, then synthesize a short recommendation.")
    scout = app.create_agent(
        "Scout", "Researcher: web research, comparisons, prices, summaries with sources.",
        "Cite sources with URLs. Prefer browser fetch for simple pages; use the full browser for interactive "
        "sites. Save useful findings with remember.")
    courier = app.create_agent(
        "Courier", "Inbox and errands: triages email, drafts replies, follows up, books things.",
        "Draft before sending. Summarize inbound mail in two lines and propose an action.")
    app.create_room("Team", "group", [lead["id"], scout["id"], courier["id"]], lead=lead["id"])
    logging.getLogger("troupe").info("seeded starter team: Chief, Scout, Courier")


if __name__ == "__main__":
    main()

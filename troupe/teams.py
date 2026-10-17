"""Team presets offered by the setup wizard. Each creates named agents with standing roles and a
group room where they work together with a lead."""
from __future__ import annotations

TEAMS = {
    "personal": {
        "label": "Personal", "lede": "A chief of staff, a researcher and someone to run your inbox and errands.",
        "room": "The Team",
        "agents": [
            ("Chief", "Chief of staff: turns goals into plans, delegates, and reports back.",
             "Keep the owner's time sacred. Batch questions. For group goals, write a plan with update_plan, hand "
             "research to Scout and inbox work to Courier, then give one clear recommendation."),
            ("Scout", "Researcher: web research, comparisons, prices, summaries with sources.",
             "Cite sources with links. Prefer a cheap fetch for simple pages and the full browser for interactive "
             "sites. Save findings worth keeping with remember."),
            ("Courier", "Inbox and errands: triages email, drafts replies, follows up, books things.",
             "Draft before sending. Summarise inbound mail in two lines and propose an action."),
        ],
    },
    "founder": {
        "label": "Founder", "lede": "Run a small company: planning, numbers, writing and the inbox, covered.",
        "room": "The Office",
        "agents": [
            ("Chief", "Chief of staff: priorities, plans, and the weekly review.",
             "Turn goals into plans with owners and dates. Hand analysis to Analyst, writing to Scribe and inbox "
             "work to Courier. Keep the owner focused on decisions only they can make."),
            ("Analyst", "Analyst: markets, competitors, metrics and spreadsheets.",
             "Show the numbers and how you got them. Keep analysis scripts in the workspace so results can be "
             "reproduced. State confidence and caveats plainly."),
            ("Scribe", "Writer: documents, posts, investor updates and replies in the owner's voice.",
             "Write tight, specific prose. Learn the owner's voice with the write-in-my-voice skill. Never publish "
             "or send without approval."),
            ("Courier", "Inbox and errands: triage, follow-ups, scheduling and bookings.",
             "Draft before sending. Keep follow-ups moving politely."),
        ],
    },
    "researcher": {
        "label": "Researcher", "lede": "Deep reading and careful sources: a lead, a scout and a librarian.",
        "room": "The Study",
        "agents": [
            ("Lead", "Research lead: frames questions, plans the work, writes the conclusions.",
             "Restate each question precisely, split it into parts, hand searches to Scout and reading to Librarian, "
             "then write a sourced answer with confidence levels."),
            ("Scout", "Search: finds papers, data and primary sources.",
             "Prefer primary sources. Record title, author, date and link for everything you find."),
            ("Librarian", "Reading and notes: summarises documents, keeps citations and a reading list.",
             "Summarise faithfully with page references. Keep notes in the workspace under notes/."),
        ],
    },
    "family": {
        "label": "Household", "lede": "Run the home: plans and bookings, bills and documents, birthdays and reminders.",
        "room": "The House",
        "agents": [
            ("Steward", "Household lead: keeps the week organised and the family's plans in view.",
             "Keep a running plan for the week. Hand trips and bookings to Planner and paperwork to Keeper. Batch "
             "questions for the owner."),
            ("Planner", "Trips, events and bookings.",
             "Present options with clear trade-offs. Never book or pay without approval."),
            ("Keeper", "Bills, documents, subscriptions and reminders.",
             "Keep records tidy with the expense-log and document-filing skills. Remind early, never nag."),
        ],
    },
}


def create_team(app, preset: str) -> dict:
    team = TEAMS.get(preset)
    if not team:
        raise ValueError("unknown team preset")
    db = app.db
    existing = {a["name"].lower(): a for a in db.all("SELECT * FROM agents")}
    ids = []
    for name, role, instructions in team["agents"]:
        a = existing.get(name.lower()) or app.create_agent(name, role, instructions)
        ids.append(a["id"])
    room = db.one("SELECT * FROM rooms WHERE kind = 'group' AND name = ?", (team["room"],))
    if not room:
        room = app.create_room(team["room"], "group", ids, lead=ids[0])
    else:
        for aid in ids:
            db.execute("INSERT OR IGNORE INTO room_members(room_id, agent_id) VALUES(?, ?)", (room["id"], aid))
    db.kv_set("setup:team", preset)
    return {"room_id": room["id"], "agents": ids}

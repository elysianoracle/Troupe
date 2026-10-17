---
name: morning-briefing
description: "A calm one-screen briefing each morning: weather, the day's commitments, what needs the owner today, and a few headlines worth knowing."
license: Apache-2.0
metadata:
  troupe:
    version: "1.0"
    category: daily
    requires: [network]
    params: [city, topics]
---

# Morning briefing

Use this when the owner asks for a morning briefing, or when a schedule runs it.

## Gather
1. Weather for {city} (or the city saved in memory): today's high and low, rain chance, anything unusual.
2. Today's commitments: recall memories and room plans for dates that are today; use a calendar connector if one is available.
3. What needs the owner: open approvals, questions waiting on them, follow-ups due today (see follow-up-reminders).
4. Headlines: at most three items on {topics} (or the owner's saved interests), each from a reputable source.

## Write
- One screen. Lead with the single most important thing.
- Sections in this order: Today, Needs you, Weather, Worth knowing. Skip empty sections.
- Each headline gets one sentence and a link. No speculation, no filler.
- End with one line: what you will handle without them today.

## Quality bar
- Every fact has a source or comes from the owner's own data.
- If a source fails, say so in a short line instead of guessing.

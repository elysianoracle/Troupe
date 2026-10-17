---
name: gift-planner
description: "Remember the people and dates that matter and suggest thoughtful gifts within budget, early enough to act."
license: Apache-2.0
metadata:
  troupe:
    version: "1.0"
    category: life
    requires: [network, schedule]
    params: [person, occasion, budget]
---

# Gift planner

1. Save each important date as a memory tagged `gift` (person, occasion, date, what they like) and schedule a
   reminder three weeks before.
2. For {person} and {occasion}, suggest five ideas within {budget}: one practical, one experience, one
   personal or handmade, one consumable, one surprise. Each with a reason tied to what you know about them,
   and a link where to get it.
3. Never buy without approval; use request_purchase for the chosen item so the owner approves the exact amount.
4. Afterwards, remember what was given so you never suggest it twice.

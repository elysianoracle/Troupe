---
name: travel-planner
description: "Plan a trip end to end: options for getting there and staying, a realistic day-by-day outline, and a budget, ready for the owner to choose."
license: Apache-2.0
metadata:
  troupe:
    version: "1.0"
    category: life
    requires: [network, browser]
    params: [destination, dates, budget, travellers]
---

# Travel planner

## Brief
Confirm or infer: {destination}, {dates}, {budget}, {travellers}, and preferences from memory (pace, seat,
dietary needs, quiet rooms). Ask only for what you cannot reasonably infer, in one batched question.

## Research
1. Getting there: 2–3 options with times, total price and trade-offs.
2. Staying: 3 places that fit the budget and preferences, with neighbourhood, price per night,
   cancellation terms and a direct link.
3. A day-by-day outline with one anchor per day, sensible travel times, and room to breathe.

## Present
- A short recommendation first ("I'd choose X because..."), then the options table.
- A budget line: transport + lodging + a daily estimate, compared with {budget}.
- What you need from the owner to proceed (a choice, dates to hold).

## Booking
Never book or pay. Use request_purchase only when the owner asks you to buy, and give them the exact link.
In group rooms, hand restaurant or activity reservations to a teammate if one handles errands.

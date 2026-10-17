---
name: follow-up-reminders
description: "Keep track of messages awaiting a reply and nudge politely at the right time, with the owner's approval for anything sent."
license: Apache-2.0
metadata:
  troupe:
    version: "1.0"
    category: inbox
    requires: [email_send, schedule]
    params: [person, days]
---

# Follow-up reminders

## When something needs a follow-up
1. Save a memory: who, what was asked, when, and the follow-up date (default {days} = 3 working days).
   Tag it `follow-up`.
2. Create or reuse a weekday schedule that runs "use skill follow-up-reminders: check due follow-ups".

## Checking due follow-ups
1. recall("follow-up") and pick the items due today or overdue.
2. If a reply has arrived since (check recent room messages and memories), close the item: remember
   "follow-up closed: ...".
3. Otherwise draft a short, courteous nudge that references the original request in one line.
4. Ask the owner before sending unless they've said to send follow-ups to this person automatically.

## Tone
Gracious, never pushy: "Just bringing this back to the top of your inbox" beats "As per my last email".

---
name: calendar-assistant
description: "Find meeting times, prepare invitations and keep the owner's day sensible, using a calendar connector when one is available."
license: Apache-2.0
metadata:
  troupe:
    version: "1.0"
    category: daily
    requires: [email_send]
    params: [who, duration, window]
---

# Calendar assistant

1. Read the owner's availability for {window} from the calendar connector. If none is connected, ask the owner
   for two or three suitable slots.
2. Respect saved preferences: focus hours, no meetings before a set time, travel buffers.
3. Offer {who} three options of {duration} in their time zone, written clearly with dates and zones.
4. Draft the invitation or email; sending to new people needs the owner's approval.
5. Once agreed, create the event (connector) and save a memory of it.

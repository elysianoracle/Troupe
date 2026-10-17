---
name: subscription-audit
description: "Find recurring charges in a bank or card export the owner provides, and list each subscription with its cost and how to cancel it."
license: Apache-2.0
metadata:
  troupe:
    version: "1.0"
    category: money
    requires: [shell, files, network]
    params: [path]
---

# Subscription audit

1. Load the export at {path} (CSV from the owner's bank). Use pandas in a short script.
2. Find recurring charges: the same merchant at similar amounts on a monthly or yearly rhythm.
3. For each subscription: merchant, amount, frequency, yearly cost, last charge date, and the official page
   explaining how to cancel.
4. Total the yearly cost and highlight ones that look unused, duplicated, or recently increased.
5. Do not cancel anything. Offer to draft cancellation emails for the owner to approve.
This is an organising aid, not financial advice.

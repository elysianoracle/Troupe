---
name: price-watch
description: "Watch a product page and tell the owner when the price drops below a target, using a small script that runs in the agent's sandbox."
license: Apache-2.0
metadata:
  troupe:
    version: "1.0"
    category: shopping
    requires: [network, shell, schedule]
    params: [url, target_price]
---

# Price watch

## Set up
1. Run the checker once to confirm it can read the price:
   `python3 /workspace/.skills/price-watch/scripts/check_price.py "{url}" {target_price}`
2. If it reports `"price": null`, open the page in the browser, find the price, and tell the owner the
   page needs a manual check; do not schedule a broken watch.
3. Save a memory tagged `price-watch` with the url, target and today's price.
4. Schedule a check (default: daily at 09:00 UTC) with the prompt
   "use skill price-watch: check {url} against {target_price}".

## Each check
Run the script. If `below_target` is true, tell the owner the price, the drop, and the link.
Otherwise stay quiet unless the owner asked for daily updates. After 30 days without a drop, ask
whether to keep watching.

## Rules
Never buy. If the owner wants it, use request_purchase so they approve the exact amount.

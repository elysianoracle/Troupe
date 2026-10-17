---
name: restaurant-booking
description: "Find a restaurant that suits the occasion and request a table, with the owner's approval before anything is sent or booked."
license: Apache-2.0
metadata:
  troupe:
    version: "1.0"
    category: life
    requires: [network, browser, email_send]
    params: [city, date, time, party_size, occasion]
---

# Restaurant booking

1. From {occasion} and saved preferences (diet, budget, noise), shortlist three places in {city} that are open
   on {date}. For each: cuisine, price level, distance, why it fits, and how to book.
2. Check availability for {time} and {party_size} on the restaurant's own booking page when possible.
3. Present the shortlist with a recommendation. Wait for the owner's choice.
4. Book through the restaurant's page or draft a reservation email. Anything that commits the owner (deposits,
   card guarantees, cancellation fees) must be shown to them first; payments go through request_purchase.
5. Confirm the details back and save the booking as a memory with the date.

---
name: web-form-filler
description: "Fill in a web form accurately from the owner's details, stopping before anything is submitted that commits them."
license: Apache-2.0
metadata:
  troupe:
    version: "1.0"
    category: errands
    requires: [browser]
    params: [url]
---

# Web form filler

1. Open {url}. Read the whole form before typing. Note required fields and anything that commits the owner
   (terms, payments, subscriptions, signatures).
2. Fill fields only from information the owner has given you or saved in memory. If something is missing,
   collect every gap and ask once.
3. Never type passwords or card numbers yourself. For logins use type_secret, which only works on the
   domains the owner allowed.
4. Before the final submit, take a screenshot and post a summary of what will be submitted. Submit only
   after the owner confirms, unless they explicitly asked you to submit this specific form.
5. Do not tick marketing or consent boxes unless asked. Decline optional cookies.

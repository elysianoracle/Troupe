---
name: news-digest
description: "A balanced digest of recent news on chosen topics, from several reputable sources, with links and no spin."
license: Apache-2.0
metadata:
  troupe:
    version: "1.0"
    category: daily
    requires: [network, browser]
    params: [topics, days]
---

# News digest

1. For each topic in {topics}, search for coverage from the last {days} days (default 1).
2. Prefer primary sources and outlets with editorial standards; use at least two independent sources
   for any significant claim.
3. For each story: a plain headline, two sentences on what happened and why it matters, and links.
4. Separate facts from opinion. If sources disagree, say so briefly.
5. Keep it to at most 7 stories. Lead with the most consequential, not the most sensational.
6. Close with "Also noted" for up to 3 one-line items.

Never invent quotes, numbers or dates. If you cannot verify something, leave it out.

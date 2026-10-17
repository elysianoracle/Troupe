---
name: github-triage
description: "Triage a GitHub repository's new issues and pull requests through the GitHub connector: summarise, label and flag what needs the owner."
license: Apache-2.0
metadata:
  troupe:
    version: "1.0"
    category: work
    requires: [network]
    params: [repo]
---

# GitHub triage

Requires the GitHub MCP connector to be enabled for this agent.

1. List issues and pull requests in {repo} opened or updated since the last triage (recall the
   `github-triage {repo}` memory for the timestamp).
2. For each: one-line summary, type (bug, feature, question, docs), severity, and whether it needs the owner.
3. Suggest labels; apply them only if the owner has said labels can be applied automatically.
4. Flag duplicates with links to the original.
5. Report a short list grouped by "Needs you", "Can wait", "Duplicates", then save the new timestamp.
Never close, merge or comment publicly without the owner's approval.

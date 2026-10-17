---
name: research-report
description: "Research a question properly and write a sourced report: findings, evidence, open questions, and a clear answer where one exists."
license: Apache-2.0
metadata:
  troupe:
    version: "1.0"
    category: work
    requires: [network, browser, files]
    params: [question, depth]
---

# Research report

## Method
1. Restate {question} precisely; note what would change the answer.
2. Gather from at least five independent, credible sources (more for {depth} = deep). Prefer primary
   data, papers, official documents. Record each source's title, publisher, date and URL.
3. Cross-check every important claim with a second source. Note disagreements.
4. Separate what is known, what is likely, and what is unknown.

## Write `/workspace/reports/<slug>.md`
- **Answer** (3–5 sentences, with confidence: high / medium / low and why).
- **Key findings**, each with inline citations [1], [2].
- **Evidence table**: claim, source, strength.
- **Open questions** and how to resolve them.
- **Sources**, numbered, with links.

## Rules
Never fabricate citations or numbers. Quote sparingly and attribute. If the evidence is thin, say so plainly.
Post the Answer section and the report's path to the room.

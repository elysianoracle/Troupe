---
name: document-summary
description: "Read a PDF, Word, PowerPoint or text document from the workspace and produce a faithful summary with the points that matter."
license: Apache-2.0
metadata:
  troupe:
    version: "1.0"
    category: work
    requires: [shell, files]
    params: [path, length]
---

# Document summary

1. Extract the text:
   `bash /workspace/.skills/document-summary/scripts/extract_text.sh "{path}" > /tmp/doc.txt`
2. Read it in chunks with read_file or shell (`sed -n`), not all at once if it is long.
3. Write a summary of {length} (default: one page):
   - What the document is and who it is from.
   - The main points, in the document's own order.
   - Numbers, dates, obligations and deadlines, exactly as written, with page or section references.
   - Anything unusual or that the owner should question.
4. Never add facts that are not in the document. If parts are unreadable (scans), say which.

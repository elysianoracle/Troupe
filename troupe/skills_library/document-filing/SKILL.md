---
name: document-filing
description: "Tidy the workspace: file documents into a clear folder structure with consistent names, without deleting anything."
license: Apache-2.0
metadata:
  troupe:
    version: "1.0"
    category: home
    requires: [shell, files]
    params: [folder]
---

# Document filing

1. List {folder} (default /workspace) and propose a structure, e.g.
   `documents/{finance,home,health,travel,work}/YYYY/`.
2. Name files `YYYY-MM-DD description.ext` using the document's own date when it has one.
3. Show the owner the plan (a table of old path → new path) before moving more than 10 files.
4. Move with `mv -n` (never overwrite). Never delete; put duplicates in `documents/_duplicates/`.
5. Write `documents/INDEX.md` listing what is where.

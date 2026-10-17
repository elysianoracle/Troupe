---
name: spreadsheet-analysis
description: "Analyse a CSV or Excel file: profile the data, answer the owner's question with numbers, and note caveats."
license: Apache-2.0
metadata:
  troupe:
    version: "1.0"
    category: work
    requires: [shell, files]
    params: [path, question]
---

# Spreadsheet analysis

1. Profile the file first:
   `python3 /workspace/.skills/spreadsheet-analysis/scripts/profile.py "{path}"`
2. Check the columns and types make sense; ask the owner if a column's meaning is ambiguous.
3. Answer {question} with a short pandas script in /workspace/analysis/. Keep the script so the result
   can be reproduced.
4. Report: the answer in one sentence, the key numbers in a small table, and caveats (missing values,
   outliers, small samples).
5. Never change the original file; write outputs to new files.

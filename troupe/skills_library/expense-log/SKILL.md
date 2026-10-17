---
name: expense-log
description: "Keep a tidy expense ledger in the workspace from receipts and forwarded emails, with monthly summaries by category."
license: Apache-2.0
metadata:
  troupe:
    version: "1.0"
    category: money
    requires: [shell, files]
    params: [month]
---

# Expense log

The ledger lives at /workspace/finance/expenses.csv (date, merchant, amount, currency, category, note).

## Add an expense
From a receipt email or the owner's message, extract date, merchant, amount, currency and a category
(Food, Travel, Home, Health, Subscriptions, Shopping, Work, Other), then run:
`python3 /workspace/.skills/expense-log/scripts/expense.py add 2026-05-16 "Café Nata" 12.40 EUR Food "lunch"`

## Summaries
`python3 /workspace/.skills/expense-log/scripts/expense.py summary {month}` (format YYYY-MM).
Present totals by category and the three largest items. Point out anything that looks duplicated.

## Rules
Record amounts exactly as shown. Never guess a missing amount; ask. This is a record, not financial advice.

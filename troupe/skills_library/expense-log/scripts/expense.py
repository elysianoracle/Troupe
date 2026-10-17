#!/usr/bin/env python3
"""Tiny expense ledger. Usage: expense.py add DATE MERCHANT AMOUNT CURRENCY CATEGORY [NOTE] | summary [YYYY-MM] | list"""
import csv
import sys
from collections import defaultdict
from pathlib import Path

LEDGER = Path("/workspace/finance/expenses.csv")
FIELDS = ["date", "merchant", "amount", "currency", "category", "note"]


def rows():
    if not LEDGER.exists():
        return []
    with LEDGER.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "add":
        date, merchant, amount, currency, category = sys.argv[2:7]
        note = sys.argv[7] if len(sys.argv) > 7 else ""
        float(amount)
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        new = not LEDGER.exists()
        with LEDGER.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerow(dict(zip(FIELDS, [date, merchant, amount, currency.upper(), category, note])))
        print(f"added {merchant} {amount} {currency.upper()}")
    elif cmd == "summary":
        month = sys.argv[2] if len(sys.argv) > 2 else ""
        totals = defaultdict(float)
        items = [r for r in rows() if r["date"].startswith(month)]
        for r in items:
            totals[(r["currency"], r["category"])] += float(r["amount"])
        for (cur, cat), total in sorted(totals.items()):
            print(f"{cat:14s} {total:10.2f} {cur}")
        for r in sorted(items, key=lambda r: -float(r["amount"]))[:3]:
            print(f"largest: {r['date']} {r['merchant']} {r['amount']} {r['currency']}")
    else:
        for r in rows()[-50:]:
            print(",".join(r[k] for k in FIELDS))


if __name__ == "__main__":
    main()

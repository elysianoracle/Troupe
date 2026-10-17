#!/usr/bin/env python3
"""Quick profile of a CSV/XLSX file. Usage: profile.py FILE"""
import sys
import pandas as pd

path = sys.argv[1]
df = pd.read_excel(path) if path.lower().endswith((".xlsx", ".xls")) else pd.read_csv(path)
print(f"rows: {len(df)}  columns: {len(df.columns)}\n")
for col in df.columns:
    s = df[col]
    line = f"- {col} ({s.dtype}): {s.notna().sum()} values, {s.isna().sum()} missing, {s.nunique()} distinct"
    if pd.api.types.is_numeric_dtype(s):
        line += f"; min {s.min()}, median {s.median()}, max {s.max()}"
    else:
        top = s.value_counts().head(3).to_dict()
        line += f"; top {top}"
    print(line)

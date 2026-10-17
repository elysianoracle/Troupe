#!/usr/bin/env bash
# Print the text of a document. Usage: extract_text.sh FILE
set -euo pipefail
f="$1"
case "${f,,}" in
  *.pdf)  pdftotext -layout "$f" - ;;
  *.docx|*.odt|*.rtf|*.epub|*.html|*.htm) pandoc -t plain --wrap=none "$f" ;;
  *.pptx) python3 - "$f" <<'PY'
import sys, zipfile, re
z = zipfile.ZipFile(sys.argv[1])
slides = sorted(n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml", n))
for i, n in enumerate(sorted(slides, key=lambda s: int(re.findall(r"\d+", s)[-1])), 1):
    text = " ".join(re.findall(r"<a:t>([^<]*)</a:t>", z.read(n).decode("utf-8", "replace")))
    print(f"--- slide {i} ---\n{text}")
PY
  ;;
  *) cat "$f" ;;
esac

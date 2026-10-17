#!/usr/bin/env python3
"""Fetch a product page and extract its price. Usage: check_price.py URL [TARGET]. Prints JSON."""
import json
import re
import sys
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) TroupeAgent/1.0 (automated price check for its owner)"


def extract(html):
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                offers = node.get("offers")
                if isinstance(offers, dict) and offers.get("price"):
                    return float(str(offers["price"]).replace(",", "")), offers.get("priceCurrency", "")
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            elif isinstance(node, list):
                stack.extend(node)
    m = re.search(r'itemprop=["\']price["\'][^>]*content=["\']([\d.,]+)', html)
    if m:
        return float(m.group(1).replace(",", "")), ""
    m = re.search(r'(?:[$€£¥]|USD|EUR|GBP)\s?(\d{1,6}(?:[.,]\d{2})?)', html)
    if m:
        return float(m.group(1).replace(",", ".")), ""
    return None, ""


def main():
    url = sys.argv[1]
    target = float(sys.argv[2]) if len(sys.argv) > 2 else None
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read(3_000_000).decode(r.headers.get_content_charset() or "utf-8", "replace")
    price, currency = extract(html)
    print(json.dumps({"url": url, "price": price, "currency": currency, "target": target,
                      "below_target": bool(price is not None and target is not None and price <= target)}))


if __name__ == "__main__":
    main()

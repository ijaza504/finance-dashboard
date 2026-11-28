#!/usr/bin/env python3
"""
Fetches the latest PSX share prices from https://dps.psx.com.pk/company/<SYMBOL>
for symbols listed in tickers.txt and writes them to a JSON file for the
dashboard to read.

  python3 fetch_prices.py OUTPUT.json [tickers.txt]

Safety rules:
- Prices are only ever copied from PSX — nothing is estimated, simulated or filled in.
- If no prices can be fetched, the script exits with an error and writes NOTHING,
  so any previous prices stay in place and the dashboard shows how old they are.
"""
import datetime
import json
import re
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser

BASE = "https://dps.psx.com.pk"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")


def http_get(url, retries=3, timeout=25):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("could not fetch %s (%s)" % (url, last))


def parse_number(text):
    if text is None:
        return None
    s = str(text).strip().replace(",", "").replace("%", "")
    s = re.sub(r"[^\d.\-]", "", s)
    if s in ("", "-", ".", "-."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


class CompanyQuoteParser(HTMLParser):
    """Extracts quote__close, quote__name, and quote__sector from /company/<SYMBOL>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._stack = []
        self._capture_cls = None
        self._buf = []
        self.close_text = None
        self.name_text = None
        self.sector_text = None

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class", "") or ""
        self._stack.append((tag, cls))
        for target in ("quote__close", "quote__name", "quote__sector"):
            if re.search(r"\b" + re.escape(target) + r"\b", cls) and self._capture_cls is None:
                self._capture_cls = target
                self._buf = []

    def handle_data(self, data):
        if self._capture_cls is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if self._stack:
            _, top_cls = self._stack.pop()
            if self._capture_cls and re.search(r"\b" + re.escape(self._capture_cls) + r"\b", top_cls):
                val = re.sub(r"\s+", " ", "".join(self._buf)).strip()
                if self._capture_cls == "quote__close" and not self.close_text:
                    self.close_text = val
                elif self._capture_cls == "quote__name" and not self.name_text:
                    self.name_text = val
                elif self._capture_cls == "quote__sector" and not self.sector_text:
                    self.sector_text = val
                self._capture_cls = None
                self._buf = []


def fetch_company_quote(symbol):
    url = "%s/company/%s" % (BASE, symbol)
    try:
        html = http_get(url, retries=1, timeout=20)
    except Exception as e:
        print("  [%s] http_get failed: %s" % (symbol, e))
        return None
    parser = CompanyQuoteParser()
    parser.feed(html)
    price = parse_number(parser.close_text)
    if price is None or price <= 0:
        title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else "no-title"
        q_idx = html.find("quote__close")
        snippet = re.sub(r"\s+", " ", html[max(0, q_idx - 60):q_idx + 140]).strip() if q_idx != -1 else "quote__close-not-in-raw-html"
        print("  [%s] close_text=%r | title=%r | len=%d | raw=%s" % (symbol, parser.close_text, title, len(html), snippet))
        return None
    entry = {
        "price": round(price, 4),
        "basis": "close",
    }
    if parser.name_text:
        entry["name"] = parser.name_text
    if parser.sector_text:
        entry["sector"] = parser.sector_text
    return entry

def read_tickers(path):
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        s = line.split("#")[0].strip().upper()
        if s and SYMBOL_RE.match(s):
            out.append(s)
    return out


def main(argv):
    if len(argv) < 2:
        print("usage: fetch_prices.py OUTPUT.json [tickers.txt]")
        return 2
    out_path = argv[1]
    ticker_path = argv[2] if len(argv) > 2 else "tickers.txt"
    tickers = read_tickers(ticker_path)

    if not tickers:
        print("ERROR: no valid tickers found in %s" % ticker_path)
        return 1

    prices = {}
    for sym in tickers:
        try:
            got = fetch_company_quote(sym)
        except Exception as e:
            print("%s: fetch error (%s)" % (sym, e))
            got = None
        print("%s: %s" % (sym, got["price"] if got else "no price found"))
        if got:
            prices[sym] = got
        time.sleep(0.35)

    if not prices:
        print("ERROR: could not fetch prices for any symbol in tickers.txt")
        return 1

    result = {
        "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "PSX Data Portal (dps.psx.com.pk) - delayed about 5 minutes",
        "count": len(prices),
        "prices": prices,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, separators=(",", ":"), sort_keys=True)
    print("wrote %s (%d prices)" % (out_path, len(prices)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

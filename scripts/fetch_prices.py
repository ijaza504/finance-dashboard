#!/usr/bin/env python3
"""
Fetches the latest PSX share prices from the public PSX Data Portal (dps.psx.com.pk,
which PSX itself labels "data delayed 5 minutes") and writes them to a JSON file
for the dashboard to read.

  python3 fetch_prices.py OUTPUT.json [tickers.txt]

Safety rules:
  * Prices are only ever copied from PSX — nothing is estimated, simulated or filled in.
  * If PSX can't be read, or the page no longer looks like it used to, the script exits
    with an error and writes NOTHING, so the last good prices stay in place and the
    dashboard shows how old they are.
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
    "User-Agent": "Mozilla/5.0 (personal portfolio dashboard; one low-frequency request)",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en",
}
MIN_SYMBOLS = 50            # a real market-watch page has hundreds; fewer means the page changed
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


class TableParser(HTMLParser):
    """Collects every <table> as header names + rows (cell text, links, cell attributes)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._table = None
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self._table = {"headers": [], "rows": []}
        elif tag == "tr" and self._table is not None:
            self._row = {"cells": [], "links": [], "attrs": [], "is_header": False}
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"text": [], "attrs": a}
            if tag == "th":
                self._row["is_header"] = True
        elif tag == "a" and self._row is not None and a.get("href"):
            self._row["links"].append(a["href"])

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"].append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row["cells"].append(re.sub(r"\s+", " ", "".join(self._cell["text"])).strip())
            self._row["attrs"].append(self._cell["attrs"])
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if self._row["is_header"] and not self._table["headers"]:
                self._table["headers"] = [c.upper() for c in self._row["cells"]]
            elif self._row["cells"]:
                self._table["rows"].append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None


def find_col(headers, *names):
    for want in names:
        for i, h in enumerate(headers):
            if h == want or h.startswith(want + " ") or h.startswith(want + "("):
                return i
    return None


def symbol_from_row(row, sym_idx):
    for href in row["links"]:                        # most reliable: the /company/SYMBOL link
        m = re.search(r"/company/([^/?#]+)", href)
        if m and SYMBOL_RE.match(m.group(1).upper()):
            return m.group(1).upper()
    if sym_idx is not None and sym_idx < len(row["attrs"]):
        for key in ("data-order", "data-search", "data-title"):
            v = (row["attrs"][sym_idx].get(key) or "").strip().upper().split(" ")[0]
            if SYMBOL_RE.match(v):
                return v
    if sym_idx is not None and sym_idx < len(row["cells"]):
        v = row["cells"][sym_idx].strip().upper().split(" ")[0]
        if SYMBOL_RE.match(v):
            return v
    return None


def parse_market_watch(html):
    """Returns {SYMBOL: {price, ldcp, name?, sector?, basis}} from the market-watch tables."""
    parser = TableParser()
    parser.feed(html)
    out = {}
    for tbl in parser.tables:
        h = tbl["headers"]
        sym_i = find_col(h, "SYMBOL", "SCRIP")
        cur_i = find_col(h, "CURRENT", "LAST", "PRICE")
        if sym_i is None or cur_i is None:
            continue
        ldcp_i = find_col(h, "LDCP")
        name_i = find_col(h, "NAME")
        sec_i = find_col(h, "SECTOR")
        for row in tbl["rows"]:
            cells = row["cells"]
            if len(cells) <= max(sym_i, cur_i):
                continue
            sym = symbol_from_row(row, sym_i)
            if not sym:
                continue
            cur = parse_number(cells[cur_i])
            ldcp = parse_number(cells[ldcp_i]) if ldcp_i is not None and ldcp_i < len(cells) else None
            if cur and cur > 0:
                price, basis = cur, "current"
            elif ldcp and ldcp > 0:
                price, basis = ldcp, "ldcp"        # not traded yet today: PSX's own last-day close
            else:
                continue
            entry = {"price": round(price, 4), "basis": basis}
            if ldcp and ldcp > 0:
                entry["ldcp"] = round(ldcp, 4)
            if name_i is not None and name_i < len(cells) and cells[name_i]:
                entry["name"] = cells[name_i]
            if sec_i is not None and sec_i < len(cells) and cells[sec_i]:
                entry["sector"] = cells[sec_i]
            out.setdefault(sym, entry)
    return out


def parse_timeseries(text):
    """Latest (timestamp, price) from PSX's timeseries JSON; tolerant of layout and ordering."""
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    rows = obj.get("data") if isinstance(obj, dict) else obj
    best = None
    for r in rows or []:
        if not isinstance(r, (list, tuple)) or len(r) < 2:
            continue
        ts, px = parse_number(r[0]), parse_number(r[1])
        if ts is None or not px or px <= 0:
            continue
        if ts > 1e12:
            ts /= 1000.0
        if best is None or ts > best[0]:
            best = (ts, px)
    return best


def fetch_single(symbol):
    for kind in ("int", "eod"):
        try:
            got = parse_timeseries(http_get("%s/timeseries/%s/%s" % (BASE, kind, symbol), retries=2))
        except RuntimeError:
            got = None
        if got:
            asof = datetime.datetime.fromtimestamp(got[0], datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return {"price": round(got[1], 4), "basis": "intraday" if kind == "int" else "eod", "asOf": asof}
        time.sleep(0.4)
    return None


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
    html = http_get(BASE + "/market-watch")
    prices = parse_market_watch(html)
    print("market-watch: read %d symbols" % len(prices))
    if len(prices) < MIN_SYMBOLS:
        print("ERROR: expected at least %d symbols but found %d — PSX's page layout has probably "
              "changed. Nothing was written, so the previous prices stay in place." % (MIN_SYMBOLS, len(prices)))
        return 1
    extra = [t for t in read_tickers(argv[2] if len(argv) > 2 else None) if t not in prices]
    for sym in extra:
        got = fetch_single(sym)
        print("extra %s: %s" % (sym, got["price"] if got else "no price found"))
        if got:
            prices[sym] = got
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

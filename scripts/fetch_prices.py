#!/usr/bin/env python3
"""
Fetches the latest PSX share prices from the public PSX Data Portal
(dps.psx.com.pk timeseries endpoints) for symbols listed in tickers.txt
and writes them to a JSON file for the dashboard to read.

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

BASE = "https://dps.psx.com.pk"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (personal portfolio dashboard; one low-frequency request)",
    "Accept": "application/json,text/plain,*/*;q=0.8",
    "Accept-Language": "en",
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
        url = "%s/timeseries/%s/%s" % (BASE, kind, symbol)
        try:
            raw = http_get(url, retries=1, timeout=15)
        except Exception as e:
            print("  [%s %s] fetch error: %s" % (symbol, kind, e))
            continue
        got = parse_timeseries(raw)
        if got:
            asof = datetime.datetime.fromtimestamp(got[0], datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return {"price": round(got[1], 4), "basis": "intraday" if kind == "int" else "eod", "asOf": asof}
        print("  [%s %s] parsed None; sample: %s" % (symbol, kind, raw[:180].replace("\n", " ")))
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
    ticker_path = argv[2] if len(argv) > 2 else "tickers.txt"
    tickers = read_tickers(ticker_path)

    if not tickers:
        print("ERROR: no valid tickers found in %s" % ticker_path)
        return 1

    prices = {}
    for sym in tickers:
        got = fetch_single(sym)
        print("%s: %s" % (sym, got["price"] if got else "no price found"))
        if got:
            prices[sym] = got

    if not prices:
        print("ERROR: could not fetch prices for any symbol in %s" % ticker_path)
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

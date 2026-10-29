"""Social-arbitrage attention tracker.

Daily: pulls Wikipedia pageviews for each watchlist brand, flags attention spikes,
and checks how much the stock has already moved (has Wall Street caught on?).
Also reads the newest scout file Grok saved from the Grok app (grok/scout/*.md),
logs every spike and Grok pick to data/signals.csv and scores them against SPY.
No Claude usage at runtime.
"""
import csv
import datetime as dt
import os
import pathlib
import re

import requests
import yaml

ROOT = pathlib.Path(__file__).parent
UA = {"User-Agent": "social-arb-tracker/1.0 (personal research; github.com/qanicuq)"}

SPIKE_RATIO = float(os.getenv("SPIKE_RATIO", "1.8"))   # last-7-day avg vs prior 8-week avg
MIN_DAILY_VIEWS = int(os.getenv("MIN_DAILY_VIEWS", "200"))  # ignore tiny pages
MOVED_PCT = 20  # stock already up this much in 30 days: Wall Street may have caught on


def pageviews(article, today):
    start = (today - dt.timedelta(days=70)).strftime("%Y%m%d")
    end = (today - dt.timedelta(days=1)).strftime("%Y%m%d")
    url = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
           f"en.wikipedia/all-access/user/{requests.utils.quote(article, safe='')}/daily/{start}/{end}")
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return [item["views"] for item in r.json()["items"]]


def spike_stats(views):
    recent, base = views[-7:], views[-63:-7]
    recent_avg = sum(recent) / len(recent)
    base_avg = sum(base) / len(base) if base else 0
    ratio = recent_avg / base_avg if base_avg else 0
    return recent_avg, base_avg, ratio


def price_change(ticker, days=30):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=3mo&interval=1d"
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c]
    past = closes[-min(days // 7 * 5 + 1, len(closes))]  # ~trading days in `days`
    return (closes[-1] / past - 1) * 100


def moved_note(move):
    return f" - already moved (+{move:.0f}% in 30 days)" if move is not None and move >= MOVED_PCT else ""


_daily = {}


def daily_closes(ticker):
    """[(date, close)] for about two years, oldest first; cached per run."""
    if ticker not in _daily:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=2y&interval=1d"
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        _daily[ticker] = [(dt.datetime.fromtimestamp(t, dt.timezone.utc).date(), c)
                          for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]) if c]
    return _daily[ticker]


def forward_return(ticker, date, bars):
    """% change from the first close on/after `date` to the close `bars` trading days later, or None."""
    closes = daily_closes(ticker)
    i = next((k for k, (d, _) in enumerate(closes) if d >= date), None)
    if i is None or i + bars >= len(closes):
        return None
    return (closes[i + bars][1] / closes[i][1] - 1) * 100


def scoreboard(signals):
    """Chan: test the signals honestly. Every spike and Grok pick is logged, then compared with SPY."""
    lines = ["## Scoreboard: did the signals beat SPY?", "",
             "Every attention spike and every Grok pick is logged on the day it appears, then checked 20 and 60 "
             "trading days later against simply owning SPY. Expect real trading to do about half as well as this "
             "(costs, timing, and not acting on every signal).", "",
             "| Signal | Days later | Signals checked | Avg stock | Avg SPY | Beat SPY |", "|---|---|---|---|---|---|"]
    for source, label in (("spike", "Attention spike"), ("scout", "Grok pick")):
        for bars in (20, 60):
            stock, spy = [], []
            for s in signals:
                if s["source"] != source:
                    continue
                d = dt.date.fromisoformat(s["date"])
                try:
                    a, b = forward_return(s["ticker"], d, bars), forward_return("SPY", d, bars)
                except Exception:
                    continue
                if a is not None and b is not None:
                    stock.append(a)
                    spy.append(b)
            if stock:
                n = len(stock)
                beat = sum(x > y for x, y in zip(stock, spy))
                lines.append(f"| {label} | {bars} | {n} | {sum(stock) / n:+.1f}% | {sum(spy) / n:+.1f}% | {beat} of {n} |")
            else:
                lines.append(f"| {label} | {bars} | 0 | not enough time yet | | |")
    return lines + [""]


def first_seen():
    """ticker -> date it first appeared in data/history.csv (the day it was added)."""
    hist = ROOT / "data" / "history.csv"
    first = {}
    if hist.exists():
        for r in csv.DictReader(hist.open()):
            first.setdefault(r["ticker"], r["date"])
    return first


def added_note(date, today):
    days = (today - dt.date.fromisoformat(date)).days if date else 0
    return "today" if days == 0 else f"{days} day{'s' if days != 1 else ''} ago"


def latest_scout():
    """Newest grok/scout/*.md written by Grok in the Grok app (subscription, no API credits)."""
    files = sorted((ROOT / "grok" / "scout").glob("*.md"))
    if not files:
        return None
    text = files[-1].read_text()
    tickers = re.findall(r"Company and ticker:.*?\b(?:NASDAQ|NYSE)?[:\s(]*\$?([A-Z]{1,5})\)?\s*$", text, re.M)
    return files[-1], list(dict.fromkeys(tickers))


def main():
    today = dt.date.today()
    watch = yaml.safe_load((ROOT / "watchlist.yml").read_text())
    rows, flagged, errors = [], [], []

    for w in watch:
        try:
            recent, base, ratio = spike_stats(pageviews(w["wiki"], today))
        except Exception as e:
            errors.append(f"{w['ticker']}: pageviews failed ({e})")
            continue
        try:
            move = price_change(w["ticker"])
        except Exception:
            move = None
        row = {"date": today.isoformat(), "ticker": w["ticker"], "name": w["name"],
               "recent_avg": round(recent), "base_avg": round(base), "ratio": round(ratio, 2),
               "price_30d_pct": None if move is None else round(move, 1)}
        rows.append(row)
        if ratio >= SPIKE_RATIO and recent >= MIN_DAILY_VIEWS:
            flagged.append((w, row))

    lines = [f"# Social arb report - {today}", ""]
    if flagged:
        lines += [f"## Attention spikes ({len(flagged)})", ""]
        for w, row in sorted(flagged, key=lambda x: -x[1]["ratio"]):
            move = "n/a" if row["price_30d_pct"] is None else f"{row['price_30d_pct']:+.1f}%"
            lines.append(f"### {row['ticker']} - {row['name']}: {row['ratio']}x attention, stock {move} (30d)"
                         f"{moved_note(row['price_30d_pct'])}")
            lines.append("")
    else:
        lines += ["No attention spikes today.", ""]

    first = first_seen()
    lines += ["## All tracked", "", "| Ticker | Added | Views/day (7d) | Baseline | Ratio | Stock 30d |",
              "|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: -r["ratio"]):
        move = "n/a" if r["price_30d_pct"] is None else f"{r['price_30d_pct']:+.1f}%"
        if moved_note(r["price_30d_pct"]):
            move += " (already moved)"
        lines.append(f"| {r['ticker']} | {added_note(first.get(r['ticker']), today)} | {r['recent_avg']:,} | {r['base_avg']:,} | {r['ratio']}x | {move} |")
    lines.append("")

    scout = latest_scout()
    if scout:
        path, tickers = scout
        lines += [f"## Latest Grok scout ({path.stem})", "",
                  f"Grok's trend picks from X, Reddit and the web: grok/scout/{path.name}", ""]
        for tk in tickers:
            try:
                move = price_change(tk)
                lines.append(f"- {tk}: stock {move:+.1f}% over 30 days{moved_note(move)}")
            except Exception:
                lines.append(f"- {tk}: price not found")
        lines.append("")

    # log today's signals once each, then score all of them
    sig_file = ROOT / "data" / "signals.csv"
    sig_fields = ["date", "ticker", "source"]
    signals = list(csv.DictReader(sig_file.open())) if sig_file.exists() else []
    have = {(s["date"], s["ticker"], s["source"]) for s in signals}
    fresh = [{"date": today.isoformat(), "ticker": r["ticker"], "source": "spike"} for _, r in flagged]
    if scout:
        fresh += [{"date": scout[0].stem, "ticker": tk, "source": "scout"} for tk in scout[1]]
    fresh = [s for s in fresh if (s["date"], s["ticker"], s["source"]) not in have]
    signals += fresh
    sig_file.parent.mkdir(exist_ok=True)
    with sig_file.open("w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=sig_fields, extrasaction="ignore")
        wr.writeheader()
        wr.writerows(signals)
    lines += scoreboard(signals)

    if errors:
        lines += ["## Errors", ""] + [f"- {e}" for e in errors] + [""]

    report = "\n".join(lines)
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / f"{today}.md").write_text(report)
    (ROOT / "reports" / "latest.md").write_text(report)

    hist = ROOT / "data" / "history.csv"
    hist.parent.mkdir(exist_ok=True)
    fields = ["date", "ticker", "name", "recent_avg", "base_avg", "ratio", "price_30d_pct"]
    new = not hist.exists()
    with hist.open("a", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            wr.writeheader()
        wr.writerows(rows)

    topic = os.getenv("NTFY_TOPIC")
    # One alert per new scout file that has a qualifying trend (never on "No qualifying trends").
    seen = ROOT / "data" / "scout_alerted.txt"
    new_scout = None
    if scout and "no qualifying trends" not in scout[0].read_text().lower():
        done = seen.read_text().split() if seen.exists() else []
        if scout[0].name not in done:
            new_scout = scout
            seen.write_text("\n".join(done + [scout[0].name]) + "\n")
    if topic and new_scout:
        names = re.findall(r"Trend:\**\s*(.+)", new_scout[0].read_text())
        body = "\n".join(f"- {n.strip()[:90]}" for n in names[:5]) or "See the scout file."
        tick = ", ".join(new_scout[1]) or "see file"
        requests.post(f"https://ntfy.sh/{topic}", data=f"Tickers: {tick}\n{body}\nAsk Claude: review the Grok scout".encode(),
                      timeout=30, headers={"Title": "Social arb: qualifying trend found", "Tags": "mag",
                                           "Click": f"https://github.com/{os.getenv('GITHUB_REPOSITORY', '')}/blob/main/grok/scout/{new_scout[0].name}"})
    if topic and flagged:
        msg = "; ".join(f"{r['ticker']} {r['ratio']}x" for _, r in flagged)
        repo = os.getenv("GITHUB_REPOSITORY", "")
        requests.post(f"https://ntfy.sh/{topic}", data=msg.encode(), timeout=30,
                      headers={"Title": "Social arb tracker",
                               "Click": f"https://github.com/{repo}/blob/main/reports/latest.md"})

    print(report)


if __name__ == "__main__":
    main()

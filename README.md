# social-arb-tracker

Camillo–style social arbitrage scanner. Runs daily on GitHub Actions (no Claude usage).

- **Every day:** Wikipedia attention for each brand in `watchlist.yml` vs its 8-week baseline, plus the stock's 30-day move. Spikes ≥1.8x get flagged.
- **Grok scout:** Grok (in the Grok app, not the API) saves a daily trend scout to `grok/scout/`; the report prices its tickers.
- **Output:** `reports/latest.md`, history in `data/history.csv`, push via ntfy when something's flagged.

## Secrets (Settings → Secrets and variables → Actions)
- `NTFY_TOPIC` – optional, your ntfy topic.

Run manually: Actions → daily-scan → Run workflow.
Paper/research only — not trading advice.

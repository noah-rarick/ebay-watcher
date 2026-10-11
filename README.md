# eBay image-match watcher

Every ~5 minutes GitHub Actions searches eBay by image for each watch in `config.json` and sends a phone alert (ntfy) for new Buy It Now listings at or under `max_price` (shipping included).

- **Secrets** (Settings → Secrets and variables → Actions): `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `NTFY_TOPIC`.
- **Change what you watch:** edit `config.json` and add photos to `images/`.
- **Run by hand:** Actions tab → eBay watcher → Run workflow.
- **Start fresh:** delete `seen.json`; the next run saves a new baseline without alerting.

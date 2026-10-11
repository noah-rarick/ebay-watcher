# eBay image-match watcher

Every ~5 minutes GitHub Actions searches eBay **by photo** (like eBay's camera search) and sends a phone alert (via [ntfy](https://ntfy.sh)) when a *new* Buy It Now listing among the closest matches is at or under a max price (shipping included).

## 🔔 Want alerts for something?

**[Sign up here →](../../issues/new?template=watch.yml)** — upload a photo, set your max price, and paste your ntfy topic. A bot replies on your form to confirm. Close the form to stop.

Tips: crop the photo tight around the item; you'll only be alerted about listings posted after you sign up. Everything on the form is public.

## For the owner

- **Secrets** (Settings → Secrets and variables → Actions): `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `NTFY_TOPIC`.
- **Approving people:** forms from you start automatically. Add trusted GitHub usernames to `allowed_users` in `config.json`, or add the **approved** label to someone's form.
- **Your own watches** live in `config.json` → `watches` (photos in `images/`).
- **Tuning:** `max_rank` (only eBay's top N visual matches can alert), `listed_within_hours`.
- **eBay limit:** each watch is one search per run (~290/day at every 5 min). The default eBay limit is about 5,000/day, so with more than ~15 watches, slow the schedule in `.github/workflows/watch.yml`.
- **Run by hand:** Actions tab → eBay watcher → Run workflow. **Start fresh:** delete `seen.json`.

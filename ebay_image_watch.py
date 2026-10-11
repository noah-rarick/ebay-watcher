#!/usr/bin/env python3
"""
eBay image-match watcher.

Searches eBay by image (Browse API searchByImage) for each "watch" in
config.json, keeps Buy It Now listings at or under your max price, and
sends a phone notification (via ntfy.sh) and/or email for any listing it
hasn't alerted on before.

Standard library only - no pip installs needed. Python 3.8+.

Usage:
    python ebay_image_watch.py            # check once (for cron / Task Scheduler)
    python ebay_image_watch.py --loop     # keep running, checking every N minutes
    python ebay_image_watch.py --test     # send a test notification and exit
"""

import argparse
import base64
import json
import smtplib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
SEEN_PATH = HERE / "seen.json"

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search_by_image"
SCOPE = "https://api.ebay.com/oauth/api_scope"

_token_cache = {"token": None, "expires": 0}


# ---------- helpers ----------

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def http(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code} from {url}: {body[:500]}") from None


# ---------- eBay ----------

def get_token(cfg):
    """App token via client-credentials grant; cached until shortly before expiry."""
    if _token_cache["token"] and time.time() < _token_cache["expires"] - 60:
        return _token_cache["token"]
    creds = f"{cfg['ebay_client_id']}:{cfg['ebay_client_secret']}".encode()
    headers = {
        "Authorization": "Basic " + base64.b64encode(creds).decode(),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": SCOPE}).encode()
    resp = http(TOKEN_URL, data=body, headers=headers, method="POST")
    _token_cache["token"] = resp["access_token"]
    _token_cache["expires"] = time.time() + int(resp.get("expires_in", 7200))
    return _token_cache["token"]


def search_by_image(cfg, watch):
    global _date_filter_unsupported
    image_path = (HERE / watch["image"]).resolve()
    image_b64 = base64.b64encode(image_path.read_bytes()).decode()

    currency = cfg.get("currency", "USD")
    filters = [
        "buyingOptions:{FIXED_PRICE}",
        f"priceCurrency:{currency}",
    ]
    if watch.get("conditions"):
        filters.append("conditions:{" + "|".join(watch["conditions"]) + "}")
    if cfg.get("delivery_country"):
        filters.append(f"deliveryCountry:{cfg['delivery_country']}")
    # No listing-date filter here: it skews eBay's image ranking. Listing age is
    # checked on the results instead (see listed_within_hours).
    hours = 0

    params = {"limit": str(watch.get("limit", 50)), "filter": ",".join(filters)}
    if watch.get("category_id"):
        params["category_ids"] = str(watch["category_id"])
    if watch.get("keywords"):
        params["q"] = watch["keywords"]

    url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
    headers = {
        "Authorization": "Bearer " + get_token(cfg),
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": cfg.get("marketplace", "EBAY_US"),
    }
    try:
        resp = http(url, data=json.dumps({"image": image_b64}).encode(), headers=headers, method="POST")
    except RuntimeError as e:
        if hours and not _date_filter_unsupported and "HTTP 400" in str(e):
            log("eBay rejected the listing-date filter; falling back to checking dates on results.")
            _date_filter_unsupported = True
            return search_by_image(cfg, watch)
        raise
    return resp.get("itemSummaries", [])


_date_filter_unsupported = False


def recent_hours(cfg, watch):
    return float(watch.get("listed_within_hours", cfg.get("listed_within_hours", 0)) or 0)


def listing_age_hours(item):
    """Hours since the listing was created, or None if eBay didn't say."""
    created = item.get("itemCreationDate") or item.get("itemOriginDate")
    if not created:
        return None
    try:
        import calendar
        t = calendar.timegm(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S"))
        return (time.time() - t) / 3600
    except ValueError:
        return None


def total_cost(item):
    """Item price plus cheapest listed shipping (if any)."""
    price = float(item.get("price", {}).get("value", 0))
    ship_costs = [
        float(o["shippingCost"]["value"])
        for o in item.get("shippingOptions", [])
        if o.get("shippingCost", {}).get("value") is not None
    ]
    return price + (min(ship_costs) if ship_costs else 0.0), price, (min(ship_costs) if ship_costs else None)


def title_ok(item, watch):
    title = item.get("title", "").lower()
    for word in watch.get("exclude_words", []):
        if word.lower() in title:
            return False
    required = watch.get("require_words", [])
    if not all(w.lower() in title for w in required):
        return False
    # Each group: the title must contain at least one word from that group.
    for group in watch.get("require_any_groups", []):
        if not any(w.lower() in title for w in group):
            return False
    return True


# ---------- notifications ----------

def notify(cfg, subject, message, link=None, image=None):
    sent = False
    topic = cfg.get("ntfy_topic")
    if topic:
        # Plain publish (same as `curl -d "msg" ntfy.sh/topic`), with extras as headers.
        def hdr(value):
            try:
                value.encode("ascii")
                return value
            except UnicodeEncodeError:
                return "=?UTF-8?B?" + base64.b64encode(value.encode("utf-8")).decode() + "?="
        server = cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/")
        cmd = ["curl", "-sS", "--fail", "-m", "30", "-H", "Title: " + hdr(subject)]
        if link:
            cmd += ["-H", "Click: " + link]
        cmd += ["--data-binary", message, f"{server}/{urllib.parse.quote(topic)}"]
        out = subprocess.run(cmd, capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(f"ntfy send failed: {out.stderr.strip() or out.stdout.strip()}")
        log("ntfy response: " + out.stdout.strip()[:200])
        sent = True

    em = cfg.get("email")
    if em and em.get("to"):
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = em["from"]
        msg["To"] = em["to"]
        msg.set_content(message + (f"\n\n{link}" if link else ""))
        with smtplib.SMTP(em.get("smtp_host", "smtp.gmail.com"), int(em.get("smtp_port", 587))) as s:
            s.starttls()
            s.login(em["username"], em["password"])
            s.send_message(msg)
        sent = True

    if not sent:
        log("No notification method configured; would have sent: " + subject)


# ---------- main loop ----------

def check_once(cfg, seen):
    new_count = 0
    for watch in cfg["watches"]:
        name = watch.get("name", watch["image"])
        try:
            items = search_by_image(cfg, watch)
        except Exception as e:  # keep other watches running
            log(f"[{name}] search failed: {e}")
            continue

        seen_ids = set(seen.setdefault(name, []))
        first_run = name not in seen.get("_initialized", [])

        max_rank = int(watch.get("max_rank", cfg.get("max_rank", 0)) or 0)
        for rank, item in enumerate(items, 1):
            item_id = item.get("itemId")
            if not item_id or item_id in seen_ids:
                continue
            if max_rank and rank > max_rank and not first_run:
                continue  # weaker visual match; not marked seen in case it ranks higher later
            seen_ids.add(item_id)

            total, price, ship = total_cost(item)
            limit = float(watch["max_price"])
            if watch.get("include_shipping", True) and total > limit:
                continue
            if not title_ok(item, watch):
                continue
            hours = recent_hours(cfg, watch)
            age = listing_age_hours(item)
            if hours and age is not None and age > hours:
                continue  # old listing that just surfaced in the results
            if first_run and not cfg.get("alert_on_existing", False):
                continue  # baseline: don't spam alerts for listings that were already up

            ship_txt = "free ship" if ship == 0 else (f"+${ship:.2f} ship" if ship else "ship ?")
            subject = f"${price:.2f} ({ship_txt}) - {name}"
            body = f"{item.get('title', '')}\nCondition: {item.get('condition', '?')}\nTotal: ${total:.2f}"
            notify(cfg, subject, body, link=item.get("itemWebUrl"),
                   image=item.get("image", {}).get("imageUrl"))
            log(f"[{name}] ALERT {subject} {item.get('itemWebUrl')}")
            new_count += 1

        # cap memory per watch
        seen[name] = list(seen_ids)[-5000:]
        if first_run:
            seen.setdefault("_initialized", []).append(name)
            log(f"[{name}] baseline saved ({len(items)} current matches).")
    return new_count


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", action="store_true", help="keep running and check on an interval")
    ap.add_argument("--test", action="store_true", help="send a test notification and exit")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit("config.json not found. Copy config.example.json to config.json and fill it in.")

    # Secrets can come from environment variables (used by GitHub Actions).
    import os
    for key, env in (("ebay_client_id", "EBAY_CLIENT_ID"),
                     ("ebay_client_secret", "EBAY_CLIENT_SECRET"),
                     ("ntfy_topic", "NTFY_TOPIC")):
        if os.environ.get(env):
            cfg[key] = os.environ[env].strip()

    if args.test:
        notify(cfg, "eBay watcher test", "Notifications are working.", link="https://www.ebay.com")
        log("Test notification sent.")
        return

    seen = load_json(SEEN_PATH, {})
    interval = max(1, int(cfg.get("check_every_minutes", 5))) * 60

    while True:
        n = check_once(cfg, seen)
        save_json(SEEN_PATH, seen)
        log(f"Check done, {n} new alert(s).")
        if not args.loop:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()

"""
Watches submitted through the GitHub sign-up form (issues labelled "watch").

Each open issue becomes one watch. The form fields are read from the issue body,
the photo is downloaded to .issue_images/, and the watcher posts a comment on the
issue the first time it starts watching (or when something in the form is wrong).

Only issues from the repo owner, from people listed in config.json "allowed_users",
or issues a maintainer has labelled "approved" are watched.
"""
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
IMG_DIR = Path(__file__).resolve().parent / ".issue_images"

FIELD_ITEM = "What are you looking for?"
FIELD_PHOTO = "Photo of the item"
FIELD_PRICE = "Max price (USD, including shipping)"
FIELD_TOPIC = "Your ntfy topic"


def _gh(method, path, token, body=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "ebay-image-watch",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode() or "null")


def parse_form(body):
    """Issue-form bodies look like '### Label\\n\\nvalue\\n\\n### Label ...'."""
    fields = {}
    for block in re.split(r"^###\s+", body or "", flags=re.M)[1:]:
        label, _, value = block.partition("\n")
        value = value.strip()
        fields[label.strip()] = "" if value == "_No response_" else value
    return fields


def find_image_url(text):
    m = re.search(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", text or "") or \
        re.search(r"<img[^>]+src=\"(https?://[^\"]+)\"", text or "")
    return m.group(1) if m else None


def parse_price(text):
    m = re.search(r"\d+(?:\.\d+)?", (text or "").replace(",", ""))
    return float(m.group(0)) if m else None


def download(url, dest, token):
    """curl drops the Authorization header on cross-host redirects (to the file host)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    for headers in ([], ["-H", f"Authorization: Bearer {token}"]):
        r = subprocess.run(["curl", "-sSL", "--fail", "-m", "60", *headers, "-o", str(dest), url],
                           capture_output=True, text=True)
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 1000:
            return True
    return False


def load(cfg, seen, log):
    """Return watch dicts built from open, approved sign-up issues."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return []
    owner = repo.split("/")[0].lower()
    allowed = {u.lower() for u in cfg.get("allowed_users", [])} | {owner}

    try:
        issues = _gh("GET", f"/repos/{repo}/issues?labels=watch&state=open&per_page=100", token) or []
    except urllib.error.URLError as e:
        log(f"Could not read sign-up issues: {e}")
        return []

    open_keys = set()
    watches = []
    notes = seen.setdefault("_issue_notes", {})  # issue number -> last comment we posted

    def comment_once(num, kind, text):
        if notes.get(str(num)) == kind:
            return
        try:
            _gh("POST", f"/repos/{repo}/issues/{num}/comments", token, {"body": text})
            notes[str(num)] = kind
        except urllib.error.URLError as e:
            log(f"Could not comment on #{num}: {e}")

    for issue in issues:
        if "pull_request" in issue:
            continue
        num = issue["number"]
        author = (issue.get("user") or {}).get("login", "").lower()
        labels = {l["name"].lower() for l in issue.get("labels", [])}
        if author not in allowed and "approved" not in labels:
            comment_once(num, "pending",
                         "Thanks! This watch will start once the repo owner approves it "
                         "(they'll add an **approved** label).")
            continue

        f = parse_form(issue.get("body", ""))
        item = f.get(FIELD_ITEM) or issue.get("title", f"#{num}")
        price = parse_price(f.get(FIELD_PRICE))
        topic = (f.get(FIELD_TOPIC) or "").strip().strip("`").strip()
        img_url = find_image_url(f.get(FIELD_PHOTO)) or find_image_url(issue.get("body"))

        problems = []
        if not img_url:
            problems.append("add a **photo** of the item (drag it into the photo box)")
        if price is None or price <= 0:
            problems.append("enter a **max price** as a number, e.g. `180`")
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", topic):
            problems.append("enter an **ntfy topic** (6–64 letters, numbers, `-` or `_`)")
        if problems:
            comment_once(num, "invalid:" + "|".join(problems),
                         "I can't start this watch yet. Please edit the issue to:\n- " + "\n- ".join(problems))
            continue

        ext = Path(img_url.split("?")[0]).suffix.lower()
        dest = IMG_DIR / f"issue-{num}{ext if ext in ('.jpg', '.jpeg', '.png', '.webp') else '.jpg'}"
        url_marker = IMG_DIR / f"issue-{num}.url"
        if not (dest.exists() and url_marker.exists() and url_marker.read_text() == img_url):
            if not download(img_url, dest, token):
                log(f"[#{num}] could not download photo {img_url}")
                continue
            url_marker.write_text(img_url)

        key = f"issue-{num}"
        open_keys.add(key)
        watches.append({
            "name": f"#{num} {item}"[:60],
            "_key": key,
            "_topic": topic,
            "_issue": num,
            "image": str(dest),
            "max_price": price,
            "include_shipping": True,
        })
        comment_once(num, "watching",
                     f"✅ Watching for **{item}** at **${price:,.2f} or less** (including shipping), "
                     f"Buy It Now only.\n\nAlerts go to ntfy topic `{topic}` — install the ntfy app and "
                     f"subscribe to it. You'll only hear about listings posted after now.\n\n"
                     f"Edit this issue to change the photo or price. **Close it to stop watching.**")

    # Forget closed watches.
    for k in [k for k in seen if k.startswith("issue-") and k not in open_keys]:
        seen.pop(k, None)
    if "_initialized" in seen:
        seen["_initialized"] = [k for k in seen["_initialized"]
                                if not k.startswith("issue-") or k in open_keys]
    return watches

"""Michigan surplus deal scanner.

Pulls live listings from public Michigan surplus/auction pages, filters them to
Alex's categories and budget, and writes:
  data/listings.json   - every Michigan listing found this run
  data/shortlist.json  - filtered candidates for Claude to score
  data/shortlist.md    - same shortlist, human-readable
  data/seen.json       - first-seen dates, so "new" items can be flagged
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DEBUG = ROOT / "debug"
CONFIG = json.loads((Path(__file__).parent / "config.json").read_text())

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
NOW = datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(msg, flush=True)


def get(url: str) -> str | None:
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=30)
            if r.status_code == 200:
                return r.text
            log(f"  HTTP {r.status_code} for {url}")
        except requests.RequestException as e:
            log(f"  error {e} for {url}")
        time.sleep(3 * (attempt + 1))
    return None


def save_debug(name: str, html: str) -> None:
    DEBUG.mkdir(exist_ok=True)
    (DEBUG / name).write_text(html)


def money(text: str) -> float | None:
    m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text or "")
    return float(m.group(1).replace(",", "")) if m else None


def hours_left(text: str) -> float | None:
    """'2 days 18 hours', '17 hours 30 mins', '45 mins' -> hours."""
    t = (text or "").lower()
    if not re.search(r"\d", t):
        return None
    total = 0.0
    found = False
    for num, unit in re.findall(r"(\d+)\s*(day|hour|hr|min|sec)", t):
        found = True
        n = float(num)
        total += {"day": 24, "hour": 1, "hr": 1, "min": 1 / 60, "sec": 1 / 3600}[unit] * n
    return round(total, 2) if found else None


# ---------------------------------------------------------------- Public Surplus
def scrape_publicsurplus() -> list[dict]:
    out: dict[str, dict] = {}
    base = "https://www.publicsurplus.com/sms/all,mi/browse/search?posting=y&sortBy=timeLeft&page={}"
    for page in range(CONFIG["publicsurplus_max_pages"]):
        html = get(base.format(page))
        if not html:
            break
        if page == 0:
            save_debug("publicsurplus_p0.html", html)
        soup = BeautifulSoup(html, "html.parser")
        new = 0
        for tr in soup.find_all("tr"):
            link = tr.find("a", href=re.compile(r"auction/view\?auc=\d+"))
            if not link:
                continue
            auc = re.search(r"auc=(\d+)", link["href"]).group(1)
            if auc in out:
                continue
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            title = (link.get("title") or link.get_text(" ", strip=True)).strip()
            if not title or title.isdigit():
                # first link may be the id cell; find the titled one
                for a in tr.find_all("a", href=re.compile(r"auc=" + auc)):
                    txt = (a.get("title") or a.get_text(" ", strip=True)).strip()
                    if txt and not txt.isdigit() and txt.lower() != "picture":
                        title = txt
                        break
            price = next((money(c) for c in reversed(cells) if money(c) is not None), None)
            left_txt = next((c for c in cells if re.search(r"\d+\s*(day|hour|min)", c, re.I)), "")
            state = next((c for c in cells if re.fullmatch(r"[A-Z]{2}", c)), "MI")
            out[auc] = {
                "source": "Public Surplus",
                "id": f"ps-{auc}",
                "title": title,
                "price": price,
                "time_left": left_txt,
                "hours_left": hours_left(left_txt),
                "location": state,
                "url": f"https://www.publicsurplus.com/sms/auction/view?auc={auc}",
            }
            new += 1
        log(f"Public Surplus page {page}: {new} new listings")
        if new == 0:
            break
        time.sleep(1.5)
    return list(out.values())


# ---------------------------------------------------------------- Municibid
def scrape_municibid() -> list[dict]:
    out: dict[str, dict] = {}
    for page in range(CONFIG["municibid_max_pages"]):
        url = f"https://municibid.com/browse?region={CONFIG['municibid_region_id']}&page={page}"
        html = get(url)
        if not html:
            break
        if page == 0:
            save_debug("municibid_p0.html", html)
        soup = BeautifulSoup(html, "html.parser")
        new = 0
        for a in soup.find_all("a", href=re.compile(r"/listing/\d+/")):
            lid = re.search(r"/listing/(\d+)/", a["href"]).group(1)
            title = a.get_text(" ", strip=True)
            if lid in out or not title:
                continue
            # walk up to the card that holds price + location
            card = a
            for _ in range(6):
                if card.parent is None:
                    break
                card = card.parent
                if "Current Bid" in card.get_text() or "Bid(s)" in card.get_text():
                    break
            text = card.get_text(" ", strip=True)
            m_bid = re.search(r"Current Bid:?\s*(\$[\d,\.]+)", text)
            m_bids = re.search(r"Bid\(s\):?\s*(\d+)", text)
            m_loc = re.search(r"([A-Z][A-Za-z\.\s]+,\s*[A-Z]{2})\b", text)
            loc = m_loc.group(1).strip() if m_loc else ""
            if loc and not loc.endswith("MI"):
                continue
            href = a["href"]
            out[lid] = {
                "source": "Municibid",
                "id": f"mb-{lid}",
                "title": title,
                "price": money(m_bid.group(1)) if m_bid else None,
                "bids": int(m_bids.group(1)) if m_bids else None,
                "time_left": "",
                "hours_left": None,
                "location": loc or "MI",
                "url": href if href.startswith("http") else "https://municibid.com" + href,
            }
            new += 1
        log(f"Municibid page {page}: {new} new listings")
        if new == 0:
            break
        time.sleep(1.5)
    return list(out.values())


# ---------------------------------------------------------------- filtering
def categorize(title: str) -> str | None:
    t = f" {title.lower()} "
    if any(re.search(p, t) for p in CONFIG["exclude"]):
        return None
    for cat, pats in CONFIG["categories"].items():
        if any(re.search(p, t) for p in pats):
            return cat
    return None


def main() -> int:
    DATA.mkdir(exist_ok=True)
    listings: list[dict] = []
    for name, fn in (("Public Surplus", scrape_publicsurplus), ("Municibid", scrape_municibid)):
        try:
            items = fn()
            log(f"{name}: {len(items)} Michigan listings")
            listings += items
        except Exception as e:  # one broken site shouldn't kill the run
            log(f"{name} FAILED: {e!r}")

    seen_path = DATA / "seen.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    today = NOW.date().isoformat()

    shortlist = []
    for it in listings:
        it["first_seen"] = seen.setdefault(it["id"], today)
        it["new_this_run"] = it["first_seen"] == today
        cat = categorize(it["title"])
        it["category"] = cat
        if not cat:
            continue
        if it["price"] is not None and it["price"] > CONFIG["max_price"]:
            continue
        hl = it["hours_left"]
        if hl is not None and hl > CONFIG["max_hours_left"]:
            continue
        shortlist.append(it)

    # prune seen ids not seen in 30 days of runs
    live = {it["id"] for it in listings}
    seen = {k: v for k, v in seen.items() if k in live or (NOW.date() - datetime.fromisoformat(v).date()).days < 30}

    shortlist.sort(key=lambda x: (x["hours_left"] is None, x["hours_left"] or 0))
    meta = {"generated_utc": NOW.isoformat(timespec="minutes"), "total_listings": len(listings),
            "shortlist_count": len(shortlist)}
    (DATA / "listings.json").write_text(json.dumps({"meta": meta, "items": listings}, indent=1))
    (DATA / "shortlist.json").write_text(json.dumps({"meta": meta, "items": shortlist}, indent=1))
    seen_path.write_text(json.dumps(seen, indent=0, sort_keys=True))

    lines = [f"# MI surplus shortlist — {meta['generated_utc']} UTC",
             f"{len(shortlist)} candidates from {len(listings)} Michigan listings", ""]
    for it in shortlist:
        price = f"${it['price']:,.0f}" if it["price"] is not None else "?"
        lines.append(f"- **{it['title']}** — {price} — {it['time_left'] or 'n/a'} — "
                     f"{it['category']} — {it['source']}{' — NEW' if it['new_this_run'] else ''}\n  {it['url']}")
    (DATA / "shortlist.md").write_text("\n".join(lines) + "\n")
    log(f"Shortlist: {len(shortlist)} of {len(listings)}")
    return 0 if listings else 1


if __name__ == "__main__":
    sys.exit(main())

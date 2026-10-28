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


# ---------------------------------------------------------------- generic card parser
def hours_left_compact(text: str) -> float | None:
    """'1d 4h 3m', '5h 2m', '45m 10s' -> hours (HiBid-style countdowns)."""
    m = re.search(r"\b(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?\b", text or "")
    best = None
    for d, h, mi, s in re.findall(r"(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?", text or ""):
        if not any((d, h, mi)):
            continue
        val = int(d or 0) * 24 + int(h or 0) + int(mi or 0) / 60 + int(s or 0) / 3600
        best = val if best is None else min(best, val)
    return round(best, 2) if best is not None else hours_left(text)


def date_hours(mdy: str) -> float | None:
    try:
        dt = datetime.strptime(mdy, "%m/%d/%Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return round((dt - NOW).total_seconds() / 3600 + 24, 1)  # assume end of that day


def parse_cards(html: str, link_re: str, base: str, source: str, prefix: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, dict] = {}
    rx = re.compile(link_re)
    groups: dict[str, list] = {}
    for a in soup.find_all("a", href=rx):
        groups.setdefault(rx.search(a["href"]).group(1), []).append(a)

    def ids_in(node) -> set:
        return {rx.search(x["href"]).group(1) for x in node.find_all("a", href=rx)}

    junk = re.compile(r"^(lot\s*#?\s*\d+|view|bid|details|bid now|\d+|watch|more info)$", re.I)
    for lid, anchors in groups.items():
        texts = [(x.get("title") or x.get_text(" ", strip=True)).strip() for x in anchors]
        texts = [t for t in texts if len(t) >= 4 and not junk.match(t)]
        if not texts:
            continue
        title = max(texts, key=len)
        a = anchors[0]
        card = a
        while card.parent is not None and card.parent.name not in ("body", "html", "[document]") \
                and ids_in(card.parent) <= {lid}:
            card = card.parent
        text = card.get_text(" ", strip=True)
        m_price = re.search(r"(?:Current|High)\s*Bid:?\s*\$?\s*([\d,]+(?:\.\d\d)?)", text, re.I) \
            or re.search(r"\$\s*([\d,]+(?:\.\d\d)?)", text)
        m_bids = re.search(r"(\d+)\s*Bids?\b", text, re.I)
        m_loc = re.search(r"((?:[A-Z][a-z\.]+\s)?[A-Z][a-z\.]+),\s*(MI|Michigan)\b", text)
        left = re.search(r"\b(\d{1,3}d(?:\s+\d{1,2}h)?(?:\s+\d{1,2}m)?|\d{1,2}h(?:\s+\d{1,2}m)?|\d{1,2}m\s+\d{1,2}s)\b", text)
        m_date = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", text)
        if re.search(r"\b(closed|sold|ended)\b", text, re.I) and not left:
            continue
        href = a["href"]
        out[lid] = {
            "source": source,
            "id": f"{prefix}-{lid}",
            "title": title,
            "price": float(m_price.group(1).replace(",", "")) if m_price else None,
            "bids": int(m_bids.group(1)) if m_bids else None,
            "time_left": left.group(1) if left else (f"closes/opens {m_date.group(1)}" if m_date else ""),
            "hours_left": hours_left_compact(left.group(1)) if left else date_hours(m_date.group(1)) if m_date else None,
            "location": f"{m_loc.group(1).strip()}, MI" if m_loc else "MI",
            "url": href if href.startswith("http") else base + href,
        }
    return list(out.values())


# ---------------------------------------------------------------- HiBid (Michigan)
def scrape_hibid() -> list[dict]:
    from urllib.parse import quote
    items: dict[str, dict] = {}
    for q in CONFIG["hibid_queries"]:
        html = get(f"https://hibid.com/michigan/lots?q={quote(q)}")
        if not html:
            continue
        if q == CONFIG["hibid_queries"][0]:
            save_debug("hibid_q0.html", html)
        found = parse_cards(html, r"/lot/(\d+)/", "https://hibid.com", "HiBid", "hb")
        new = [f for f in found if f["id"] not in items]
        for f in new:
            items[f["id"]] = f
        log(f"HiBid '{q}': {len(new)} new lots")
        time.sleep(2)
    return list(items.values())


# ---------------------------------------------------------------- Miedema
def scrape_miedema() -> list[dict]:
    base = "https://bid.miedemaauctioneering.com"
    html = get(base + "/")
    if not html:
        return []
    save_debug("miedema_home.html", html)
    ids = sorted(set(re.findall(r"auction(?:_id=|/)(\d+)", html)))
    log(f"Miedema: {len(ids)} auctions found")
    items: dict[str, dict] = {}
    for aid in ids[: CONFIG["miedema_max_auctions"]]:
        for page in range(1, CONFIG["miedema_max_pages"] + 1):
            ah = get(f"{base}/?auction_id={aid}&page={page}&limit=100") or ""
            if aid == ids[0] and page == 1:
                save_debug("miedema_auction.html", ah)
            found = parse_cards(ah, r"/lot/(\d+)", base, "Miedema", "md")
            new = [f for f in found if f["id"] not in items]
            for f in new:
                items[f["id"]] = f
            if not new:
                break
            time.sleep(1.5)
    return list(items.values())


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
    for name, fn in (("Public Surplus", scrape_publicsurplus), ("Municibid", scrape_municibid),
                     ("HiBid", scrape_hibid), ("Miedema", scrape_miedema)):
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

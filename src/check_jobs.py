import json
import os
import re
import time
from pathlib import Path
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

BASE = "https://recruitment.macquarie.com/en_US/careers"
SEARCH_URL = f"{BASE}/SearchJobs"
STATE_PATH = Path("jobs/seen_jobs.json")

# --- Performance knobs ---
RECORDS_PER_PAGE = 9
OFFSETS = list(range(0, 72, 9))  # ~8 pages (fast). Increase if needed: range(0, 180, 9)
HTTP_TIMEOUT_SECONDS = 10        # keep it snappy
MAX_SECONDS = 120               # hard cap to avoid long runs (2 minutes)

# --- Filters ---
KEYWORDS = [
    "technology risk",
    "operational risk",
    "risk manager",
    "business operational risk",
    "technology business operational risk",
    "tborm",
    "operational resilience",
    "resilience",
    "controls",
    "governance",
    "first line",
    "1lod",
    "macquarie asset management",
    "2LOD",
    "Cloud Security"
    "Privacy"
    "SOX"
    "Audit"
]

# Hard location filter: London + UK variants
LONDON_UK_PATTERNS = [
    r"\blondon\b.*\b(uk|united kingdom|england)\b",
    r"\b(uk|united kingdom|england)\b.*\blondon\b",
]


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def is_london_uk(text: str) -> bool:
    t = norm(text)
    return any(re.search(p, t) for p in LONDON_UK_PATTERNS)


def keyword_match(text: str) -> bool:
    t = norm(text)
    return any(k in t for k in KEYWORDS)


def load_seen_ids() -> set:
    if not STATE_PATH.exists():
        return set()
    data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return set(data.get("seen_job_ids", []))


def save_seen_ids(seen: set) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"seen_job_ids": sorted(seen)}, indent=2),
        encoding="utf-8",
    )


def http_get(url: str, params: Optional[dict] = None) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; macquarie-job-agent/1.0)",
        "Accept-Language": "en-GB,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    r = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
    r.raise_for_status()
    return r.text


def extract_job_links(html: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if "/JobDetail" in href:
            if href.startswith("/"):
                href = "https://recruitment.macquarie.com" + href
            links.append(href)
    # de-dupe while preserving order
    return list(dict.fromkeys(links))


def parse_job_id(url: str) -> Optional[str]:
    # Patterns:
    # .../JobDetail?jobId=20258
    # .../JobDetail/Some-Title/15459
    m = re.search(r"[?&]jobId=(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/JobDetail/.*/(\d+)", url)
    if m:
        return m.group(1)
    return None


def extract_title(soup: BeautifulSoup) -> str:
    h = soup.find(["h1", "h2"])
    return h.get_text(" ", strip=True) if h else "Macquarie role"


def extract_location_guess(text: str) -> str:
    # best-effort: find a London, UK string near “Location”
    t = re.sub(r"\s+", " ", text)
    m = re.search(r"(?i)\blocation\b[:\s-]*([^|]{0,80})", t)
    if m:
        return m.group(1).strip()
    # fallback: London + UK snippet
    m2 = re.search(r"(?i)(London[^.]{0,80}(United Kingdom|UK|England))", t)
    if m2:
        return m2.group(1).strip()
    return "London, UK (inferred)"


def extract_summary_bullets(soup: BeautifulSoup, limit: int = 3) -> List[str]:
    bullets = []
    for li in soup.find_all("li"):
        txt = li.get_text(" ", strip=True)
        if 25 <= len(txt) <= 220:
            bullets.append(txt)
        if len(bullets) >= limit:
            break
    return bullets


def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram secrets missing; skipping Telegram send.")
        return

    api = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "disable_web_page_preview": True}
    r = requests.post(api, json=payload, timeout=HTTP_TIMEOUT_SECONDS)
    r.raise_for_status()


def main() -> int:
    start = time.time()
    seen_ids = load_seen_ids()
    new_alerts = []

    def time_cap_reached() -> bool:
        return (time.time() - start) > MAX_SECONDS

    for offset in OFFSETS:
        if time_cap_reached():
            print("Time cap reached; stopping early.")
            break

        params = {
            "jobOffset": offset,
            "jobRecordsPerPage": RECORDS_PER_PAGE,
            "listFilterMode": 1,
        }

        try:
            search_html = http_get(SEARCH_URL, params=params)
        except Exception as e:
            print(f"Search page fetch failed at offset={offset}: {e}")
            continue

        links = extract_job_links(search_html)

        for link in links:
            if time_cap_reached():
                print("Time cap reached during link scan; stopping early.")
                break

            job_id = parse_job_id(link)
            if not job_id or job_id in seen_ids:
                continue

            try:
                detail_html = http_get(link)
            except Exception as e:
                print(f"Detail fetch failed for {link}: {e}")
                continue

            soup = BeautifulSoup(detail_html, "lxml")
            page_text = soup.get_text(" ", strip=True)

            # HARD location filter
            if not is_london_uk(page_text):
                continue

            # Keyword filter
            if not keyword_match(page_text):
                continue

            title = extract_title(soup)
            location = extract_location_guess(page_text)
            bullets = extract_summary_bullets(soup, limit=3)

            msg_lines = [
                "📢 New Macquarie London (UK) role matching your filters",
                "",
                f"• {title}",
                f"• Location: {location}",
                f"• Link: {link}",
            ]
            for b in bullets:
                msg_lines.append(f"  - {b}")

            new_alerts.append("\n".join(msg_lines))
            seen_ids.add(job_id)

    # Persist state even if we stop early
    save_seen_ids(seen_ids)

    if not new_alerts:
        print("No new matching London, UK roles found.")
        # Heartbeat: remove this line later if you don’t want a daily ping
        send_telegram("✅ Macquarie job agent ran successfully — no new London (UK) matches today.")
        return 0

    for alert in new_alerts:
        send_telegram(alert)

    print(f"Sent {len(new_alerts)} Telegram alert(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import json
import os
import re
from pathlib import Path
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

BASE = "https://recruitment.macquarie.com/en_US/careers"
SEARCH_URL = f"{BASE}/SearchJobs"
STATE_PATH = Path("jobs/seen_jobs.json")

RECORDS_PER_PAGE = 9
OFFSETS = list(range(0, 180, 9))  # scan ~20 pages

KEYWORDS = [
    "technology risk",
    "operational risk",
    "risk manager",
    "business operational risk",
    "technology business operational risk",
    "tborm",
    "operational resilience",
    "controls",
    "governance",
    "first line",
    "1lod",
    "macquarie asset management",
    "mam",
]

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
    data = json.loads(STATE_PATH.read_text())
    return set(data.get("seen_job_ids", []))


def save_seen_ids(seen: set) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"seen_job_ids": sorted(seen)}, indent=2))


def http_get(url: str, params: Optional[dict] = None) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-GB,en;q=0.9",
    }
    r = requests.get(url, params=params, headers=headers, timeout=30)
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
    return list(dict.fromkeys(links))


def parse_job_id(url: str) -> Optional[str]:
    m = re.search(r"[?&]jobId=(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/JobDetail/.*/(\d+)", url)
    if m:
        return m.group(1)
    return None


def extract_summary_bullets(soup: BeautifulSoup, limit: int = 3) -> List[str]:
    bullets = []
    for li in soup.find_all("li"):
        text = li.get_text(" ", strip=True)
        if 25 <= len(text) <= 200:
            bullets.append(text)
        if len(bullets) >= limit:
            break
    return bullets


def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }
    requests.post(url, json=payload, timeout=20).raise_for_status()


def main() -> int:
    seen_ids = load_seen_ids()
    new_messages = []

    for offset in OFFSETS:
        params = {
            "jobOffset": offset,
            "jobRecordsPerPage": RECORDS_PER_PAGE,
            "listFilterMode": 1,
        }
        html = http_get(SEARCH_URL, params=params)
        links = extract_job_links(html)

        for link in links:
            job_id = parse_job_id(link)
            if not job_id or job_id in seen_ids:
                continue

            detail_html = http_get(link)
            soup = BeautifulSoup(detail_html, "lxml")
            page_text = soup.get_text(" ", strip=True)

            if not is_london_uk(page_text):
                continue

            if not keyword_match(page_text):
                continue

            bullets = extract_summary_bullets(soup)
            message = [
                "📢 New Macquarie London (UK) role found",
                "",
                link,
            ]
            for b in bullets:
                message.append(f"• {b}")

            new_messages.append("\n".join(message))
            seen_ids.add(job_id)

    if not new_messages:
        print("No new matching London, UK roles found.")
        send_telegram("✅ Macquarie job agent ran successfully — no new London (UK) matches today.")
        save_seen_ids(seen_ids)
        return 0

    for msg in new_messages:
        send_telegram(msg)

    save_seen_ids(seen_ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

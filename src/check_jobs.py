import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

BASE = "https://recruitment.macquarie.com/en_US/careers"
SEARCH_URL = f"{BASE}/SearchJobs"
STATE_PATH = Path("jobs/seen_jobs.json")

# Hard location filter: London + UK variants
LONDON_UK_PATTERNS = [
    r"\blondon\b.*\b(uk|united kingdom|england)\b",
    r"\b(uk|united kingdom|england)\b.*\blondon\b",
]

# Theme keywords (edit freely)
KEYWORDS = [
    "technology risk",
    "operational risk",
    "risk manager",
    "technology business operational risk",
    "tborm",
    "operational resilience",
    "controls",
    "governance",
    "macquarie asset management",
    "mam",
]

# Pagination knobs
RECORDS_PER_PAGE = 9
OFFSETS = [0, 9, 18, 27, 36]  # first 5 pages


@dataclass
class Job:
    job_id: str
    title: str
    location: str
    posted: str
    url: str


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def is_london_uk(location: str) -> bool:
    loc = norm(location)
    return any(re.search(p, loc) for p in LONDON_UK_PATTERNS)


def keyword_match(text: str) -> bool:
    t = norm(text)
    return any(k in t for k in KEYWORDS)


def load_state() -> set:
    if not STATE_PATH.exists():
        return set()
    data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return set(data.get("seen_job_ids", []))


def save_state(seen: set) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"seen_job_ids": sorted(seen)}, indent=2),
        encoding="utf-8",
    )


def http_get(url: str, params: Optional[dict] = None) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    r = requests.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text


def extract_job_links(search_html: str) -> List[str]:
    soup = BeautifulSoup(search_html, "lxml")
    links = []
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if "/JobDetail" in href:
            if href.startswith("/"):
                href = "https://recruitment.macquarie.com" + href
            links.append(href)

    # de-dupe while preserving order
    seen = set()
    out = []
    for u in links:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def parse_job_id(url: str) -> Optional[str]:
    # Examples:
    # .../JobDetail?jobId=20258
    # .../JobDetail/Some-Title/15459
    m = re.search(r"[?&]jobId=(\d+)\b", url)
    if m:
        return m.group(1)
    m2 = re.search(r"/JobDetail/.*/(\d+)(?:[/?#]|$)", url)
    if m2:
        return m2.group(1)
    return None


def extract_summary_bullets(soup: BeautifulSoup, max_bullets: int = 3) -> List[str]:
    """
    Best-effort: find 2-3 meaningful bullets from the job detail page.
    Strategy:
      1) Prefer bullets under common headings (Responsibilities/Requirements/etc.)
      2) Else, take the first short <li> items anywhere
      3) Else, take a couple short paragraphs as bullets
    """
    heading_keywords = [
        "responsibilities", "key responsibilities", "what you will do",
        "key accountabilities", "accountabilities", "role", "about the role",
        "requirements", "skills", "what you bring", "about you", "you will",
        "key skills", "who you are"
    ]

    def clean_bullet(t: str) -> str:
        t = re.sub(r"\s+", " ", (t or "").strip())
        if len(t) > 220:
            t = t[:217].rstrip() + "…"
        return t

    bullets: List[str] = []

    # 1) Find bullets near a relevant heading
    headings = soup.find_all(["h1", "h2", "h3", "h4"])
    for h in headings:
        ht = norm(h.get_text(" ", strip=True))
        if not ht:
            continue
        if any(k in ht for k in heading_keywords):
            nxt = h
            for _ in range(12):
                nxt = nxt.find_next()
                if not nxt:
                    break
                if nxt.name in ["ul", "ol"]:
                    li_items = [clean_bullet(li.get_text(" ", strip=True)) for li in nxt.find_all("li")]
                    li_items = [x for x in li_items if x and len(x) >= 20]
                    for x in li_items:
                        if x not in bullets:
                            bullets.append(x)
                        if len(bullets) >= max_bullets:
                            return bullets
                    break

    # 2) Fallback: first decent <li> bullets anywhere
    if len(bullets) < max_bullets:
        for li in soup.find_all("li"):
            txt = clean_bullet(li.get_text(" ", strip=True))
            if txt and 20 <= len(txt) <= 220 and txt not in bullets:
                bullets.append(txt)
            if len(bullets) >= max_bullets:
                return bullets

    # 3) Fallback: short paragraphs as bullets
    if len(bullets) < max_bullets:
        for p in soup.find_all("p"):
            txt = clean_bullet(p.get_text(" ", strip=True))
            if txt and 40 <= len(txt) <= 220 and txt not in bullets:
                bullets.append(txt)
            if len(bullets) >= max_bullets:
                return bullets

    return bullets[:max_bullets]


def parse_job_detail(detail_html: str, url: str, job_id: str) -> Job:
    soup = BeautifulSoup(detail_html, "lxml")

    # Title
    h1 = soup.find(["h1", "h2"])
    title = h1.get_text(strip=True) if h1 else f"Job {job_id}"

    # Location heuristics
    location = ""
    for line in soup.get_text("\n", strip=True).split("\n"):
        ln = line.strip()
        if re.search(r"\blocation\b", ln, flags=re.I):
            loc_guess = re.sub(r"(?i)^location[:\s]*", "", ln).strip()
            if loc_guess and len(loc_guess) <= 100:
                location = loc_guess
                break

    if not location:
        text_norm = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        mloc = re.search(r"(London[^.]{0,80}(United Kingdom|UK|England))", text_norm, flags=re.I)
        if mloc:
            location = mloc.group(1).strip()

    # Posted date heuristics
    posted = ""
    for line in soup.get_text("\n", strip=True).split("\n"):
        if re.search(r"\b(posted|date posted)\b", line, flags=re.I):
            posted_guess = re.sub(r"(?i).*(posted|date posted)[:\s]*", "", line).strip()
            if posted_guess and len(posted_guess) <= 60:
                posted = posted_guess
                break

    return Job(
        job_id=job_id,
        title=title,
        location=location or "Unknown",
        posted=posted or "Unknown",
        url=url,
    )


def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "disable_web_page_preview": True}
    requests.post(api, json=payload, timeout=30).raise_for_status()


def main() -> int:
    seen = load_state()
    new_matches: List[Job] = []

    for offset in OFFSETS:
        params = {
            "jobOffset": offset,
            "jobRecordsPerPage": RECORDS_PER_PAGE,
            "listFilterMode": 1,
        }
        search_html = http_get(SEARCH_URL, params=params)
        links = extract_job_links(search_html)

        for link in links:
            job_id = parse_job_id(link)
            if not job_id or job_id in seen:
                continue

            try:
                detail_html = http_get(link)
            except Exception:
                continue

            job = parse_job_detail(detail_html, link, job_id)

            # HARD location filter: London + UK
            if not is_london_uk(job.location):
                # Fallback: inspect raw HTML for London/UK words if location isn't scraped cleanly
                if not (re.search(r"\blondon\b", detail_html, flags=re.I) and
                        re.search(r"\b(united kingdom|uk|england)\b", detail_html, flags=re.I)):
                    continue

            # Keyword filter on title + full text
            soup = BeautifulSoup(detail_html, "lxml")
            blob = f"{job.title} {soup.get_text(' ', strip=True)}"
            if not keyword_match(blob):
                continue

            new_matches.append(job)
            
    if not new_matches:
    print("No new matching London, UK roles found.")
    send_telegram("✅ Macquarie job agent ran successfully — no new London (UK) matches today.")
    return 0
    
    # Build Telegram message (with 2–3 bullet summary)
    lines = ["New Macquarie London (UK) roles matching your filters:"]

    for j in new_matches:
        lines += [
            "",
            f"• {j.title}",
            f"  Location: {j.location}",
            f"  Posted: {j.posted}",
            f"  Link: {j.url}",
        ]
        try:
            detail_html = http_get(j.url)
            soup = BeautifulSoup(detail_html, "lxml")
            bullets = extract_summary_bullets(soup, max_bullets=3)
            for b in bullets:
                lines.append(f"  - {b}")
        except Exception:
            pass

    msg = "\n".join(lines)
    print(msg)

    # Notify
    try:
        send_telegram(msg)
    except Exception as e:
        print(f"Telegram notify failed: {e}", file=sys.stderr)

    # Update state so we don't alert again for the same jobs
    for j in new_matches:
        seen.add(j.job_id)
    save_state(seen)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

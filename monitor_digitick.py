import os
import json
import time
import hashlib
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://web.digitick.com/index-css5-rhe76mobile-pg1.html"
STATE_FILE = Path("state.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (hockey-monitor)",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

def fetch_html(url: str) -> str:
    url_nocache = f"{url}?t={int(time.time())}"
    r = requests.get(url_nocache, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.content.decode(r.encoding or "utf-8", errors="replace")

def extract_items(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    items = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = " ".join(a.get_text(" ", strip=True).split())

        if href.startswith("/"):
            href = "https://web.digitick.com" + href

        if "digitick.com" not in href:
            continue

        if len(text) < 3:
            continue

        key_src = (href + "||" + text).encode("utf-8", errors="ignore")
        key = hashlib.sha1(key_src).hexdigest()

        items.append({"title": text[:200], "href": href, "key": key})

    uniq = {}
    for it in items:
        uniq[it["key"]] = it
    return list(uniq.values())

def load_seen() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return set(data.get("seen_keys", []))

def save_seen(seen: set[str]):
    STATE_FILE.write_text(
        json.dumps({"seen_keys": sorted(seen)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram non configuré.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=20)

def main():
    html = fetch_html(URL)
    current = extract_items(html)

    seen = load_seen()
    new_items = [it for it in current if it["key"] not in seen]

    if new_items:
        lines = ["🏒 Nouveaux matchs détectés :"]
        for it in new_items:
            lines.append(f"• {it['title']}")
            lines.append(f"  {it['href']}")
        msg = "\n".join(lines)

        send_telegram(msg)

        for it in new_items:
            seen.add(it["key"])
        save_seen(seen)

if __name__ == "__main__":
    main()

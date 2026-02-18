import os
import json
import time
import re
import hashlib
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://web.digitick.com/index-css5-rhe76mobile-pg1.html"
STATE_FILE = Path("state.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Exemple: "ROUEN vs GRENOBLE - SLM - 19/02/2026"
TITLE_RE = re.compile(
    r"^(?P<home>ROUEN)\s+vs\s+(?P<away>.+?)\s*-\s*SLM\s*-\s*(?P<date>\d{2}/\d{2}/\d{4})$",
    re.IGNORECASE
)

# Exemple: "Jeudi 19 Février 2026 - 20h00"
DETAIL_RE = re.compile(r".+?\s-\s(?P<time>\d{1,2}h\d{2})", re.IGNORECASE)

def fetch_html(url: str) -> str:
    url_nocache = f"{url}?t={int(time.time())}"
    r = requests.get(url_nocache, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.content.decode(r.encoding or "utf-8", errors="replace")

def normalize_href(href: str) -> str:
    href = href.strip()
    if href.startswith("/"):
        return "https://web.digitick.com" + href
    return href

def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram non configuré (secrets manquants).")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=20)

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

def extract_matches(html: str) -> list[dict]:
    """
    On repère les 'cartes' match sur la page.
    Chaque match a un titre + souvent une ligne de détails dessous.
    """
    soup = BeautifulSoup(html, "lxml")

    matches = []

    # On parcourt tous les textes, et on détecte ceux qui matchent TITLE_RE
    # Pour essayer de récupérer l'heure, on regarde le texte du parent (bloc)
    for a in soup.find_all("a", href=True):
        title = " ".join(a.get_text(" ", strip=True).split())
        m = TITLE_RE.match(title)
        if not m:
            continue

        href = normalize_href(a["href"])

        # Essayer de trouver un "détail" (heure) dans le même bloc
        block_text = " ".join(a.parent.get_text(" ", strip=True).split()) if a.parent else ""
        time_match = DETAIL_RE.search(block_text)
        hour = time_match.group("time") if time_match else None

        home = m.group("home").upper()
        away = m.group("away").upper()
        date = m.group("date")

        # clé stable basée sur titre (si nouveau titre => nouveau match)
        key = hashlib.sha1(title.encode("utf-8")).hexdigest()

        matches.append({
            "key": key,
            "title": title,
            "home": home,
            "away": away,
            "date": date,
            "hour": hour,
            "href": href,
        })

    # dédup
    uniq = {}
    for it in matches:
        uniq[it["key"]] = it
    return list(uniq.values())

def format_message(match: dict) -> str:
    lines = ["🏒 Nouveau match Dragons détecté !"]
    lines.append(f"🆚 {match['home']} vs {match['away']}")
    if match.get("date"):
        lines.append(f"📅 {match['date']}")
    if match.get("hour"):
        lines.append(f"🕒 {match['hour']}")
    lines.append(f"🔗 {match['href']}")
    return "\n".join(lines)

def main():
    html = fetch_html(URL)
    matches = extract_matches(html)

    seen = load_seen()
    new_matches = [m for m in matches if m["key"] not in seen]

    if new_matches:
        for m in new_matches:
            send_telegram(format_message(m))
            seen.add(m["key"])
        save_seen(seen)
    else:
        print("Rien de nouveau.")

if __name__ == "__main__":
    main()

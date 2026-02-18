import os
import json
import time
import re
import hashlib
from pathlib import Path

import requests
from bs4 import BeautifulSoup

LIST_URL = "https://web.digitick.com/index-css5-rhe76mobile-pg1.html"
STATE_FILE = Path("state.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Détection sur la page liste ---
# Exemple: "ROUEN vs GRENOBLE - SLM- 19/02/2026" (il y a parfois des espaces bizarres)
TITLE_RE = re.compile(
    r"^(?P<home>ROUEN)\s+vs\s+(?P<away>.+?)\s*-\s*SLM\s*-\s*(?P<date>\d{2}/\d{2}/\d{4})$",
    re.IGNORECASE
)

# Exemple: "Jeudi 19 Février 2026 - 20h00"
DETAIL_RE = re.compile(r".+?\s-\s(?P<time>\d{1,2}h\d{2})", re.IGNORECASE)

# --- Détection sold out sur page événement ---
SOLD_OUT_PATTERNS = [
    "Toutes les places ont été vendues ou ajoutées en panier",
    "Aucune place disponible",
    "Complet",
]

def fetch_html(url: str) -> str:
    url_nocache = f"{url}?t={int(time.time())}"
    r = requests.get(url_nocache, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.content.decode(r.encoding or "utf-8", errors="replace")

def normalize_href(href: str) -> str:
    href = href.strip()
    if href.startswith("/"):
        return "https://web.digitick.com" + href
    if href.startswith("http"):
        return href
    return "https://web.digitick.com/" + href.lstrip("./")

def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram non configuré (secrets manquants).")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=20)

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"seen_keys": [], "events": {}}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))

def save_state(state: dict):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def extract_matches_from_list(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    matches = []

    for a in soup.find_all("a", href=True):
        title = " ".join(a.get_text(" ", strip=True).split())
        m = TITLE_RE.match(title)
        if not m:
            continue

        href = normalize_href(a["href"])

        block_text = " ".join(a.parent.get_text(" ", strip=True).split()) if a.parent else ""
        time_match = DETAIL_RE.search(block_text)
        hour = time_match.group("time") if time_match else None

        home = m.group("home").upper()
        away = m.group("away").upper()
        date = m.group("date")

        key = hashlib.sha1(href.encode("utf-8")).hexdigest()  # stable sur URL

        matches.append({
            "key": key,
            "title": title,
            "home": home,
            "away": away,
            "date": date,
            "hour": hour,
            "href": href,
        })

    uniq = {}
    for it in matches:
        uniq[it["key"]] = it
    return list(uniq.values())

def detect_availability(event_html: str) -> str:
    text = " ".join(BeautifulSoup(event_html, "lxml").get_text(" ", strip=True).split()).lower()
    for p in SOLD_OUT_PATTERNS:
        if p.lower() in text:
            return "SOLD_OUT"
    return "AVAILABLE"

def format_new_match_message(match: dict) -> str:
    lines = ["🏒 Nouveau match détecté !"]
    lines.append(f"🆚 {match['home']} vs {match['away']}")
    if match.get("date"):
        lines.append(f"📅 {match['date']}")
    if match.get("hour"):
        lines.append(f"🕒 {match['hour']}")
    lines.append(f"🔗 {match['href']}")
    return "\n".join(lines)

def format_status_change_message(match: dict, old: str, new: str) -> str:
    if old == "SOLD_OUT" and new == "AVAILABLE":
        head = "🚨 PLACES DISPO (probable) !"
    elif old == "AVAILABLE" and new == "SOLD_OUT":
        head = "⛔️ Ça vient de repasser complet."
    else:
        head = "🔁 Changement de statut détecté."

    lines = [head]
    lines.append(f"🆚 {match['home']} vs {match['away']}")
    if match.get("date"):
        lines.append(f"📅 {match['date']}")
    if match.get("hour"):
        lines.append(f"🕒 {match['hour']}")
    lines.append(f"📌 {old} → {new}")
    lines.append(f"🔗 {match['href']}")
    return "\n".join(lines)

def main():
    state = load_state()
    seen = set(state.get("seen_keys", []))
    events = state.get("events", {})  # key -> {"status": "...", "last_seen": ...}

    list_html = fetch_html(LIST_URL)
    matches = extract_matches_from_list(list_html)

    # 1) Nouveaux matchs
    for m in matches:
        if m["key"] not in seen:
            send_telegram(format_new_match_message(m))
            seen.add(m["key"])
            # init event record (on met un statut au prochain step)
            events.setdefault(m["key"], {"status": "UNKNOWN", "last_seen": None})

    # 2) Statut dispo/complet pour chaque match
    for m in matches:
        try:
            event_html = fetch_html(m["href"])
            new_status = detect_availability(event_html)
        except Exception as e:
            print(f"Erreur fetch event {m['href']}: {e}")
            continue

        old_status = events.get(m["key"], {}).get("status", "UNKNOWN")

        # on notifie seulement si ça change (et qu'on a un old connu)
        if old_status != "UNKNOWN" and new_status != old_status:
            send_telegram(format_status_change_message(m, old_status, new_status))

        events[m["key"]] = {
            "status": new_status,
            "last_seen": int(time.time()),
            "href": m["href"],
            "title": m["title"],
        }

    state["seen_keys"] = sorted(list(seen))
    state["events"] = events
    save_state(state)

    print("OK")

if __name__ == "__main__":
    main()

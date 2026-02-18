import os
import json
import time
import re
import hashlib
from pathlib import Path
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

# Page liste (celle que tu m'as donnée)
LIST_URL = "https://web.digitick.com/index-css5-rhe76mobile-pg1.html"
STATE_FILE = Path("state.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; HockeyMonitor/1.0)",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Exemple liste: "ROUEN vs GRENOBLE - SLM- 19/02/2026" (tolérant sur espaces et tirets)
TITLE_RE = re.compile(
    r"^(?P<home>ROUEN)\s+vs\s+(?P<away>.+?)\s*-\s*SLM\s*-\s*(?P<date>\d{2}/\d{2}/\d{4})$",
    re.IGNORECASE
)

# Exemple dans la carte: "Jeudi 19 Février 2026 - 20h00"
DETAIL_RE = re.compile(r"(?P<time>\d{1,2}h\d{2})", re.IGNORECASE)

# Phrases typiques "plus de places"
SOLD_OUT_PHRASES = [
    "toutes les places ont été vendues",
    "ajoutées en panier",
    "aucune place disponible",
    "complet",
]


def fetch_html(url: str, timeout: int = 25) -> str:
    """
    Récupère une page en évitant le cache (ajout d'un param t=timestamp).
    """
    url_nocache = f"{url}{'&' if '?' in url else '?'}t={int(time.time())}"
    r = requests.get(url_nocache, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.content.decode(r.encoding or "utf-8", errors="replace")


def normalize_href(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("/"):
        return "https://web.digitick.com" + href
    return href


def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram non configuré (secrets manquants).")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=20)
    except Exception as e:
        print(f"[WARN] Envoi Telegram échoué: {e}")


def load_state() -> Dict:
    if not STATE_FILE.exists():
        return {"seen_keys": [], "events": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        # si le JSON a été corrompu, on repart propre
        return {"seen_keys": [], "events": {}}


def save_state(state: Dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def extract_matches_from_list(html: str) -> List[Dict]:
    """
    Parse la page LIST_URL et sort tous les matchs.
    """
    soup = BeautifulSoup(html, "lxml")
    matches: List[Dict] = []

    for a in soup.find_all("a", href=True):
        title = " ".join(a.get_text(" ", strip=True).split())
        m = TITLE_RE.match(title)
        if not m:
            continue

        href = normalize_href(a.get("href", ""))

        # heure: on cherche dans le texte du parent (bloc carte)
        block_text = ""
        if a.parent:
            block_text = " ".join(a.parent.get_text(" ", strip=True).split())
        tmatch = DETAIL_RE.search(block_text)
        hour = tmatch.group("time") if tmatch else None

        home = m.group("home").upper()
        away = m.group("away").strip().upper()
        date = m.group("date")

        # clé stable : titre + href (au cas où un même titre existerait avec un lien différent)
        key_src = f"{title}|{href}"
        key = hashlib.sha1(key_src.encode("utf-8")).hexdigest()

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


def detect_event_status(event_html: str) -> str:
    """
    Détermine AVAILABLE / SOLD_OUT en lisant le HTML de la page évènement.
    """
    text = " ".join(BeautifulSoup(event_html, "lxml").get_text(" ", strip=True).split()).lower()

    # Si on retrouve une des phrases “plus de places”, on considère SOLD_OUT
    for phrase in SOLD_OUT_PHRASES:
        if phrase in text:
            return "SOLD_OUT"

    # Sinon on considère qu’il y a de la dispo (ou au moins pas "complet")
    return "AVAILABLE"


def format_new_match_message(match: Dict, status: str) -> str:
    lines = ["🏒 Nouveau match Dragons détecté !"]
    lines.append(f"🆚 {match['home']} vs {match['away']}")
    if match.get("date"):
        lines.append(f"📅 {match['date']}")
    if match.get("hour"):
        lines.append(f"🕒 {match['hour']}")
    lines.append(f"🎟️ Statut: {status}")
    lines.append(f"🔗 {match['href']}")
    return "\n".join(lines)


def format_status_change_message(match: Dict, old_status: str, new_status: str) -> str:
    # message simple et très lisible
    emoji = "✅" if new_status == "AVAILABLE" else "⛔️"
    lines = [f"{emoji} Changement de statut billetterie !"]
    lines.append(f"🆚 {match['home']} vs {match['away']}")
    if match.get("date"):
        lines.append(f"📅 {match['date']}")
    if match.get("hour"):
        lines.append(f"🕒 {match['hour']}")
    lines.append(f"🔁 {old_status} → {new_status}")
    lines.append(f"🔗 {match['href']}")
    return "\n".join(lines)


def main() -> None:
    now = int(time.time())

    # 1) lire la liste
    list_html = fetch_html(LIST_URL)
    matches = extract_matches_from_list(list_html)

    # 2) charger state
    state = load_state()
    seen_keys = set(state.get("seen_keys", []))
    events = state.get("events", {}) or {}

    if not matches:
        print("Aucun match détecté sur la page liste.")
        return

    # 3) pour chaque match, lire sa page évènement et détecter le statut
    for match in matches:
        key = match["key"]
        href = match["href"]

        try:
            event_html = fetch_html(href)
            status = detect_event_status(event_html)
        except Exception as e:
            print(f"[WARN] Impossible de lire l'évènement {href} : {e}")
            continue

        old = events.get(key, {}).get("status")

        # cas 1 : nouveau match
        if key not in seen_keys:
            send_telegram(format_new_match_message(match, status))
            seen_keys.add(key)

        # cas 2 : match connu, changement de statut
        elif old and old != status:
            send_telegram(format_status_change_message(match, old, status))

        # mise à jour state
        events[key] = {
            "status": status,
            "last_seen": now,
            "href": href,
            "title": match.get("title"),
            "date": match.get("date"),
            "hour": match.get("hour"),
        }

        print(f"[OK] {match.get('title')} => {status}")

    # 4) sauvegarde
    state["seen_keys"] = sorted(seen_keys)
    state["events"] = events
    save_state(state)


if __name__ == "__main__":
    main()

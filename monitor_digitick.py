import os
import json
import time
import re
import hashlib
import unicodedata
import html as html_lib
from pathlib import Path
from typing import Dict, List

import requests
from bs4 import BeautifulSoup

# Page liste
LIST_URL = "https://web.digitick.com/index-css5-rhe76mobile-pg1.html"
STATE_FILE = Path("state.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; HockeyMonitor/1.0)",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Exemple liste: "ROUEN vs GRENOBLE - SLM - 19/02/2026"
# Date n'importe où (13/03/2026 ou 13-03-2026)
DATE_ANY_RE = re.compile(r"(\d{2}[/-]\d{2}[/-]\d{4})")

# "ROUEN vs XXXXX" n'importe où dans le titre (playoffs inclus)
VS_ANY_RE = re.compile(
    r"\b(ROUEN)\s+vs\s+(.+?)(?=\s+\d{2}[/-]\d{2}[/-]\d{4}\b|\s+-|\s+@|$)",
    re.IGNORECASE,
)

# Exemple dans la carte: "Jeudi 19 Février 2026 - 20h00"
DETAIL_RE = re.compile(r"(?P<time>\d{1,2}h\d{2})", re.IGNORECASE)


def _norm(s: str) -> str:
    # Decode entities, lower, collapse spaces
    s = html_lib.unescape(s).lower()
    s = " ".join(s.split())
    # Remove accents
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s


def detect_event_status(event_html: str) -> str:
    soup = BeautifulSoup(event_html, "lxml")
    text = _norm(soup.get_text(" ", strip=True))

    # DEBUG (tu peux supprimer ces 3 lignes si tu veux des logs plus courts)
    print("------ DEBUG EVENT TEXT START ------")
    print(text[:2000])
    print("------ DEBUG EVENT TEXT END ------")

    # Si module de sélection visible → billets disponibles
    if "plan de la salle" in text or "selection siege" in text:
        return "AVAILABLE"

    # Pages où on voit le gros bouton "RÉSERVER"
    if "reserver" in text:
        return "AVAILABLE"

    # Sinon → sold out
    return "SOLD_OUT"

def fetch_html(url: str, timeout: int = 25) -> str:
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
        return {"seen_keys": [], "events": {}, "consecutive_failures": 0}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        state = {"seen_keys": [], "events": {}, "consecutive_failures": 0}

    # rétro-compat
    if "consecutive_failures" not in state:
        state["consecutive_failures"] = 0
    if "seen_keys" not in state:
        state["seen_keys"] = []
    if "events" not in state:
        state["events"] = {}

    return state


def save_state(state: Dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def extract_matches_from_list(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    matches: List[Dict] = []

    for a in soup.find_all("a", href=True):
        title_raw = " ".join(a.get_text(" ", strip=True).split())
        title_norm = _norm(title_raw)

        # On ne garde que ce qui contient "rouen vs ..."
        mvs = VS_ANY_RE.search(title_raw)
        if not mvs:
            continue

        home = mvs.group(1).upper().strip()  # ROUEN
        away = mvs.group(2).strip().upper()  # AMIENS / ANGERS / etc.

        # On récupère une date où qu'elle soit dans le titre
        d = DATE_ANY_RE.search(title_raw)
        if not d:
            # Si pas de date dans le texte du lien, on tente le bloc parent
            parent_text = " ".join(a.parent.get_text(" ", strip=True).split()) if a.parent else ""
            d = DATE_ANY_RE.search(parent_text)
        date = d.group(1).replace("-", "/") if d else None

        href = normalize_href(a.get("href", ""))

        # Heure éventuelle (dans le bloc autour du lien)
        block_text = " ".join(a.parent.get_text(" ", strip=True).split()) if a.parent else ""
        tmatch = DETAIL_RE.search(block_text)
        hour = tmatch.group("time") if tmatch else None

        # Clé stable
        key_src = f"{title_raw}|{href}"
        key = hashlib.sha1(key_src.encode("utf-8")).hexdigest()

        matches.append(
            {
                "key": key,
                "title": title_raw,
                "home": home,
                "away": away,
                "date": date,
                "hour": hour,
                "href": href,
            }
        )

    # dédoublonnage
    uniq = {}
    for it in matches:
        uniq[it["key"]] = it
    return list(uniq.values())

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

def debug_list_page(html: str) -> None:
    soup = BeautifulSoup(html, "lxml")

    print("========== DEBUG LIST PAGE : LINKS START ==========")
    for i, a in enumerate(soup.find_all("a", href=True), start=1):
        title = " ".join(a.get_text(" ", strip=True).split())
        href = normalize_href(a.get("href", ""))
        if title:
            print(f"[A {i}] TITLE: {title}")
            print(f"[A {i}] HREF : {href}")
    print("========== DEBUG LIST PAGE : LINKS END ==========")

    print("========== DEBUG LIST PAGE : BLOCKS WITH 'ROUEN' START ==========")
    seen = set()
    for tag in soup.find_all(["div", "section", "article", "li"]):
        text = " ".join(tag.get_text(" ", strip=True).split())
        if not text:
            continue
        norm = _norm(text)
        if "rouen" in norm and len(text) > 30:
            short = text[:500]
            if short not in seen:
                seen.add(short)
                print(short)
                print("-----")
    print("========== DEBUG LIST PAGE : BLOCKS WITH 'ROUEN' END ==========")

def main() -> None:
    now = int(time.time())

    state = load_state()
    seen_keys = set(state.get("seen_keys", []))
    events = state.get("events", {}) or {}
    failures = int(state.get("consecutive_failures", 0))

    try:
        list_html = fetch_html(LIST_URL)
        matches = extract_matches_from_list(list_html)

        if not matches:
            print("Aucun match détecté sur la page liste.")
            state["consecutive_failures"] = 0
            save_state(state)
            return

        for match in matches:
            key = match["key"]
            href = match["href"]

            event_html = fetch_html(href)
            status = detect_event_status(event_html)

            old = events.get(key, {}).get("status")

            if key not in seen_keys:
                send_telegram(format_new_match_message(match, status))
                seen_keys.add(key)
            elif old and old != status:
                send_telegram(format_status_change_message(match, old, status))

            events[key] = {
                "status": status,
                "last_seen": now,
                "href": href,
                "title": match.get("title"),
                "date": match.get("date"),
                "hour": match.get("hour"),
            }

            print(f"[OK] {match.get('title')} => {status}")

        # run OK -> reset compteur
        state["consecutive_failures"] = 0

    except Exception as e:
        failures += 1
        state["consecutive_failures"] = failures

        print(f"[ERROR] Run failed: {e}")

        # alerte au 10e, 20e, 30e...
        if failures % 10 == 0:
            send_telegram(
                f"⚠️ Hockey tickets monitor: {failures} échecs d’affilée.\n"
                "Va voir GitHub Actions pour le détail du log."
            )

        state["seen_keys"] = sorted(seen_keys)
        state["events"] = events
        save_state(state)
        return

    state["seen_keys"] = sorted(seen_keys)
    state["events"] = events
    save_state(state)


if __name__ == "__main__":
    main()

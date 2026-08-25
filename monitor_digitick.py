#!/usr/bin/env python3
"""
Monitoring billetterie Rouen Hockey Elite 76 (nouveau site Next.js).

Remplace monitor_digitick.py. Le club a migré vers billetterie.rouenhockeyelite76.com,
une app Next.js qui rend ses données côté serveur (SSR). La page /fr renvoie donc,
dans son HTML brut, un flux JSON contenant chaque évènement à venir avec le champ
`hasPublicOffersAvailable` :
    - True  -> billetterie ouverte  (bouton "Réserver" orange)
    - False -> pas (ou plus) ouverte (bouton grisé)

Le passage False -> True = ouverture des ventes. C'est ce qu'on veut détecter.

Dépendances : requests
"""

import os
import re
import sys
import json
import time
from datetime import datetime, timezone, timedelta

import requests

# --- Configuration -----------------------------------------------------------

BASE_URL = "https://billetterie.rouenhockeyelite76.com"
LIST_URL = BASE_URL + "/fr"          # page "Évènement(s) à venir" (SSR)
STATE_FILE = "state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

PARIS = timezone(timedelta(hours=2))  # suffisant pour l'affichage ; ajuste l'hiver si besoin

HEADERS = {
    # User-Agent réaliste : sans ça, certains fronts renvoient une page vide / un blocage.
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}


# --- Récupération + parsing --------------------------------------------------

def fetch_html(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def _balance_braces(text: str, start: int):
    """Retourne l'objet JSON {...} commençant à l'index `start`, en équilibrant
    les accolades (en ignorant celles à l'intérieur des chaînes)."""
    depth = 0
    in_str = False
    esc = False
    for j in range(start, len(text)):
        c = text[j]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:j + 1], j + 1
    return None, len(text)


def _find_events_in_chunk(text: str):
    """Extrait tous les objets event {...} d'un fragment JSON déséchappé."""
    events = []
    marker = '"event":{'
    idx = 0
    while True:
        i = text.find(marker, idx)
        if i == -1:
            break
        brace_start = i + len('"event":')  # pointe sur le '{'
        obj, end = _balance_braces(text, brace_start)
        idx = end
        if not obj:
            continue
        try:
            data = json.loads(obj)
        except json.JSONDecodeError:
            continue
        if "id" in data and "hasPublicOffersAvailable" in data:
            events.append(data)
    return events


def extract_events(html: str) -> dict:
    """Parcourt les blocs self.__next_f.push([1, "..."]) de la page Next.js,
    déséchappe chaque fragment et en extrait les évènements.
    Retourne {event_id: event_dict}."""
    events = {}
    # Capture le contenu (chaîne échappée) de chaque push RSC.
    pattern = re.compile(r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)', re.DOTALL)
    for m in pattern.finditer(html):
        try:
            chunk = json.loads('"' + m.group(1) + '"')  # déséchappe le fragment
        except json.JSONDecodeError:
            continue
        if '"event":{' not in chunk:
            continue
        for ev in _find_events_in_chunk(chunk):
            events[ev["id"]] = ev
    return events


# --- Mise en forme -----------------------------------------------------------

def format_event(ev: dict) -> dict:
    home = ev.get("homeTeam", {}).get("displayName", "?")
    opp = ev.get("opponent", {}).get("displayName", "?")
    arena = ev.get("arena", {}).get("name", "")
    when = ""
    dt_raw = ev.get("startDatetime")
    if dt_raw:
        try:
            dt = datetime.fromisoformat(dt_raw).astimezone(PARIS)
            when = dt.strftime("%d/%m/%Y %Hh%M")
        except ValueError:
            when = dt_raw
    return {
        "id": ev["id"],
        "title": f"{home} vs {opp}",
        "when": when,
        "arena": arena,
        "available": bool(ev.get("hasPublicOffersAvailable")),
        "url": f"{BASE_URL}/fr/evenement/{ev['id']}",
    }


# --- Telegram ----------------------------------------------------------------

def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram non configuré (secrets manquants).")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": False},
            timeout=20,
        )
    except Exception as e:
        print(f"[WARN] Envoi Telegram échoué: {e}")


# --- State -------------------------------------------------------------------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"events": {}, "consecutive_failures": 0}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# --- Main --------------------------------------------------------------------

def main() -> int:
    state = load_state()
    known = state.get("events", {})  # {str(id): {"available": bool, "title": ...}}

    try:
        html = fetch_html(LIST_URL)
        raw_events = extract_events(html)
        if not raw_events:
            raise RuntimeError(
                "Aucun évènement extrait. Le format de la page a peut-être changé, "
                "ou la requête a été bloquée (anti-bot). Voir le log."
            )
    except Exception as e:
        failures = state.get("consecutive_failures", 0) + 1
        state["consecutive_failures"] = failures
        save_state(state)
        print(f"[ERREUR] {e}  (échec #{failures})")
        if failures % 10 == 0:
            send_telegram(
                f"⚠️ Monitor billetterie RHE76 : {failures} échecs d'affilée.\n"
                "Va voir GitHub Actions pour le détail du log."
            )
        return 1

    # Succès -> on remet le compteur d'échecs à zéro
    state["consecutive_failures"] = 0

    for ev_id, ev in raw_events.items():
        e = format_event(ev)
        key = str(ev_id)
        old = known.get(key)

        if old is None:
            # Nouveau match découvert
            statut = "OUVERTE ✅" if e["available"] else "pas encore ouverte ⏳"
            send_telegram(
                "🏒 Nouveau match détecté !\n"
                f"🆚 {e['title']}\n"
                f"📅 {e['when']}\n"
                f"📍 {e['arena']}\n"
                f"🎟️ Billetterie : {statut}\n"
                f"🔗 {e['url']}"
            )
        elif old.get("available") != e["available"]:
            # Changement d'état de la billetterie
            if e["available"]:
                send_telegram(
                    "✅ Billetterie OUVERTE !\n"
                    f"🆚 {e['title']}\n"
                    f"📅 {e['when']}\n"
                    f"🔗 {e['url']}"
                )
            else:
                send_telegram(
                    "⛔ Billetterie fermée / épuisée\n"
                    f"🆚 {e['title']}\n"
                    f"📅 {e['when']}\n"
                    f"🔗 {e['url']}"
                )

        known[key] = {"available": e["available"], "title": e["title"], "when": e["when"]}

    state["events"] = known
    save_state(state)
    print(f"OK - {len(raw_events)} évènement(s) vérifié(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

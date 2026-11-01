
#!/usr/bin/env python3
"""Surveillance de la billetterie Rouen Hockey Elite 76 (Next.js SSR)."""

import html as html_lib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# --- Configuration -----------------------------------------------------------

BASE_URL = "https://billetterie.rouenhockeyelite76.com"
LIST_URL = BASE_URL + "/fr"
STATE_FILE = Path("state.json")

HEALTHCHECK_URL = os.environ.get(
    "HEALTHCHECK_URL",
    "https://hc-ping.com/65ae6222-4000-4f9b-8fd9-22b2a92c2980",
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

PARIS = ZoneInfo("Europe/Paris")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

EMPTY_MESSAGE_RE = re.compile(
    r"aucun\s+[ée]v[ée]nement\s+n['’]est\s+disponible\s+pour\s+le\s+moment",
    re.IGNORECASE,
)

NEXT_CHUNK_RE = re.compile(
    r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)',
    re.DOTALL,
)


# --- Récupération et diagnostic ---------------------------------------------

def fetch_html(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=30)

    print(f"[DEBUG] HTTP : {response.status_code}", flush=True)
    print(f"[DEBUG] URL finale : {response.url}", flush=True)
    print(f"[DEBUG] Taille HTML : {len(response.content)} octets", flush=True)
    print(
        f"[DEBUG] Content-Type : {response.headers.get('Content-Type')}",
        flush=True,
    )

    response.raise_for_status()

    page = response.text

    print(f"[DEBUG] Début HTML : {page[:1000]!r}", flush=True)
    print(
        f"[DEBUG] Présence Next.js : {'self.__next_f.push' in page}",
        flush=True,
    )
    print(
        f"[DEBUG] Présence événements : {'hasPublicOffersAvailable' in page}",
        flush=True,
    )
    print(
        f"[DEBUG] Présence 'Aucun' : {'aucun' in page.lower()}",
        flush=True,
    )

    return page


# --- Extraction des événements ----------------------------------------------

def _balance_braces(text: str, start: int):
    depth = 0
    in_str = False
    escaped = False

    for j in range(start, len(text)):
        char = text[j]

        if in_str:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_str = False
            continue

        if char == '"':
            in_str = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:j + 1], j + 1

    return None, len(text)


def _find_events_in_chunk(text: str):
    events = []
    marker = '"event":{'
    idx = 0

    while True:
        i = text.find(marker, idx)
        if i < 0:
            break

        obj, end = _balance_braces(text, i + len('"event":'))
        idx = max(end, i + len(marker))

        if not obj:
            continue

        try:
            event = json.loads(obj)
        except json.JSONDecodeError:
            continue

        if "id" in event and "hasPublicOffersAvailable" in event:
            events.append(event)

    return events


def extract_events(page: str) -> dict:
    events = {}
    chunks = list(NEXT_CHUNK_RE.finditer(page))

    print(f"[DEBUG] Fragments Next.js trouvés : {len(chunks)}", flush=True)

    for match in chunks:
        try:
            chunk = json.loads('"' + match.group(1) + '"')
        except json.JSONDecodeError:
            continue

        if '"event":{' not in chunk:
            continue

        for event in _find_events_in_chunk(chunk):
            events[str(event["id"])] = event

    print(f"[DEBUG] Événements extraits : {len(events)}", flush=True)

    return events


# --- Détection de billetterie vide -------------------------------------------

def is_confirmed_empty(page: str) -> bool:
    if EMPTY_MESSAGE_RE.search(html_lib.unescape(page)):
        print("[DEBUG] Message billetterie vide trouvé dans le HTML.")
        return True

    for match in NEXT_CHUNK_RE.finditer(page):
        try:
            chunk = json.loads('"' + match.group(1) + '"')
        except json.JSONDecodeError:
            continue

        if EMPTY_MESSAGE_RE.search(html_lib.unescape(chunk)):
            print("[DEBUG] Message billetterie vide trouvé dans Next.js.")
            return True

    print("[DEBUG] Message officiel de billetterie vide NON trouvé.")
    return False


# --- Formatage ---------------------------------------------------------------

def format_event(event: dict) -> dict:
    home = (event.get("homeTeam") or {}).get("displayName", "?")
    opponent = (event.get("opponent") or {}).get("displayName", "?")
    arena = (event.get("arena") or {}).get("name", "")

    when = ""
    raw_datetime = event.get("startDatetime")

    if raw_datetime:
        try:
            parsed = datetime.fromisoformat(
                raw_datetime.replace("Z", "+00:00")
            )
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(PARIS)
            when = parsed.strftime("%d/%m/%Y %Hh%M")
        except (ValueError, TypeError):
            when = str(raw_datetime)

    return {
        "id": str(event["id"]),
        "title": f"{home} vs {opponent}",
        "when": when,
        "arena": arena,
        "available": bool(event["hasPublicOffersAvailable"]),
        "url": f"{BASE_URL}/fr/evenement/{event['id']}",
    }


# --- Telegram ---------------------------------------------------------------

def send_telegram(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram non configuré (secrets manquants).")
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": False,
            },
            timeout=20,
        )
        response.raise_for_status()

        if not response.json().get("ok"):
            raise RuntimeError("Telegram a répondu ok=false")

        return True

    except (requests.RequestException, ValueError, RuntimeError) as error:
        print(f"[WARN] Envoi Telegram échoué: {error}")
        return False


# --- Healthchecks -----------------------------------------------------------

def ping_healthcheck() -> None:
    if not HEALTHCHECK_URL:
        return

    try:
        response = requests.get(HEALTHCHECK_URL, timeout=10)
        response.raise_for_status()
        print("[OK] Ping Healthchecks envoyé.")

    except requests.RequestException as error:
        print(f"[WARN] Ping Healthchecks échoué: {error}")


# --- Gestion de l'état -------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            state = json.loads(
                STATE_FILE.read_text(encoding="utf-8")
            )

            if isinstance(state, dict) and isinstance(
                state.get("events", {}), dict
            ):
                state.setdefault("events", {})
                state.setdefault("consecutive_failures", 0)
                return state

        except (OSError, ValueError):
            pass

    return {
        "events": {},
        "consecutive_failures": 0,
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# --- Programme principal -----------------------------------------------------

def main() -> int:
    state = load_state()
    known = state["events"]
    previous_mode = state.get("listing_mode")

    try:
        page = fetch_html(LIST_URL)
        raw_events = extract_events(page)

        if raw_events:
            mode = "EVENTS"

        elif is_confirmed_empty(page):
            mode = "EMPTY"

        else:
            raise RuntimeError(
                "Aucun événement extrait et message officiel "
                "de billetterie vide absent. "
                "Possible changement de format ou blocage anti-bot."
            )

    except Exception as error:
        failures = int(
            state.get("consecutive_failures", 0)
        ) + 1

        state["consecutive_failures"] = failures
        save_state(state)

        print(
            f"[ERREUR] {error} (échec #{failures})",
            flush=True,
        )

        # Aucun ping Healthchecks en cas d'échec.
        return 1

    # Run réussi
    state["consecutive_failures"] = 0

    # --- Billetterie vide ----------------------------------------------------

    if mode == "EMPTY":
        if previous_mode != "EMPTY":
            send_telegram(
                "💤 Aucun match actuellement en billetterie.\n"
                "La billetterie officielle des Dragons ne propose "
                "aucun événement pour le moment.\n"
                "🔎 Surveillance toujours active.\n"
                f"🔗 {LIST_URL}"
            )

        state["listing_mode"] = "EMPTY"
        save_state(state)

        print(
            "[OK] Billetterie vide confirmée ; surveillance active.",
            flush=True,
        )

        ping_healthcheck()
        return 0

    # --- Retour des événements ----------------------------------------------

    if previous_mode == "EMPTY":
        send_telegram(
            "🏒 La billetterie des Dragons est de retour !\n"
            f"📅 {len(raw_events)} événement(s) affiché(s).\n"
            "🔎 Surveillance des disponibilités active.\n"
            f"🔗 {LIST_URL}"
        )

    # --- Surveillance des matchs --------------------------------------------

    for event_id, raw_event in raw_events.items():
        event = format_event(raw_event)
        old = known.get(event_id)

        if old is None:
            status = (
                "OUVERTE ✅"
                if event["available"]
                else "pas encore ouverte ⏳"
            )

            send_telegram(
                "🏒 Nouveau match détecté !\n"
                f"🆚 {event['title']}\n"
                f"📅 {event['when']}\n"
                f"📍 {event['arena']}\n"
                f"🎟️ Billetterie : {status}\n"
                f"🔗 {event['url']}"
            )

        elif old.get("available") != event["available"]:
            if event["available"]:
                send_telegram(
                    "✅ Billetterie OUVERTE !\n"
                    f"🆚 {event['title']}\n"
                    f"📅 {event['when']}\n"
                    f"🔗 {event['url']}"
                )
            else:
                send_telegram(
                    "⛔ Billetterie fermée / épuisée\n"
                    f"🆚 {event['title']}\n"
                    f"📅 {event['when']}\n"
                    f"🔗 {event['url']}"
                )

        known[event_id] = {
            "available": event["available"],
            "title": event["title"],
            "when": event["when"],
        }

    state["listing_mode"] = "EVENTS"
    state["events"] = known

    save_state(state)

    print(
        f"[OK] {len(raw_events)} événement(s) vérifié(s).",
        flush=True,
    )

    ping_healthcheck()
    return 0


if __name__ == "__main__":
    sys.exit(main())

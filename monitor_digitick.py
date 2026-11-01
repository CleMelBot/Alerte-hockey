
#!/usr/bin/env python3
"""Surveillance automatique de la billetterie des Dragons de Rouen."""

import html as html_lib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


# ============================================================
# CONFIGURATION
# ============================================================

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

NEXT_CHUNK_RE = re.compile(
    r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)',
    re.DOTALL,
)


# ============================================================
# RÉCUPÉRATION HTML
# ============================================================

def fetch_html(url):
    response = requests.get(url, headers=HEADERS, timeout=30)

    print(f"[DEBUG] HTTP : {response.status_code}", flush=True)
    print(f"[DEBUG] URL finale : {response.url}", flush=True)
    print(
        f"[DEBUG] Taille HTML : {len(response.content)} octets",
        flush=True,
    )
    print(
        f"[DEBUG] Content-Type : "
        f"{response.headers.get('Content-Type')}",
        flush=True,
    )

    response.raise_for_status()

    page = response.text

    print(
        f"[DEBUG] Présence Next.js : "
        f"{'self.__next_f.push' in page}",
        flush=True,
    )
    print(
        f"[DEBUG] Présence événements : "
        f"{'hasPublicOffersAvailable' in page}",
        flush=True,
    )
    print(
        f"[DEBUG] Présence 'aucun' : "
        f"{'aucun' in page.lower()}",
        flush=True,
    )

    return page


# ============================================================
# OUTILS DE DÉCODAGE
# ============================================================

def normalize_text(text):
    """Normalise les accents, espaces et apostrophes."""

    text = html_lib.unescape(text)

    text = text.replace("\\u0027", "'")
    text = text.replace("\\u2019", "'")
    text = text.replace("\\u00e9", "é")
    text = text.replace("\\u00e8", "è")
    text = text.replace("\\n", " ")
    text = text.replace("\\t", " ")

    text = re.sub(r"<[^>]+>", " ", text)

    text = unicodedata.normalize("NFKD", text)

    text = "".join(
        character
        for character in text
        if not unicodedata.combining(character)
    )

    text = text.lower()
    text = text.replace("’", "'")
    text = " ".join(text.split())

    return text


def next_chunks(page):
    """Décode les fragments JSON du flux Next.js."""

    for match in NEXT_CHUNK_RE.finditer(page):
        try:
            yield json.loads('"' + match.group(1) + '"')
        except json.JSONDecodeError:
            continue


# ============================================================
# EXTRACTION DES MATCHS
# ============================================================

def balance_braces(text, start):
    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1

            if depth == 0:
                return text[start:index + 1], index + 1

    return None, len(text)


def find_events_in_chunk(text):
    events = []
    marker = '"event":{'
    position = 0

    while True:
        index = text.find(marker, position)

        if index < 0:
            break

        obj, end = balance_braces(
            text,
            index + len('"event":'),
        )

        position = max(end, index + len(marker))

        if not obj:
            continue

        try:
            event = json.loads(obj)
        except json.JSONDecodeError:
            continue

        if (
            "id" in event
            and "hasPublicOffersAvailable" in event
        ):
            events.append(event)

    return events


def extract_events(page):
    events = {}
    chunks = list(next_chunks(page))

    print(
        f"[DEBUG] Fragments Next.js décodés : {len(chunks)}",
        flush=True,
    )

    for chunk in chunks:
        if '"event":{' not in chunk:
            continue

        for event in find_events_in_chunk(chunk):
            events[str(event["id"])] = event

    print(
        f"[DEBUG] Événements extraits : {len(events)}",
        flush=True,
    )

    return events


# ============================================================
# DÉTECTION BILLETTERIE VIDE
# ============================================================

def is_confirmed_empty(page):
    """
    Reconnaît le message officiel de billetterie vide,
    même avec des accents ou apostrophes encodés.
    """

    candidates = [page]
    candidates.extend(next_chunks(page))

    for candidate in candidates:
        text = normalize_text(candidate)

        patterns = [
            r"aucun evenement n'est disponible",
            r"aucun evenement disponible pour le moment",
            r"aucun evenement n'est disponible pour le moment",
        ]

        for pattern in patterns:
            if re.search(pattern, text):
                print(
                    "[OK] Billetterie vide officiellement confirmée.",
                    flush=True,
                )
                return True

    # Diagnostic ciblé : afficher le contexte du mot "aucun".
    for candidate in candidates:
        text = normalize_text(candidate)
        position = text.find("aucun")

        if position >= 0:
            start = max(0, position - 100)
            end = min(len(text), position + 200)

            print(
                "[DEBUG] Contexte autour de 'aucun' : "
                + repr(text[start:end]),
                flush=True,
            )
            break

    print(
        "[DEBUG] Message officiel de billetterie vide non reconnu.",
        flush=True,
    )

    return False


# ============================================================
# FORMATAGE DES MATCHS
# ============================================================

def format_event(event):
    home = (event.get("homeTeam") or {}).get(
        "displayName", "?"
    )
    opponent = (event.get("opponent") or {}).get(
        "displayName", "?"
    )
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
        "available": bool(
            event["hasPublicOffersAvailable"]
        ),
        "url": (
            f"{BASE_URL}/fr/evenement/{event['id']}"
        ),
    }


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(
            "[WARN] Telegram non configuré.",
            flush=True,
        )
        return False

    try:
        response = requests.post(
            (
                "https://api.telegram.org/bot"
                f"{TELEGRAM_TOKEN}/sendMessage"
            ),
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": False,
            },
            timeout=20,
        )

        response.raise_for_status()

        if not response.json().get("ok"):
            raise RuntimeError(
                "Telegram a répondu ok=false"
            )

        return True

    except (
        requests.RequestException,
        ValueError,
        RuntimeError,
    ) as error:
        print(
            f"[WARN] Telegram : {error}",
            flush=True,
        )
        return False


# ============================================================
# HEALTHCHECKS
# ============================================================

def ping_healthcheck():
    if not HEALTHCHECK_URL:
        return

    try:
        response = requests.get(
            HEALTHCHECK_URL,
            timeout=10,
        )

        response.raise_for_status()

        print(
            "[OK] Ping Healthchecks envoyé.",
            flush=True,
        )

    except requests.RequestException as error:
        print(
            f"[WARN] Healthchecks : {error}",
            flush=True,
        )


# ============================================================
# MÉMOIRE
# ============================================================

def load_state():
    if STATE_FILE.exists():
        try:
            state = json.loads(
                STATE_FILE.read_text(
                    encoding="utf-8"
                )
            )

            if (
                isinstance(state, dict)
                and isinstance(
                    state.get("events", {}),
                    dict,
                )
            ):
                state.setdefault("events", {})
                state.setdefault(
                    "consecutive_failures", 0
                )
                return state

        except (OSError, ValueError):
            pass

    return {
        "events": {},
        "consecutive_failures": 0,
    }


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================

def main():
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
                "Aucun événement extrait et "
                "billetterie vide non confirmée. "
                "Vérifier les logs DEBUG."
            )

    except Exception as error:
        failures = (
            int(
                state.get(
                    "consecutive_failures", 0
                )
            ) + 1
        )

        state["consecutive_failures"] = failures
        save_state(state)

        print(
            f"[ERREUR] {error} "
            f"(échec #{failures})",
            flush=True,
        )

        # Pas de ping Healthchecks en cas d'erreur.
        return 1

    # Exécution réussie.
    state["consecutive_failures"] = 0

    # --------------------------------------------------------
    # AUCUN MATCH
    # --------------------------------------------------------

    if mode == "EMPTY":
        if previous_mode != "EMPTY":
            send_telegram(
                "💤 Aucun match actuellement "
                "en billetterie.\n"
                "La billetterie officielle "
                "des Dragons ne propose "
                "aucun événement pour le moment.\n"
                "🔎 Surveillance toujours active.\n"
                f"🔗 {LIST_URL}"
            )

        state["listing_mode"] = "EMPTY"
        save_state(state)

        print(
            "[OK] Billetterie vide. "
            "Surveillance active.",
            flush=True,
        )

        ping_healthcheck()
        return 0

    # --------------------------------------------------------
    # RETOUR DES MATCHS
    # --------------------------------------------------------

    if previous_mode == "EMPTY":
        send_telegram(
            "🏒 La billetterie des Dragons "
            "est de retour !\n"
            f"📅 {len(raw_events)} "
            "événement(s) affiché(s).\n"
            "🔎 Surveillance active.\n"
            f"🔗 {LIST_URL}"
        )

    # --------------------------------------------------------
    # SURVEILLANCE DES MATCHS
    # --------------------------------------------------------

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

        elif (
            old.get("available")
            != event["available"]
        ):
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
        f"[OK] {len(raw_events)} "
        "événement(s) vérifié(s).",
        flush=True,
    )

    ping_healthcheck()

    return 0


if __name__ == "__main__":
    sys.exit(main())

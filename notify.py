"""
Tippbot Schritt 4: Telegram.

python notify.py --chat-id   zeigt deine Chat-ID (vorher dem Bot eine Nachricht schreiben)
python notify.py --test      sendet eine Testnachricht

Umgebungsvariablen: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
import os
import sys

import requests

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000  # Telegram-Limit ist 4096 Zeichen pro Nachricht


def _split(text):
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > MAX_LEN:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


def api(method, **params):
    """Telegram-API-Aufruf. Gibt das Ergebnis zurueck oder None bei Fehler."""
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        return None
    try:
        r = requests.post(API.format(token=token, method=method), json=params, timeout=40)
    except requests.RequestException as e:
        print("Telegram: Verbindungsfehler", type(e).__name__)
        return None
    if not r.ok:
        print("Telegram-Fehler", method, r.status_code, r.json().get("description", ""))
        return None
    return r.json().get("result")


def send(text, chat_id=None):
    """Sendet Text an Telegram. Gibt True bei Erfolg zurueck."""
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram nicht konfiguriert (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID fehlen).")
        return False
    for chunk in _split(text):
        try:
            r = requests.post(API.format(token=token, method="sendMessage"),
                              data={"chat_id": chat_id, "text": chunk}, timeout=30)
        except requests.RequestException as e:
            # Fehlertext nicht ausgeben: er koennte den Token in der URL enthalten
            print("Telegram: Verbindungsfehler", type(e).__name__)
            return False
        if not r.ok:
            print("Telegram-Fehler", r.status_code, r.json().get("description", ""))
            return False
    return True


def find_chat_id():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_TOKEN ist nicht gesetzt.")
    try:
        r = requests.get(API.format(token=token, method="getUpdates"), timeout=30)
    except requests.RequestException as e:
        raise SystemExit(f"Verbindungsfehler: {type(e).__name__}")
    if not r.ok:
        raise SystemExit(f"Telegram-Fehler {r.status_code}: {r.json().get('description', '')}")
    seen = {}
    for upd in r.json().get("result", []):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat")
        if chat:
            seen[chat["id"]] = chat.get("first_name") or chat.get("title") or "?"
    if not seen:
        print("Keine Nachrichten gefunden. Schreibe deinem Bot in Telegram eine Nachricht "
              "(z.B. 'hallo') und starte den Befehl nochmals.")
    for cid, name in seen.items():
        print(f"Chat-ID: {cid}   (Name: {name})")


if __name__ == "__main__":
    if "--chat-id" in sys.argv:
        find_chat_id()
    elif "--test" in sys.argv:
        ok = send("Tippbot: Testnachricht. Telegram funktioniert!")
        print("Gesendet." if ok else "Senden fehlgeschlagen.")
    else:
        print(__doc__)

# Tippbot: commands.py
"""
Telegram-Befehle beantworten: /stand, /heute, /morgen, /multi, /dashboard, /hilfe

Laeuft alle 10 Minuten im Workflow telegram.yml (liest die Datenbank nur, aendert nichts).
Lokal:  python commands.py
Antwortet nur im Chat TELEGRAM_CHAT_ID, Nachrichten aus anderen Chats werden ignoriert.
"""
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import bot
import calib
import multi
import notify
import paper

COMMANDS = [
    ("stand", "Kontostand und Trefferquoten"),
    ("heute", "Tipps für heute"),
    ("morgen", "Tipps für morgen"),
    ("multi", "Stand des Multiplikators"),
    ("dashboard", "Link zum Dashboard"),
    ("hilfe", "Alle Befehle"),
]
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def fr(v):
    return f"{v:,.2f}".replace(",", "'")


def local(iso):
    return bot.parse_date(iso).astimezone(paper.LOCAL_TZ)


def dashboard_url():
    repo = os.environ.get("GITHUB_REPOSITORY", "joellanini26-arch/tippbot")
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/"


# ---------- Antworten ----------
def cmd_hilfe(con):
    return "🤖 Tippbot-Befehle:\n" + "\n".join(f"/{c} – {d}" for c, d in COMMANDS) + \
        "\n\nAntworten kommen innerhalb von etwa 10 Minuten."


def cmd_stand(con):
    acc = paper.account(con)
    lines = [f"💰 Kontostand: {fr(acc['balance'])} CHF ({acc['total_profit']:+.2f} CHF)",
             f"Treffer {acc['won']} · Nieten {acc['lost']} · offen {acc['open']} "
             f"({fr(acc['open_stake'])} CHF Einsatz)", ""]
    label = {"1X2": "Tipp 1X2", "OU": "Over/Under", "SCORE": "Resultat"}
    for kind, f in calib.factors(con).items():
        if f["n"]:
            lines.append(f"{label[kind]}: {f['rate']:.0%} getroffen, Modell erwartete {f['pred']:.0%} "
                         f"({f['n']} Wetten, Korrektur ×{f['factor']:.2f})")
        else:
            lines.append(f"{label[kind]}: noch keine abgerechneten Wetten")
    won, lost, profit = paper.botd_record(con)
    lines.append(f"Bet of the day: {won} Treffer, {lost} Nieten, {profit:+.2f} CHF")
    return "\n".join(lines)


def tips_for_day(con, day_offset):
    now = datetime.now(timezone.utc)
    day = (now.astimezone(paper.LOCAL_TZ) + timedelta(days=day_offset)).date()
    title = "heute" if day_offset == 0 else "morgen"
    games = {}
    for mid, utc, league, home, away, status in con.execute(
            "SELECT id, utc_date, league, home, away, status FROM matches "
            "WHERE utc_date >= ? AND utc_date <= ? ORDER BY utc_date",
            ((now - timedelta(days=2)).strftime("%Y-%m-%d"), (now + timedelta(days=3)).strftime("%Y-%m-%dT23:59:59Z"))):
        if league in bot.LEAGUES and local(utc).date() == day:
            games[mid] = {"utc": utc, "league": league, "home": home, "away": away, "status": status, "bets": []}
    if not games:
        return f"📅 Für {title} sind keine Spiele in den Daten."
    for mid, kind, pick, stake, odds, status in con.execute(
            "SELECT match_id, kind, pick, stake, odds, status FROM bets"):
        if mid in games:
            games[mid]["bets"].append((kind, pick, stake, odds, status))
    mark = {"OPEN": "", "WON": " ✅", "LOST": " ❌", "VOID": " ↩️"}
    label = {"1X2": "Tipp", "SCORE": "Resultat", "OU": ""}
    lines = [f"📅 Tipps für {title} ({WEEKDAYS[day.weekday()]} {day.strftime('%d.%m.')}):"]
    skipped = 0
    for g in games.values():
        if not g["bets"]:
            skipped += 1
            continue
        t = "?" if g["utc"].endswith("T00:00:00Z") else local(g["utc"]).strftime("%H:%M")
        lines.append(f"\n{t} · {bot.LEAGUES[g['league']]}\n{g['home']} – {g['away']}")
        for kind, pick, stake, odds, status in sorted(g["bets"]):
            what = f"{label[kind]} {pick}".strip()
            lines.append(f"   {what} @{odds:.2f} · {stake:g} CHF{mark.get(status, '')}")
    if skipped:
        lines.append(f"\n{skipped} weitere Spiele ohne Tipp (zu unsicher).")
    if len(lines) == 1:
        lines.append("Der Bot tippt bei keinem dieser Spiele (zu unsicher) oder die Tipps folgen noch.")
    return "\n".join(lines)


def cmd_heute(con):
    return tips_for_day(con, 0)


def cmd_morgen(con):
    return tips_for_day(con, 1)


def cmd_multi(con):
    d = multi.data(con)
    if not d["runs"]:
        return "Noch kein Multiplikator-Versuch."
    r = next((x for x in d["runs"] if x["status"] == "ACTIVE"), d["runs"][0])
    status = {"ACTIVE": "läuft", "REACHED": "Ziel erreicht 🏆", "LOST": "verloren",
              "EXPIRED": "Zeit abgelaufen", "CANCELLED": "abgebrochen"}[r["status"]]
    total = r["pot"] + r["savings"]
    lines = [f"🚀 Multiplikator #{r['id']} · {status}",
             f"Total {fr(total)} Fr. von {fr(r['target'])} Fr. (×{total / r['start']:.1f})",
             f"Topf {fr(r['pot'])} Fr. · Sparkonto {fr(r['savings'])} Fr.",
             f"Treffer {r['wins']} · bis Sparrunde {r['streak']}/{multi.SAVE_EVERY}"]
    if r["status"] == "ACTIVE":
        left = bot.parse_date(r["ends_at"]) - datetime.now(timezone.utc)
        lines.append(f"Noch {max(0, left.days)} Tage · etwa {r['need']} Treffer in Folge nötig")
    open_bet = next((b for b in r["bets"] if b["status"] == "OPEN"), None)
    if open_bet:
        lines.append(f"\nOffene Kombi @{open_bet['odds']:.2f}, Einsatz {fr(open_bet['stake'])} Fr.:")
        icon = {"WON": "✅", "LOST": "❌", "VOID": "↩️", "OPEN": "⏳"}
        for l in open_bet["legs"]:
            lines.append(f"{icon[l['state']]} {local(l['utc']).strftime('%d.%m. %H:%M')} {l['home']} – {l['away']}: "
                         f"{paper.pick_label(l['pick'])}")
    elif r["status"] == "ACTIVE":
        lines.append("\nGerade keine Kombi offen, der Bot sucht stündlich nach einer passenden.")
    return "\n".join(lines)


def cmd_dashboard(con):
    return f"📊 Dashboard: {dashboard_url()}"


HANDLERS = {"stand": cmd_stand, "heute": cmd_heute, "morgen": cmd_morgen, "multi": cmd_multi,
            "dashboard": cmd_dashboard, "hilfe": cmd_hilfe, "help": cmd_hilfe, "start": cmd_hilfe}


def answer(con, text):
    word = (text or "").strip().split()[0].lower() if (text or "").strip() else ""
    name = word.lstrip("/").split("@")[0]
    handler = HANDLERS.get(name)
    if not handler:
        return None if not word.startswith("/") else "Unbekannter Befehl. " + cmd_hilfe(con)
    return handler(con)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not os.environ.get("TELEGRAM_TOKEN") or not chat_id:
        print("Telegram nicht konfiguriert.")
        return
    notify.api("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
    updates = notify.api("getUpdates", timeout=0, allowed_updates=["message"]) or []
    if not updates:
        print("Keine neuen Nachrichten.")
        return
    con = sqlite3.connect(bot.DB_PATH)
    bot.init_db(con)
    paper.init_bets(con)
    multi.init(con)
    for upd in updates:
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(chat_id):
            continue                       # fremder Chat: ignorieren
        try:
            reply = answer(con, msg.get("text"))
        except Exception as e:             # ein fehlerhafter Befehl soll die anderen nicht blockieren
            reply = f"Fehler bei diesem Befehl ({type(e).__name__})."
        if reply:
            notify.send(reply)
            print("Beantwortet:", (msg.get("text") or "")[:30])
    # Verarbeitete Nachrichten bei Telegram bestaetigen, damit sie nicht nochmals kommen
    notify.api("getUpdates", offset=updates[-1]["update_id"] + 1, timeout=0)
    con.close()


if __name__ == "__main__":
    main()

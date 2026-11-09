"""
Tippbot Schritt 3: Fake-Wetten mit virtuellem Geld.

Start:   python paper.py            (holt Daten, rechnet ab, platziert neue Wetten)
         python paper.py --days 2   (Wetten nur fuer die naechsten 2 Tage)
         python paper.py --no-fetch (ohne neuen API-Abruf)
         python paper.py --no-telegram (nichts senden, nur anzeigen)
Liegt im selben Ordner wie bot.py.
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timezone

import bot
import notify

# ---------- Einstellungen ----------
START_BALANCE = 1000.0   # virtuelles Startguthaben in CHF
STAKE_1X2 = 10.0         # Einsatz auf den 1/X/2-Tipp
STAKE_SCORE = 2.0        # Einsatz auf den Resultat-Tipp
STAKE_OU = 5.0           # Einsatz auf den Over/Under-Tipp
OU_LINE = 2.5            # Over/Under-Grenze (moeglich: 1.5, 2.5, 3.5)
MARGIN = 0.10            # Buchmacher-Marge auf die Modell-Quoten


KIND_LABEL = {"1X2": "Tipp 1X2", "SCORE": "Resultat", "OU": "Over/Under"}


def init_bets(con):
    con.execute("""
    CREATE TABLE IF NOT EXISTS bets (
        match_id INTEGER, kind TEXT, pick TEXT,
        stake REAL, odds REAL, placed_at TEXT,
        status TEXT DEFAULT 'OPEN',
        profit REAL, settled_at TEXT,
        PRIMARY KEY (match_id, kind)
    )""")
    con.commit()


# ---------- Wetten platzieren ----------
def make_odds(prob):
    """Faire Quote minus Marge. Platzhalter, spaeter durch echte Quoten ersetzbar."""
    return max(1.01, round((1 - MARGIN) / prob, 2))


def place_bets(con, days):
    now = datetime.now(timezone.utc)
    placed = []
    for code, name in bot.LEAGUES.items():
        try:
            model = bot.fit_model(con, code)
        except bot.NotEnoughData:
            print(f"{name}: zu wenig Daten, keine Wetten.")
            continue
        rows = con.execute(
            "SELECT id, utc_date, home, away FROM matches "
            "WHERE league=? AND status IN ('SCHEDULED','TIMED') ORDER BY utc_date",
            (code,)).fetchall()
        for mid, d, home, away in rows:
            secs = (bot.parse_date(d) - now).total_seconds()
            if not (0 < secs <= days * 86400):
                continue
            if home not in model["att"] or away not in model["att"]:
                continue
            p = bot.predict(model, home, away)
            probs = {"1": p["p_home"], "X": p["p_draw"], "2": p["p_away"]}
            p_over = p[f"p_o{int(OU_LINE * 10)}"]
            ou_pick, ou_prob = (("Over", p_over) if p_over >= 0.5 else ("Under", 1 - p_over))
            bets = [
                ("1X2", p["tip_1x2"], STAKE_1X2, make_odds(probs[p["tip_1x2"]])),
                ("SCORE", p["tip_score"], STAKE_SCORE, make_odds(p["score_prob"])),
                ("OU", f"{ou_pick} {OU_LINE}", STAKE_OU, make_odds(ou_prob)),
            ]
            for kind, pick, stake, odds in bets:
                cur = con.execute(
                    "INSERT OR IGNORE INTO bets (match_id, kind, pick, stake, odds, placed_at) "
                    "VALUES (?,?,?,?,?,?)", (mid, kind, pick, stake, odds, now.isoformat()))
                if cur.rowcount:
                    placed.append((name, home, away, kind, pick, stake, odds))
    con.commit()
    return placed


# ---------- Abrechnung ----------
def settle_bets(con):
    now = datetime.now(timezone.utc).isoformat()
    rows = con.execute("""
        SELECT b.match_id, b.kind, b.pick, b.stake, b.odds,
               m.home, m.away, m.home_goals, m.away_goals, m.status
        FROM bets b JOIN matches m ON m.id = b.match_id
        WHERE b.status = 'OPEN'
          AND (m.status IN ('POSTPONED','CANCELLED')
               OR (m.status = 'FINISHED' AND m.home_goals IS NOT NULL))
    """).fetchall()
    results = []
    for mid, kind, pick, stake, odds, home, away, hg, ag, mstatus in rows:
        if mstatus in ("POSTPONED", "CANCELLED"):
            status, profit, actual = "VOID", 0.0, "-"
        else:
            if kind == "SCORE":
                actual = f"{hg}:{ag}"
                won = (pick == actual)
            elif kind == "OU":
                side, line = pick.split()
                actual = "Over" if hg + ag > float(line) else "Under"
                won = (side == actual)
            else:
                actual = "1" if hg > ag else "X" if hg == ag else "2"
                won = (pick == actual)
            status = "WON" if won else "LOST"
            profit = round(stake * (odds - 1), 2) if won else -stake
        con.execute("UPDATE bets SET status=?, profit=?, settled_at=? WHERE match_id=? AND kind=?",
                    (status, profit, now, mid, kind))
        results.append({"home": home, "away": away, "kind": kind, "pick": pick,
                        "actual": (f"{hg}:{ag}" if hg is not None else "-"),
                        "stake": stake, "odds": odds, "status": status, "profit": profit})
    con.commit()
    return results


def account(con):
    total = con.execute("SELECT COALESCE(SUM(profit),0) FROM bets WHERE status!='OPEN'").fetchone()[0]
    n_open, open_stake = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(stake),0) FROM bets WHERE status='OPEN'").fetchone()
    won, lost = (con.execute("SELECT COUNT(*) FROM bets WHERE status=?", (s,)).fetchone()[0]
                 for s in ("WON", "LOST"))
    return {"balance": START_BALANCE + total, "total_profit": total,
            "open": n_open, "open_stake": open_stake, "won": won, "lost": lost}


# ---------- Bericht ----------
def build_report(results, acc, emoji=False):
    """Text-Bericht (wird spaeter auch per Telegram gesendet)."""
    lines = []
    if results:
        lines.append("Abgerechnete Wetten:")
        for r in results:
            marks = ({"WON": "\u2705", "LOST": "\u274c", "VOID": "\u21a9\ufe0f"} if emoji
                     else {"WON": "[OK]", "LOST": "[--]", "VOID": "[ - ]"})
            mark = marks[r["status"]]
            what = KIND_LABEL[r["kind"]]
            lines.append(f"{mark} {r['home']} - {r['away']}: {what} {r['pick']} "
                         f"@{r['odds']:.2f}, Ergebnis {r['actual']}, {r['profit']:+.2f} CHF")
        day = sum(r["profit"] for r in results)
        lines.append(f"\nTotal G/V (neu abgerechnet): {day:+.2f} CHF")
    else:
        lines.append("Keine neuen Wetten abgerechnet.")
    lines.append(f"Kontostand: {acc['balance']:.2f} CHF "
                 f"(gesamt {acc['total_profit']:+.2f} CHF, Treffer {acc['won']}, Nieten {acc['lost']})")
    lines.append(f"Offen: {acc['open']} Wetten, {acc['open_stake']:.2f} CHF Einsatz")
    return "\n".join(lines)


def build_tips_text(placed, emoji=False):
    if not placed:
        return "Keine neuen Wetten platziert."
    title = "\u26bd Neue Tipps:" if emoji else "Neue Tipps:"
    by_league = {}
    for league, home, away, kind, pick, stake, odds in placed:
        by_league.setdefault(league, {}).setdefault((home, away), {})[kind] = (pick, odds)
    lines = [title]
    for league, games in by_league.items():
        lines.append(f"\n{league}:")
        for (home, away), b in games.items():
            parts = []
            if "1X2" in b:
                parts.append(f"Tipp {b['1X2'][0]} (@{b['1X2'][1]:.2f})")
            if "SCORE" in b:
                parts.append(f"Resultat {b['SCORE'][0]} (@{b['SCORE'][1]:.2f})")
            if "OU" in b:
                parts.append(f"{b['OU'][0]} (@{b['OU'][1]:.2f})")
            lines.append(f"{home} - {away}: " + ", ".join(parts))
    return "\n".join(lines)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")   # Sonderzeichen in Teamnamen sicher ausgeben
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--no-telegram", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(bot.DB_PATH)
    bot.init_db(con)
    init_bets(con)
    if not args.no_fetch:
        bot.fetch_matches(con)

    results = settle_bets(con)
    acc = account(con)
    print(build_report(results, acc))

    placed = place_bets(con, args.days)
    print(f"\nNeu platziert: {len(placed)} Wetten")
    for league, home, away, kind, pick, stake, odds in placed:
        what = KIND_LABEL[kind]
        print(f"  [{league}] {home} - {away}: {what} {pick} @{odds:.2f}, Einsatz {stake:.2f} CHF")

    if args.no_telegram:
        pass
    elif not results and not placed:
        print("\nTelegram: nichts Neues, keine Nachricht gesendet")
    else:
        parts = []
        if results:
            parts.append(build_report(results, acc, emoji=True))
        if placed:
            parts.append(build_tips_text(placed, emoji=True))
        print("\nTelegram:", "gesendet" if notify.send("\n\n".join(parts)) else "nicht gesendet")
    con.close()


if __name__ == "__main__":
    main()

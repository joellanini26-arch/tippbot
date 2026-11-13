# Tippbot: paper.py
"""
Tippbot Schritt 3: Fake-Wetten mit virtuellem Geld.

Start:   python paper.py            (holt Daten, rechnet ab, platziert neue Wetten)
         python paper.py --days 2   (Wetten nur fuer die naechsten 2 Tage)
         python paper.py --no-fetch (ohne neuen API-Abruf)
         python paper.py --no-telegram (nichts senden, nur anzeigen)
Liegt im selben Ordner wie bot.py.
"""
import argparse
import itertools
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import bot
import calib
import daygame
import duel
import multi
import notify

# ---------- Einstellungen ----------
START_BALANCE = 1000.0   # virtuelles Startguthaben in CHF
STAKE_1X2 = 10.0         # Einsatz auf den 1/X/2-Tipp
STAKE_SCORE = 2.0        # Einsatz auf den Resultat-Tipp
STAKE_OU = 5.0           # Einsatz auf den Over/Under-Tipp
OU_LINE = 2.5            # Over/Under-Grenze (moeglich: 1.5, 2.5, 3.5)
MARGIN = 0.10            # Buchmacher-Marge auf die Modell-Quoten

# Der Bot tippt nur, wo er sich sicher genug ist (Modell-Wahrscheinlichkeit mindestens):
MIN_PROB_1X2 = 0.60      # 1/X/2-Tipp
MIN_PROB_OU = 0.65       # Over/Under
MIN_PROB_SCORE = 0.15    # exaktes Resultat (ist immer unsicher, daher tiefe Schwelle)
MIN_GAMES = 8            # beide Teams brauchen so viele Spiele in den Daten
MAX_STAKE_FACTOR = 3.0   # Einsatz nach Sicherheit: knapp ueber der Schwelle 1x, sehr sicher bis 3x

# Bet of the day: eine Kombiwette pro Tag aus mehreren sehr wahrscheinlichen Einzeltipps
STAKE_BOTD = 50.0          # Einsatz
BOTD_MIN_LEGS = 2          # mindestens so viele Tipps in der Kombi (muss eine Kombi sein)
BOTD_MAX_LEGS = 3          # hoechstens so viele Tipps
BOTD_LEG_MIN_PROB = 0.80   # jeder Einzeltipp: Modell-Wahrscheinlichkeit mindestens
BOTD_LEG_MIN_ODDS = 1.05   # jeder Einzeltipp: Quote mindestens
BOTD_TARGET_ODDS = 1.30    # Kombi-Quote, ab der keine weiteren Tipps dazukommen
BOTD_MIN_JOINT_PROB = 0.60 # Gesamtwahrscheinlichkeit der Kombi mindestens
MARGIN_BOTD = 0.05         # Marge fuer Quoten in diesem tiefen Bereich
BOTD_MIN_GAMES = 10        # beide Teams brauchen mindestens so viele Spiele in den Daten
BOTD_HOUR = 8              # fruehestens ab dieser Uhrzeit (Schweizer Zeit) wird gewaehlt

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Zurich")
except Exception:        # z.B. Windows ohne tzdata-Paket
    LOCAL_TZ = timezone(timedelta(hours=1))


KIND_LABEL = {"1X2": "Tipp 1X2", "SCORE": "Resultat", "OU": "Over/Under", "BOTD": "Bet of the day"}


def init_bets(con):
    con.execute("""
    CREATE TABLE IF NOT EXISTS bets (
        match_id INTEGER, kind TEXT, pick TEXT,
        stake REAL, odds REAL, placed_at TEXT,
        status TEXT DEFAULT 'OPEN',
        profit REAL, settled_at TEXT,
        PRIMARY KEY (match_id, kind)
    )""")
    calib.ensure_prob_column(con, MARGIN)
    con.execute("CREATE TABLE IF NOT EXISTS botd_log (day TEXT PRIMARY KEY, match_id INTEGER)")
    con.execute("""
    CREATE TABLE IF NOT EXISTS combos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT,
        stake REAL, odds REAL, prob REAL, placed_at TEXT,
        status TEXT DEFAULT 'OPEN', profit REAL, settled_at TEXT
    )""")
    con.execute("""
    CREATE TABLE IF NOT EXISTS combo_legs (
        combo_id INTEGER, match_id INTEGER, pick TEXT, odds REAL, prob REAL,
        PRIMARY KEY (combo_id, match_id)
    )""")
    con.commit()


# ---------- Wetten platzieren ----------
def make_odds(prob, margin=None):
    """Faire Quote minus Marge. Platzhalter, spaeter durch echte Quoten ersetzbar."""
    margin = MARGIN if margin is None else margin
    return max(1.01, round((1 - margin) / prob, 2))


_models = {}


def get_model(con, code):
    """Modell pro Liga, pro Programmlauf nur einmal berechnet."""
    if code not in _models:
        _models[code] = bot.fit_model(con, code)
    return _models[code]


BASE_STAKE = {"1X2": STAKE_1X2, "SCORE": STAKE_SCORE, "OU": STAKE_OU}
MIN_PROB = {"1X2": MIN_PROB_1X2, "SCORE": MIN_PROB_SCORE, "OU": MIN_PROB_OU}


def stake_for(kind, prob):
    """Einsatz nach Sicherheit: an der Schwelle der Grundeinsatz, bei 100 % das MAX_STAKE_FACTOR-fache."""
    thr = MIN_PROB[kind]
    f = 1 + (MAX_STAKE_FACTOR - 1) * (prob - thr) / (1 - thr)
    f = max(1.0, min(MAX_STAKE_FACTOR, f))
    return round(BASE_STAKE[kind] * f * 2) / 2          # auf 0.50 CHF gerundet


def selected_bets(p, fac=None):
    """Nur die Tipps, bei denen die (selbstkorrigierte) Wahrscheinlichkeit ueber der Schwelle liegt.
    Liefert [(kind, pick, modell_prob, korrigierte_prob, einsatz)]."""
    fac = fac or {}
    probs = {"1": p["p_home"], "X": p["p_draw"], "2": p["p_away"]}
    p_over = p[f"p_o{int(OU_LINE * 10)}"]
    ou_pick, ou_prob = (("Over", p_over) if p_over >= 0.5 else ("Under", 1 - p_over))
    out = []
    for kind, pick, prob in (("1X2", p["tip_1x2"], probs[p["tip_1x2"]]),
                             ("SCORE", p["tip_score"], p["score_prob"]),
                             ("OU", f"{ou_pick} {OU_LINE}", ou_prob)):
        pc = min(0.99, prob * fac.get(kind, {}).get("factor", 1.0))
        if pc >= MIN_PROB[kind]:
            out.append((kind, pick, prob, pc, stake_for(kind, pc)))
    return out


def place_bets(con, days):
    now = datetime.now(timezone.utc)
    placed = []
    fac = calib.factors(con)
    for code, name in bot.LEAGUES.items():
        try:
            model = get_model(con, code)
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
            games = model.get("n_games", {})
            if min(games.get(home, 0), games.get(away, 0)) < MIN_GAMES:
                continue
            p = bot.predict(model, home, away)
            for kind, pick, prob, pc, stake in selected_bets(p, fac):
                odds = make_odds(prob)
                cur = con.execute(
                    "INSERT OR IGNORE INTO bets (match_id, kind, pick, stake, odds, placed_at, prob) "
                    "VALUES (?,?,?,?,?,?,?)", (mid, kind, pick, stake, odds, now.isoformat(), prob))
                if cur.rowcount:
                    placed.append((name, home, away, kind, pick, stake, odds))
    con.commit()
    return placed


# ---------- Abrechnung ----------
def is_won(kind, pick, hg, ag):
    """Ist die Wette mit dem Endstand hg:ag gewonnen? (alle Wettarten)"""
    if kind == "SCORE":
        return pick == f"{hg}:{ag}"
    if pick.startswith(("Over", "Under")):
        side, line = pick.split()
        return (hg + ag > float(line)) == (side == "Over")
    result = "1" if hg > ag else "X" if hg == ag else "2"
    return result in pick          # "1", "X", "2", "1X", "X2"


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
            status, profit = "VOID", 0.0
        else:
            won = is_won(kind, pick, hg, ag)
            status = "WON" if won else "LOST"
            profit = round(stake * (odds - 1), 2) if won else -stake
        con.execute("UPDATE bets SET status=?, profit=?, settled_at=? WHERE match_id=? AND kind=?",
                    (status, profit, now, mid, kind))
        results.append({"home": home, "away": away, "kind": kind, "pick": pick,
                        "actual": (f"{hg}:{ag}" if hg is not None else "-"),
                        "stake": stake, "odds": odds, "status": status, "profit": profit})
    con.commit()
    results.extend(settle_combos(con))
    return results


def settle_combos(con):
    """Kombiwetten: verloren sobald ein Tipp verloren ist, gewonnen wenn alle gewonnen sind.
    Verschobene/abgesagte Spiele werden aus der Kombi genommen (Quote ohne diesen Tipp)."""
    now = datetime.now(timezone.utc).isoformat()
    results = []
    for cid, stake, odds in con.execute(
            "SELECT id, stake, odds FROM combos WHERE status='OPEN'").fetchall():
        legs = []
        for mid, pick, lodds, home, away, hg, ag, mstatus in con.execute("""
                SELECT l.match_id, l.pick, l.odds, m.home, m.away, m.home_goals, m.away_goals, m.status
                FROM combo_legs l JOIN matches m ON m.id = l.match_id
                WHERE l.combo_id = ?""", (cid,)).fetchall():
            if mstatus in ("POSTPONED", "CANCELLED"):
                state = "VOID"
            elif mstatus == "FINISHED" and hg is not None:
                state = "WON" if is_won("BOTD", pick, hg, ag) else "LOST"
            else:
                state = "OPEN"
            legs.append({"home": home, "away": away, "pick": pick, "odds": lodds, "state": state,
                         "score": f"{hg}:{ag}" if hg is not None else "-"})
        states = [l["state"] for l in legs]
        if "LOST" in states:
            status, profit = "LOST", -stake
        elif "OPEN" in states:
            continue
        else:
            won_odds = [l["odds"] for l in legs if l["state"] == "WON"]
            if not won_odds:
                status, profit, odds = "VOID", 0.0, 1.0
            else:
                if len(won_odds) < len(legs):          # Tipp(s) storniert: Quote neu berechnen
                    total = 1.0
                    for o in won_odds:
                        total *= o
                    odds = round(total, 2)
                status, profit = "WON", round(stake * (odds - 1), 2)   # sonst: gespeicherte Kombi-Quote
        con.execute("UPDATE combos SET status=?, profit=?, odds=?, settled_at=? WHERE id=?",
                    (status, profit, odds, now, cid))
        results.append({"kind": "BOTD", "combo": True, "home": "Kombi", "away": f"{len(legs)} Tipps",
                        "pick": "", "actual": "", "stake": stake, "odds": odds,
                        "status": status, "profit": profit, "legs": legs})
    con.commit()
    return results


def account(con):
    def one(sql, *args):
        return con.execute(sql, args).fetchone()
    total = (one("SELECT COALESCE(SUM(profit),0) FROM bets WHERE status!='OPEN'")[0]
             + one("SELECT COALESCE(SUM(profit),0) FROM combos WHERE status!='OPEN'")[0])
    b_open = one("SELECT COUNT(*), COALESCE(SUM(stake),0) FROM bets WHERE status='OPEN'")
    c_open = one("SELECT COUNT(*), COALESCE(SUM(stake),0) FROM combos WHERE status='OPEN'")
    won = sum(one(f"SELECT COUNT(*) FROM {t} WHERE status='WON'")[0] for t in ("bets", "combos"))
    lost = sum(one(f"SELECT COUNT(*) FROM {t} WHERE status='LOST'")[0] for t in ("bets", "combos"))
    return {"balance": START_BALANCE + total, "total_profit": total,
            "open": b_open[0] + c_open[0], "open_stake": b_open[1] + c_open[1],
            "won": won, "lost": lost}


# ---------- Bet of the day ----------
PICK_LABEL = {"1": "Heimsieg", "2": "Auswärtssieg",
              "1X": "Heimsieg oder Unentschieden", "X2": "Auswärtssieg oder Unentschieden"}


def pick_label(pick):
    if pick.startswith("Over"):
        return f"Über {pick.split()[1]} Tore"
    if pick.startswith("Under"):
        return f"Unter {pick.split()[1]} Tore"
    return PICK_LABEL.get(pick, pick)


def botd_candidates(p):
    """Maerkte, aus denen die Einzeltipps der Kombi gewaehlt werden."""
    return [("1", p["p_home"]), ("2", p["p_away"]),
            ("1X", p["p_home"] + p["p_draw"]), ("X2", p["p_draw"] + p["p_away"]),
            ("Over 1.5", p["p_o15"]), ("Under 3.5", 1 - p["p_o35"])]


def place_botd(con, now=None):
    """Stellt einmal pro Tag eine Kombiwette aus den sichersten Einzeltipps der heutigen Spiele
    zusammen. None = heute schon erledigt oder noch zu frueh, {} = heute nichts Passendes."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(LOCAL_TZ)
    if local.hour < BOTD_HOUR:
        return None
    day = local.strftime("%Y-%m-%d")
    if con.execute("SELECT 1 FROM botd_log WHERE day=?", (day,)).fetchone():
        return None

    pool = []   # je Spiel der sicherste passende Einzeltipp
    for code, name in bot.LEAGUES.items():
        try:
            model = get_model(con, code)
        except bot.NotEnoughData:
            continue
        rows = con.execute(
            "SELECT id, utc_date, home, away FROM matches "
            "WHERE league=? AND status IN ('SCHEDULED','TIMED')", (code,)).fetchall()
        for mid, d, home, away in rows:
            if d.endswith("T00:00:00Z"):
                continue                       # Anstosszeit unbekannt (z.B. Nations League)
            ko = bot.parse_date(d)
            if ko <= now or ko.astimezone(LOCAL_TZ).strftime("%Y-%m-%d") != day:
                continue
            if home not in model["att"] or away not in model["att"]:
                continue
            games = model.get("n_games", {})
            if min(games.get(home, 0), games.get(away, 0)) < BOTD_MIN_GAMES:
                continue
            p = bot.predict(model, home, away)
            for pick, prob in botd_candidates(p):
                odds = make_odds(prob, MARGIN_BOTD)
                if prob >= BOTD_LEG_MIN_PROB and odds >= BOTD_LEG_MIN_ODDS:
                    pool.append({"match_id": mid, "league": name, "home": home, "away": away,
                                 "utc": d, "pick": pick, "prob": prob, "odds": odds})

    # Alle Kombinationen aus 2..MAX Tipps (je Spiel hoechstens einer) durchrechnen und die mit der
    # hoechsten Gesamtwahrscheinlichkeit nehmen, die Ziel-Quote und Mindestwahrscheinlichkeit erreicht.
    pool.sort(key=lambda x: -x["prob"])
    pool = pool[:80]
    chosen, odds, prob = [], 0.0, 0.0
    for k in range(BOTD_MIN_LEGS, BOTD_MAX_LEGS + 1):
        for combo in itertools.combinations(pool, k):
            if len({l["match_id"] for l in combo}) < k:
                continue
            c_odds = 1.0
            c_prob = 1.0
            for l in combo:
                c_odds *= l["odds"]
                c_prob *= l["prob"]
            c_odds = round(c_odds, 2)
            if c_odds >= BOTD_TARGET_ODDS and c_prob >= BOTD_MIN_JOINT_PROB and c_prob > prob:
                chosen, odds, prob = sorted(combo, key=lambda l: l["utc"]), c_odds, c_prob
    ok = bool(chosen)

    con.execute("INSERT OR IGNORE INTO botd_log (day, match_id) VALUES (?,NULL)", (day,))
    combo = {}
    if ok:
        cur = con.execute(
            "INSERT INTO combos (day, stake, odds, prob, placed_at) VALUES (?,?,?,?,?)",
            (day, STAKE_BOTD, odds, prob, now.isoformat()))
        combo = {"id": cur.lastrowid, "odds": odds, "prob": prob, "legs": chosen}
        con.executemany(
            "INSERT INTO combo_legs (combo_id, match_id, pick, odds, prob) VALUES (?,?,?,?,?)",
            [(cur.lastrowid, l["match_id"], l["pick"], l["odds"], l["prob"]) for l in chosen])
    con.commit()
    return combo


def botd_record(con):
    won, lost, profit = con.execute(
        "SELECT COALESCE(SUM(status='WON'),0), COALESCE(SUM(status='LOST'),0), "
        "COALESCE(SUM(profit),0) FROM combos WHERE status IN ('WON','LOST')").fetchone()
    return int(won), int(lost), float(profit)


def build_botd_text(best, record, emoji=True):
    star = "\u2b50 " if emoji else ""
    won, lost, profit = record
    rec = f"Bilanz Bet of the day: {won} Treffer, {lost} Nieten, {profit:+.2f} CHF"
    if not best:
        return (f"{star}Bet of the day: Heute gibt es keine Kombi, die meine Kriterien erfüllt "
                f"(Einzeltipps ab {BOTD_LEG_MIN_PROB:.0%}, Kombi-Quote ab {BOTD_TARGET_ODDS:.2f}, "
                f"Gesamtwahrscheinlichkeit ab {BOTD_MIN_JOINT_PROB:.0%}).\n{rec}")
    lines = [f"{star}Bet of the day: Kombi mit {len(best['legs'])} Tipps (Fake-Einsatz {STAKE_BOTD:.0f} CHF)"]
    for i, l in enumerate(best["legs"], 1):
        ko = bot.parse_date(l["utc"]).astimezone(LOCAL_TZ).strftime("%H:%M")
        lines.append(f"{i}) {l['league']} · {ko}\n   {l['home']} – {l['away']}: "
                     f"{pick_label(l['pick'])} (@{l['odds']:.2f}, {l['prob']:.0%})")
    win = STAKE_BOTD * (best["odds"] - 1)
    miss = round(1 / (1 - best["prob"]))
    lines.append(f"Kombi-Quote @{best['odds']:.2f} · Modell-Wahrscheinlichkeit gesamt {best['prob']:.0%}")
    lines.append(f"Bei Treffer {win:+.2f} CHF, bei Niete {-STAKE_BOTD:.2f} CHF")
    lines.append(f"Keine sichere Wette: Bei {best['prob']:.0%} geht im Schnitt etwa jede {miss}. Kombi "
                 f"daneben, und schon ein einziger verlorener Tipp reicht dafür.")
    lines.append(rec)
    return "\n".join(lines)


# ---------- Bericht ----------
def build_report(results, acc, emoji=False):
    """Text-Bericht (wird spaeter auch per Telegram gesendet)."""
    lines = []
    if results:
        lines.append("Abgerechnete Wetten:")
        for r in results:
            if r.get("combo"):
                m = {"WON": "\u2705", "LOST": "\u274c", "VOID": "\u21a9\ufe0f"} if emoji else \
                    {"WON": "[OK]", "LOST": "[--]", "VOID": "[ - ]"}
                lines.append(f"{m[r['status']]} Bet of the day (Kombi @{r['odds']:.2f}): {r['profit']:+.2f} CHF")
                for l in r["legs"]:
                    sm = {"WON": "ok", "LOST": "x", "VOID": "-", "OPEN": "offen"}[l["state"]]
                    lines.append(f"   {l['home']} - {l['away']}: {pick_label(l['pick'])} "
                                 f"({l['score']}, {sm})")
                continue
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
        by_league.setdefault(league, {}).setdefault((home, away), {})[kind] = (pick, odds, stake)
    lines = [title]
    for league, games in by_league.items():
        lines.append(f"\n{league}:")
        for (home, away), b in games.items():
            parts = []
            if "1X2" in b:
                parts.append(f"Tipp {b['1X2'][0]} (@{b['1X2'][1]:.2f}, {b['1X2'][2]:g} CHF)")
            if "SCORE" in b:
                parts.append(f"Resultat {b['SCORE'][0]} (@{b['SCORE'][1]:.2f}, {b['SCORE'][2]:g} CHF)")
            if "OU" in b:
                parts.append(f"{b['OU'][0]} (@{b['OU'][1]:.2f}, {b['OU'][2]:g} CHF)")
            lines.append(f"{home} - {away}: " + ", ".join(parts))
    return "\n".join(lines)


def _env_num(name):
    """Zahl aus einer Umgebungsvariable (Eingaben des GitHub-Workflows), sonst None."""
    v = (os.environ.get(name) or "").strip().replace("'", "").replace(",", ".")
    try:
        return float(v) if v else None
    except ValueError:
        return None


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")   # Sonderzeichen in Teamnamen sicher ausgeben
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--multi-start", type=float, default=_env_num("MULTI_START"),
                    help="neuer Multiplikator-Versuch: Startkapital")
    ap.add_argument("--multi-target", type=float, default=_env_num("MULTI_TARGET"),
                    help="neuer Multiplikator-Versuch: Ziel")
    ap.add_argument("--multi-days", type=float, default=_env_num("MULTI_DAYS"),
                    help="neuer Multiplikator-Versuch: Dauer in Tagen")
    args = ap.parse_args()

    con = sqlite3.connect(bot.DB_PATH)
    bot.init_db(con)
    init_bets(con)
    multi.init(con)
    duel.init(con)
    daygame.init(con)
    if args.multi_start:
        try:
            rid = multi.start_run(con, args.multi_start,
                                  args.multi_target or multi.DEFAULT_TARGET,
                                  args.multi_days or multi.DEFAULT_DAYS)
            print(f"Multiplikator: neuer Versuch #{rid} gestartet")
        except ValueError as e:
            print(f"Multiplikator: {e}")
    else:
        multi.ensure_first_run(con)
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

    botd = place_botd(con)
    botd_text = None
    if botd is not None:
        botd_text = build_botd_text(botd, botd_record(con))
        print("\n" + build_botd_text(botd, botd_record(con), emoji=False))

    dg = daygame.pick(con)
    if dg:
        print(f"\nTagesduell: Spiel des Tages gewaehlt (Bot tippt {dg['bot_score']})")

    duel_events = duel.settle(con)
    duel_placed = duel.place(con)
    duel_text = duel.build_text(duel_placed, duel_events)
    if duel_text:
        print("\n" + duel_text)
    elif duel_placed == {}:
        print("\nTippduell: gerade keine passende Wette, naechster Versuch beim naechsten Lauf")

    multi_events = multi.settle(con)
    multi_placed = multi.place(con)
    multi_text = multi.build_text(multi_placed, multi_events)
    if multi_text:
        print("\n" + multi.build_text(multi_placed, multi_events, emoji=False))
    elif multi_placed == {}:
        print("\nMultiplikator: gerade keine passende Kombi, naechster Versuch beim naechsten Lauf")

    if args.no_telegram:
        pass
    elif not results and not placed and botd is None and not multi_text and not duel_text:
        print("\nTelegram: nichts Neues, keine Nachricht gesendet")
    else:
        parts = []
        if botd_text:
            parts.append(botd_text)
        if multi_text:
            parts.append(multi_text)
        if duel_text:
            parts.append(duel_text)
        if results:
            parts.append(build_report(results, acc, emoji=True))
        if placed:
            parts.append(build_tips_text(placed, emoji=True))
        print("\nTelegram:", "gesendet" if notify.send("\n\n".join(parts)) else "nicht gesendet")
    con.close()


if __name__ == "__main__":
    main()

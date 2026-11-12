# Tippbot: duel.py
"""
Tippduell-Challenge gegen GoalOracle: aus 100 CHF Spielgeld 100'000 CHF machen.

Regeln:
- Immer nur EINE offene Wette. Erst wenn sie abgerechnet ist, kommt die naechste.
- Der ganze aktuelle Betrag wird eingesetzt.
- Gesamtquote 1.85 bis 2.30. Einzelwette oder Kombi mit 2-3 Spielen; genommen wird die Variante
  mit der hoechsten Gewinnchance laut Modell. Nur Spiele, die noch nicht angepfiffen sind.
- Gewinn: neuer Betrag = Einsatz x Quote. Verlust: Versuch beendet, naechster startet mit 100.
- Abgesagtes Spiel: Einzelwette zaehlt nicht (Einsatz zurueck), in einer Kombi zaehlt es mit Quote 1.
- Alle Versuche bleiben gespeichert.
Die Datei site/duel.json (Format tippduell/1) schreibt build_site.py nach jedem Lauf.
"""
import itertools
import math
from datetime import datetime, timedelta, timezone

import bot

START = 100.0
GOAL = 100000.0
ODDS_MIN = 1.85
ODDS_MAX = 2.30
MAX_LEGS = 3
MARGIN = 0.05              # Marge fuer die Modell-Quoten (wie Multiplikator)
LOOKAHEAD_HOURS = 36       # nur Spiele, die in diesem Zeitfenster beginnen
MIN_LEAD_MINUTES = 10      # Anpfiff fruehestens so viele Minuten nach dem Setzen
MIN_GAMES = 10             # beide Teams brauchen so viele Spiele in den Daten
POOL_SIZE = 40             # so viele Einzeltipps werden fuer Kombis kombiniert

FORMAT = "tippduell/1"
BOT_INFO = {"name": "Tippbot", "owner": "Joel", "url": "https://joellanini26-arch.github.io/tippbot/"}
PARTNER_URL = "https://timeo44.github.io/goaloracle/duel.json"
VOID_STATUS = ("POSTPONED", "CANCELLED", "SUSPENDED", "AWARDED")

# Teamnamen auf Englisch wie bei ESPN/Buchmachern (Datenquelle nutzt Kurzformen)
ESPN_NAMES = {
    "Brighton Hove": "Brighton & Hove Albion", "Man City": "Manchester City",
    "Man United": "Manchester United", "Newcastle": "Newcastle United",
    "Nottingham": "Nottingham Forest", "Tottenham": "Tottenham Hotspur", "Wolverhampton": "Wolverhampton Wanderers",
    "Wolves": "Wolverhampton Wanderers", "West Ham": "West Ham United", "Leicester": "Leicester City",
    "Alavés": "Alavés", "Athletic": "Athletic Club", "Atleti": "Atlético Madrid", "Barça": "Barcelona",
    "Celta": "Celta Vigo", "Sevilla FC": "Sevilla", "Santander": "Racing Santander", "Deportivo": "Deportivo La Coruña",
    "1. FC Köln": "FC Cologne", "Bayern": "Bayern Munich", "Bremen": "Werder Bremen",
    "Dortmund": "Borussia Dortmund", "Frankfurt": "Eintracht Frankfurt", "Freiburg": "SC Freiburg",
    "HSV": "Hamburg SV", "Leverkusen": "Bayer Leverkusen", "M'gladbach": "Borussia Mönchengladbach",
    "Mainz": "Mainz", "Stuttgart": "VfB Stuttgart", "Schalke": "Schalke 04", "Hoffenheim": "TSG Hoffenheim",
    "SC Paderborn": "SC Paderborn 07", "Elversberg": "SV Elversberg", "Wolfsburg": "VfL Wolfsburg",
    "Como 1907": "Como", "Inter": "Internazionale", "Milan": "AC Milan", "Roma": "AS Roma",
    "Venezia FC": "Venezia",
    "Angers SCO": "Angers", "Olympique Lyon": "Lyon", "PSG": "Paris Saint-Germain", "RC Lens": "Lens",
    "Stade Rennais": "Rennes", "Le Mans": "Le Mans", "Marseille": "Marseille",
    "Turkey": "Türkiye", "Czech Republic": "Czechia", "Republic of Ireland": "Republic of Ireland",
}


def espn(name):
    return ESPN_NAMES.get(name, name)


def init(con):
    con.execute("""
    CREATE TABLE IF NOT EXISTS duel_attempts (
        n INTEGER PRIMARY KEY, started TEXT, ended TEXT,
        status TEXT DEFAULT 'open', bank REAL, peak REAL
    )""")
    con.execute("""
    CREATE TABLE IF NOT EXISTS duel_bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, attempt INTEGER, placed TEXT,
        stake REAL, odds REAL, probability REAL,
        status TEXT DEFAULT 'open', payout REAL, settled TEXT
    )""")
    con.execute("""
    CREATE TABLE IF NOT EXISTS duel_legs (
        bet_id INTEGER, match_id INTEGER, kickoff TEXT, competition TEXT, home TEXT, away TEXT,
        pick TEXT, label TEXT, odds REAL, probability REAL,
        status TEXT DEFAULT 'open', score TEXT,
        PRIMARY KEY (bet_id, match_id)
    )""")
    con.commit()


def _now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def kickoff_iso(utc_date):
    return iso(bot.parse_date(utc_date))


def open_attempt(con, now=None):
    """Aktueller Versuch; legt den ersten bzw. naechsten an, falls keiner offen ist."""
    row = con.execute("SELECT n, bank FROM duel_attempts WHERE status='open' ORDER BY n DESC LIMIT 1").fetchone()
    if row:
        return row
    n = (con.execute("SELECT MAX(n) FROM duel_attempts").fetchone()[0] or 0) + 1
    con.execute("INSERT INTO duel_attempts (n, started, ended, status, bank, peak) VALUES (?,?,NULL,'open',?,?)",
                (n, iso(now or _now()), START, START))
    con.commit()
    return n, START


# ---------- Maerkte ----------
def candidates(p, home, away):
    """[(pick-code, label, Wahrscheinlichkeit)] fuer ein Spiel (90 Minuten)."""
    ph, pd, pa = p["p_home"], p["p_draw"], p["p_away"]
    btts = (1 - math.exp(-p["exp_home"])) * (1 - math.exp(-p["exp_away"]))
    out = [("1", f"Sieg {home}", ph), ("X", "Unentschieden", pd), ("2", f"Sieg {away}", pa),
           ("1X", f"{home} oder Unentschieden", ph + pd), ("X2", f"{away} oder Unentschieden", pd + pa),
           ("12", f"Sieg {home} oder {away}", ph + pa),
           ("BTTS_YES", "Beide Teams treffen", btts), ("BTTS_NO", "Nicht beide Teams treffen", 1 - btts)]
    for line, key in ((1.5, "p_o15"), (2.5, "p_o25"), (3.5, "p_o35")):
        out.append((f"O{line}", f"Über {line} Tore", p[key]))
        out.append((f"U{line}", f"Unter {line} Tore", 1 - p[key]))
    return out


def leg_won(pick, hg, ag):
    if pick in ("1", "X", "2", "1X", "X2", "12"):
        res = "1" if hg > ag else "X" if hg == ag else "2"
        return res in pick
    if pick == "BTTS_YES":
        return hg > 0 and ag > 0
    if pick == "BTTS_NO":
        return not (hg > 0 and ag > 0)
    if pick[0] in "OU":
        line = float(pick[1:])
        return (hg + ag > line) == (pick[0] == "O")
    raise ValueError(pick)


# ---------- Abrechnung ----------
def settle(con, now=None):
    now = now or _now()
    events = []
    for bid, n, stake, odds in con.execute(
            "SELECT id, attempt, stake, odds FROM duel_bets WHERE status='open'").fetchall():
        legs = con.execute("""SELECT l.match_id, l.pick, l.odds, m.status, m.home_goals, m.away_goals
                              FROM duel_legs l LEFT JOIN matches m ON m.id=l.match_id
                              WHERE l.bet_id=?""", (bid,)).fetchall()
        states = []
        for mid, pick, lodds, mstatus, hg, ag in legs:
            if mstatus in VOID_STATUS:
                st, score = "void", None
            elif mstatus == "FINISHED" and hg is not None:
                st, score = ("won" if leg_won(pick, hg, ag) else "lost"), f"{hg}:{ag}"
            else:
                st, score = "open", None
            if st != "open":
                con.execute("UPDATE duel_legs SET status=?, score=? WHERE bet_id=? AND match_id=?",
                            (st, score, bid, mid))
            states.append((st, lodds))
        sts = [s for s, _ in states]
        if "lost" in sts:
            status, payout = "lost", 0.0
        elif "open" in sts:
            continue
        elif all(s == "void" for s in sts):
            status, payout = "void", stake                     # zaehlt nicht: Einsatz zurueck
        else:
            eff = math.prod(o for s, o in states if s == "won")   # abgesagte Spiele mit Quote 1
            status, payout = "won", round(stake * eff, 2)
        con.execute("UPDATE duel_bets SET status=?, payout=?, settled=? WHERE id=?",
                    (status, payout, iso(now), bid))
        bank, peak = con.execute("SELECT bank, peak FROM duel_attempts WHERE n=?", (n,)).fetchone()
        if status == "lost":
            con.execute("UPDATE duel_attempts SET status='lost', bank=0, ended=? WHERE n=?", (iso(now), n))
        else:
            bank = payout
            peak = max(peak, bank)
            att_status = "won" if bank >= GOAL else "open"
            con.execute("UPDATE duel_attempts SET bank=?, peak=?, status=?, ended=? WHERE n=?",
                        (bank, peak, att_status, iso(now) if att_status == "won" else None, n))
        con.commit()
        events.append({"attempt": n, "status": status, "stake": stake, "odds": odds, "payout": payout})
    return events


# ---------- Neue Wette ----------
def place(con, now=None):
    """Setzt die naechste Wette, falls keine offen ist. None = offen, {} = nichts Passendes gefunden."""
    import paper
    now = now or _now()
    if con.execute("SELECT 1 FROM duel_bets WHERE status='open'").fetchone():
        return None
    n, bank = open_attempt(con, now)
    earliest = now + timedelta(minutes=MIN_LEAD_MINUTES)
    horizon = now + timedelta(hours=LOOKAHEAD_HOURS)
    pool = []
    for code, name in bot.LEAGUES.items():
        try:
            model = paper.get_model(con, code)
        except bot.NotEnoughData:
            continue
        for mid, d, home, away in con.execute(
                "SELECT id, utc_date, home, away FROM matches "
                "WHERE league=? AND status IN ('SCHEDULED','TIMED')", (code,)).fetchall():
            if d.endswith("T00:00:00Z"):
                continue                   # Anpfiffzeit unbekannt
            ko = bot.parse_date(d)
            if not (earliest <= ko <= horizon):
                continue
            if home not in model["att"] or away not in model["att"]:
                continue
            games = model.get("n_games", {})
            if min(games.get(home, 0), games.get(away, 0)) < MIN_GAMES:
                continue
            p = bot.predict(model, home, away)
            h, a = espn(home), espn(away)
            for pick, label, prob in candidates(p, h, a):
                if prob < 0.05 or prob > 0.97:
                    continue
                pool.append({"match_id": mid, "kickoff": kickoff_iso(d), "competition": name,
                             "home": h, "away": a, "pick": pick, "label": label, "prob": prob,
                             "odds": paper.make_odds(prob, MARGIN)})

    best, best_prob, best_odds = None, 0.0, 0.0
    for leg in pool:                                   # Einzelwetten
        if ODDS_MIN <= leg["odds"] <= ODDS_MAX and leg["prob"] > best_prob:
            best, best_prob, best_odds = (leg,), leg["prob"], leg["odds"]
    short = sorted((l for l in pool if l["odds"] < ODDS_MAX), key=lambda l: -l["prob"])[:POOL_SIZE]
    for k in range(2, MAX_LEGS + 1):                   # Kombis aus 2-3 Spielen
        for combo in itertools.combinations(short, k):
            if len({l["match_id"] for l in combo}) < k:
                continue
            o = math.prod(l["odds"] for l in combo)
            if not (ODDS_MIN <= o <= ODDS_MAX):
                continue
            pr = math.prod(l["prob"] for l in combo)
            if pr > best_prob:
                best, best_prob, best_odds = combo, pr, o
    if not best:
        return {}
    legs = sorted(best, key=lambda l: l["kickoff"])
    odds = round(best_odds, 4)
    cur = con.execute("INSERT INTO duel_bets (attempt, placed, stake, odds, probability) VALUES (?,?,?,?,?)",
                      (n, iso(now), round(bank, 2), odds, round(best_prob, 4)))
    con.executemany("""INSERT INTO duel_legs (bet_id, match_id, kickoff, competition, home, away, pick, label,
                       odds, probability) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    [(cur.lastrowid, l["match_id"], l["kickoff"], l["competition"], l["home"], l["away"],
                      l["pick"], l["label"], l["odds"], round(l["prob"], 4)) for l in legs])
    con.commit()
    return {"attempt": n, "stake": round(bank, 2), "odds": odds, "prob": best_prob, "legs": legs}


# ---------- Export ----------
def challenge(con):
    attempts = []
    for n, started, ended, status, bank, peak in con.execute(
            "SELECT n, started, ended, status, bank, peak FROM duel_attempts ORDER BY n"):
        bets = []
        for bid, placed, stake, odds, prob, bstatus, payout in con.execute(
                "SELECT id, placed, stake, odds, probability, status, payout FROM duel_bets "
                "WHERE attempt=? ORDER BY id", (n,)).fetchall():
            legs = [{"kickoff": k, "competition": c, "home": h, "away": a, "pick": pk, "label": lb,
                     "odds": float(o), "status": st, "score": sc}
                    for k, c, h, a, pk, lb, o, st, sc in con.execute(
                        "SELECT kickoff, competition, home, away, pick, label, odds, status, score "
                        "FROM duel_legs WHERE bet_id=? ORDER BY kickoff, match_id", (bid,))]
            bets.append({"placed": placed, "stake": float(stake), "odds": float(odds),
                         "probability": float(prob), "status": bstatus,
                         "payout": None if payout is None else float(payout), "legs": legs})
        attempts.append({"n": n, "started": started, "ended": ended, "status": status,
                         "bank": float(bank), "peak": float(peak), "bets": bets})
    return {"start": START, "goal": GOAL, "currency": "CHF", "attempts": attempts}


def duel_json(con, now=None):
    return {"format": FORMAT, "updated": iso(now or _now()), "bot": BOT_INFO, "tips": [],
            "challenge": challenge(con)}


# ---------- Text fuer Telegram ----------
def _chf(v):
    return f"{v:,.2f}".replace(",", "'")


def build_text(placed, events):
    lines = []
    mark = {"won": "✅", "lost": "❌", "void": "↩️"}
    for e in events:
        if e["status"] == "lost":
            lines.append(f"❌ Tippduell-Versuch #{e['attempt']}: Wette @{e['odds']:.2f} verloren, "
                         f"Versuch beendet. Neuer Versuch startet mit {START:.0f} CHF.")
        else:
            lines.append(f"{mark[e['status']]} Tippduell-Versuch #{e['attempt']}: Wette @{e['odds']:.2f} "
                         f"{'gewonnen' if e['status'] == 'won' else 'storniert'}, Betrag jetzt {_chf(e['payout'])} CHF")
    if placed:
        kind = "Einzelwette" if len(placed["legs"]) == 1 else f"Kombi mit {len(placed['legs'])} Spielen"
        lines.append(f"🥊 Tippduell #{placed['attempt']}: {kind}, Einsatz {_chf(placed['stake'])} CHF "
                     f"@{placed['odds']:.2f} ({placed['prob']:.0%})")
        for l in placed["legs"]:
            ko = bot.parse_date(l["kickoff"])
            import paper
            t = ko.astimezone(paper.LOCAL_TZ)
            lines.append(f"   {['Mo','Di','Mi','Do','Fr','Sa','So'][t.weekday()]} {t:%H:%M} · {l['competition']}: "
                         f"{l['home']} – {l['away']}: {l['label']} (@{l['odds']:.2f})")
    return "\n".join(lines)

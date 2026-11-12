# Tippbot: multi.py
"""
Multiplikator-Challenge: aus einem Startkapital in einer festen Zeit ein Ziel erreichen.

Regeln:
- Der Bot setzt immer den ganzen Topf auf eine Kombiwette mit Quote um 2.0.
- Erst wenn die Kombi abgerechnet ist, kommt die naechste (der Gewinn wird weitergesetzt).
- Nach jeweils 3 Treffern gehen 50 % des Gewinns seit der letzten Sparrunde aufs Sparkonto,
  der Rest bleibt im Topf und wird weitergesetzt.
- Eine verlorene Kombi kostet den ganzen Topf und beendet den Versuch. Das Sparkonto bleibt.
- Ziel erreicht, sobald Topf + Sparkonto >= Ziel.
- Neue Versuche: python paper.py --multi-start 100 --multi-target 5000 --multi-days 28
  (oder ueber das Formular im Dashboard, das den GitHub-Workflow mit diesen Werten startet).
"""
import itertools
import math
from datetime import datetime, timedelta, timezone

import bot

# ---------- Einstellungen ----------
DEFAULT_START = 100.0      # erster Versuch, falls noch keiner existiert
DEFAULT_TARGET = 5000.0
DEFAULT_DAYS = 28

TARGET_ODDS_MIN = 1.85     # Kombi-Quote mindestens
TARGET_ODDS_MAX = 2.30     # Kombi-Quote hoechstens
MIN_LEGS = 2
MAX_LEGS = 4
LEG_MIN_PROB = 0.60        # jeder Einzeltipp: Modell-Wahrscheinlichkeit mindestens
MIN_JOINT_PROB = 0.40      # Kombi gesamt: Modell-Wahrscheinlichkeit mindestens ("sicher genug")
MIN_GAMES = 10             # beide Teams brauchen so viele Spiele in den Daten
MARGIN = 0.05              # Marge fuer die Modell-Quoten
LOOKAHEAD_HOURS = 36       # nur Spiele, die in diesem Zeitfenster beginnen
MIN_LEAD_MINUTES = 30      # Anstoss fruehestens so viele Minuten nach dem Platzieren
SAVE_EVERY = 3             # nach so vielen Treffern wird gespart
SAVE_SHARE = 0.5           # Anteil des Gewinns, der aufs Sparkonto geht
POOL_SIZE = 40             # so viele Einzeltipps werden kombiniert (Rechenzeit)


def init(con):
    con.execute("""
    CREATE TABLE IF NOT EXISTS multi_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, ends_at TEXT,
        start_capital REAL, target REAL, days INTEGER,
        pot REAL, savings REAL DEFAULT 0, base REAL, streak INTEGER DEFAULT 0,
        status TEXT DEFAULT 'ACTIVE', ended_at TEXT
    )""")
    con.execute("""
    CREATE TABLE IF NOT EXISTS multi_bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER,
        stake REAL, odds REAL, prob REAL, placed_at TEXT,
        status TEXT DEFAULT 'OPEN', profit REAL, settled_at TEXT,
        pot_after REAL, saved REAL DEFAULT 0
    )""")
    con.execute("""
    CREATE TABLE IF NOT EXISTS multi_legs (
        bet_id INTEGER, match_id INTEGER, pick TEXT, odds REAL, prob REAL,
        PRIMARY KEY (bet_id, match_id)
    )""")
    con.commit()


def _now():
    return datetime.now(timezone.utc)


def active_run(con):
    row = con.execute("SELECT id FROM multi_runs WHERE status='ACTIVE' ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def start_run(con, start, target, days, now=None):
    """Neuer Versuch. Ein laufender Versuch wird abgebrochen (seine offene Kombi verfaellt)."""
    now = now or _now()
    if start <= 0 or target <= start or days <= 0:
        raise ValueError(f"Ungueltige Werte: Start {start}, Ziel {target}, Tage {days}")
    con.execute("UPDATE multi_bets SET status='CANCELLED', settled_at=? WHERE status='OPEN' AND run_id IN "
                "(SELECT id FROM multi_runs WHERE status='ACTIVE')", (now.isoformat(),))
    con.execute("UPDATE multi_runs SET status='CANCELLED', ended_at=? WHERE status='ACTIVE'", (now.isoformat(),))
    cur = con.execute(
        "INSERT INTO multi_runs (created_at, ends_at, start_capital, target, days, pot, base) "
        "VALUES (?,?,?,?,?,?,?)",
        (now.isoformat(), (now + timedelta(days=days)).isoformat(), start, target, int(days), start, start))
    con.commit()
    return cur.lastrowid


def ensure_first_run(con):
    if not con.execute("SELECT 1 FROM multi_runs LIMIT 1").fetchone():
        return start_run(con, DEFAULT_START, DEFAULT_TARGET, DEFAULT_DAYS)
    return None


def run_info(con, rid):
    r = con.execute("SELECT id, created_at, ends_at, start_capital, target, days, pot, savings, base, "
                    "streak, status, ended_at FROM multi_runs WHERE id=?", (rid,)).fetchone()
    keys = ["id", "created_at", "ends_at", "start", "target", "days", "pot", "savings", "base",
            "streak", "status", "ended_at"]
    return dict(zip(keys, r)) if r else None


def leg_rows(con, bet_id):
    """Einzeltipps einer Kombi mit aktuellem Zustand."""
    import paper
    legs = []
    for mid, pick, lodds, prob, home, away, utc, league, hg, ag, mstatus in con.execute("""
            SELECT l.match_id, l.pick, l.odds, l.prob, m.home, m.away, m.utc_date, m.league,
                   m.home_goals, m.away_goals, m.status
            FROM multi_legs l JOIN matches m ON m.id=l.match_id
            WHERE l.bet_id=? ORDER BY m.utc_date""", (bet_id,)).fetchall():
        if mstatus in ("POSTPONED", "CANCELLED"):
            state = "VOID"
        elif mstatus == "FINISHED" and hg is not None:
            state = "WON" if paper.is_won("MULTI", pick, hg, ag) else "LOST"
        else:
            state = "OPEN"
        legs.append({"match_id": mid, "league": league, "home": home, "away": away, "utc": utc,
                     "pick": pick, "odds": lodds, "prob": prob, "state": state, "hg": hg, "ag": ag})
    return legs


# ---------- Abrechnung ----------
def settle(con, now=None):
    """Rechnet die offene Kombi des laufenden Versuchs ab und beendet abgelaufene Versuche.
    Gibt eine Liste von Ereignissen fuer Bericht/Telegram zurueck."""
    now = now or _now()
    events = []
    rid = active_run(con)
    if rid is None:
        return events
    run = run_info(con, rid)
    row = con.execute("SELECT id, stake, odds FROM multi_bets WHERE run_id=? AND status='OPEN'", (rid,)).fetchone()
    if row:
        bid, stake, odds = row
        legs = leg_rows(con, bid)
        states = [l["state"] for l in legs]
        if "LOST" in states:
            status, profit = "LOST", -stake
        elif "OPEN" in states:
            status = None
        else:
            won_odds = [l["odds"] for l in legs if l["state"] == "WON"]
            if not won_odds:
                status, profit, odds = "VOID", 0.0, 1.0
            else:
                if len(won_odds) < len(legs):          # stornierte Tipps: Quote neu berechnen
                    odds = round(math.prod(won_odds), 2)
                status, profit = "WON", round(stake * (odds - 1), 2)
        if status:
            pot, savings, base, streak = stake + profit, run["savings"], run["base"], run["streak"]
            saved = 0.0
            if status == "WON":
                streak += 1
                if streak >= SAVE_EVERY:
                    gain = pot - base
                    if gain > 0:
                        saved = round(gain * SAVE_SHARE, 2)
                        pot = round(pot - saved, 2)
                        savings = round(savings + saved, 2)
                    base, streak = pot, 0
            elif status == "VOID":
                pot = stake
            con.execute("UPDATE multi_bets SET status=?, profit=?, odds=?, settled_at=?, pot_after=?, saved=? "
                        "WHERE id=?", (status, profit, odds, now.isoformat(), pot, saved, bid))
            run_status = "ACTIVE"
            if status == "LOST":
                run_status = "LOST"
            elif pot + savings >= run["target"]:
                run_status = "REACHED"
            con.execute("UPDATE multi_runs SET pot=?, savings=?, base=?, streak=?, status=?, ended_at=? WHERE id=?",
                        (pot, savings, base, streak, run_status,
                         None if run_status == "ACTIVE" else now.isoformat(), rid))
            con.commit()
            events.append({"type": "settled", "status": status, "stake": stake, "odds": odds,
                           "profit": profit, "pot": pot, "savings": savings, "saved": saved,
                           "run_status": run_status, "target": run["target"], "legs": legs})
            run = run_info(con, rid)
        else:
            return events          # Kombi laeuft noch
    if run["status"] == "ACTIVE" and now >= bot.parse_date(run["ends_at"]):
        con.execute("UPDATE multi_runs SET status='EXPIRED', ended_at=? WHERE id=?", (now.isoformat(), rid))
        con.commit()
        events.append({"type": "expired", "pot": run["pot"], "savings": run["savings"], "target": run["target"]})
    return events


# ---------- Neue Kombi ----------
def place(con, now=None):
    """Platziert die naechste Kombi, falls ein Versuch laeuft und keine Kombi offen ist.
    None = nichts zu tun, {} = keine passende Kombi gefunden (wird beim naechsten Lauf erneut versucht)."""
    import paper
    now = now or _now()
    rid = active_run(con)
    if rid is None:
        return None
    if con.execute("SELECT 1 FROM multi_bets WHERE run_id=? AND status='OPEN'", (rid,)).fetchone():
        return None
    run = run_info(con, rid)
    if run["pot"] <= 0:
        return None
    ends = bot.parse_date(run["ends_at"])
    horizon = min(now + timedelta(hours=LOOKAHEAD_HOURS), ends)
    earliest = now + timedelta(minutes=MIN_LEAD_MINUTES)

    pool = []
    for code, name in bot.LEAGUES.items():
        try:
            model = paper.get_model(con, code)
        except bot.NotEnoughData:
            continue
        rows = con.execute(
            "SELECT id, utc_date, home, away FROM matches "
            "WHERE league=? AND status IN ('SCHEDULED','TIMED')", (code,)).fetchall()
        for mid, d, home, away in rows:
            if d.endswith("T00:00:00Z"):
                continue                   # Anstosszeit unbekannt
            ko = bot.parse_date(d)
            if not (earliest <= ko <= horizon):
                continue
            if home not in model["att"] or away not in model["att"]:
                continue
            games = model.get("n_games", {})
            if min(games.get(home, 0), games.get(away, 0)) < MIN_GAMES:
                continue
            p = bot.predict(model, home, away)
            for pick, prob in paper.botd_candidates(p):
                if prob >= LEG_MIN_PROB:
                    pool.append({"match_id": mid, "league": name, "home": home, "away": away, "utc": d,
                                 "pick": pick, "prob": prob, "odds": paper.make_odds(prob, MARGIN)})

    pool.sort(key=lambda x: -x["prob"])
    pool = pool[:POOL_SIZE]
    best, best_odds, best_prob = None, 0.0, 0.0
    for k in range(MIN_LEGS, MAX_LEGS + 1):
        for combo in itertools.combinations(pool, k):
            if len({l["match_id"] for l in combo}) < k:
                continue
            c_odds = round(math.prod(l["odds"] for l in combo), 2)
            if not (TARGET_ODDS_MIN <= c_odds <= TARGET_ODDS_MAX):
                continue
            c_prob = math.prod(l["prob"] for l in combo)
            if c_prob >= MIN_JOINT_PROB and c_prob > best_prob:
                best, best_odds, best_prob = combo, c_odds, c_prob
    if not best:
        return {}
    legs = sorted(best, key=lambda l: l["utc"])
    stake = round(run["pot"], 2)
    cur = con.execute("INSERT INTO multi_bets (run_id, stake, odds, prob, placed_at) VALUES (?,?,?,?,?)",
                      (rid, stake, best_odds, best_prob, now.isoformat()))
    con.executemany("INSERT INTO multi_legs (bet_id, match_id, pick, odds, prob) VALUES (?,?,?,?,?)",
                    [(cur.lastrowid, l["match_id"], l["pick"], l["odds"], l["prob"]) for l in legs])
    con.commit()
    return {"id": cur.lastrowid, "stake": stake, "odds": best_odds, "prob": best_prob, "legs": legs,
            "savings": run["savings"], "target": run["target"], "streak": run["streak"],
            "ends_at": run["ends_at"]}


# ---------- Kennzahlen ----------
def wins_needed(pot, savings, base, streak, target, odds=2.0):
    """Wie viele Treffer in Folge (bei durchschnittlicher Quote) noch bis zum Ziel, inkl. Sparregel."""
    n = 0
    while pot + savings < target and n < 100:
        pot *= odds
        streak += 1
        n += 1
        if streak >= SAVE_EVERY:
            gain = pot - base
            if gain > 0:
                pot -= gain * SAVE_SHARE
                savings += gain * SAVE_SHARE
            base, streak = pot, 0
    return n


def data(con):
    """Alles, was das Dashboard fuer den Multiplikator braucht."""
    runs = []
    for (rid,) in con.execute("SELECT id FROM multi_runs ORDER BY id DESC").fetchall():
        r = run_info(con, rid)
        bets = []
        for bid, stake, odds, prob, placed, status, profit, pot_after, saved in con.execute(
                "SELECT id, stake, odds, prob, placed_at, status, profit, pot_after, saved FROM multi_bets "
                "WHERE run_id=? ORDER BY id", (rid,)).fetchall():
            bets.append({"stake": stake, "odds": odds, "prob": prob, "placed": placed, "status": status,
                         "profit": profit, "pot_after": pot_after, "saved": saved or 0,
                         "legs": [{k: l[k] for k in ("league", "home", "away", "utc", "pick", "odds",
                                                       "prob", "state", "hg", "ag")}
                                  for l in leg_rows(con, bid)]})
        r["bets"] = bets
        r["wins"] = sum(b["status"] == "WON" for b in bets)
        if r["status"] == "ACTIVE":
            n = wins_needed(r["pot"], r["savings"], r["base"], r["streak"], r["target"])
            r["need"] = n
            r["chance"] = 0.45 ** n
        runs.append(r)
    return {"runs": runs, "defaults": {"start": DEFAULT_START, "target": DEFAULT_TARGET, "days": DEFAULT_DAYS},
            "rules": {"odds_min": TARGET_ODDS_MIN, "odds_max": TARGET_ODDS_MAX, "save_every": SAVE_EVERY,
                      "save_share": SAVE_SHARE}}


# ---------- Texte ----------
def _fr(v):
    return f"{v:,.2f}".replace(",", "'")


def build_text(placed, events, emoji=True):
    import paper
    LOCAL_TZ = paper.LOCAL_TZ
    mk = (lambda e, t: e) if emoji else (lambda e, t: t)
    lines = []
    for ev in events:
        if ev["type"] == "expired":
            lines.append(f"{mk('⏱', '[Zeit]')} Multiplikator: Zeit abgelaufen. Ziel {_fr(ev['target'])} Fr. "
                         f"nicht erreicht. Endstand {_fr(ev['pot'] + ev['savings'])} Fr. "
                         f"(Topf {_fr(ev['pot'])}, Sparkonto {_fr(ev['savings'])}).")
            continue
        mark = {"WON": mk("✅", "[OK]"), "LOST": mk("❌", "[--]"), "VOID": mk("↩️", "[ - ]")}[ev["status"]]
        lines.append(f"{mark} Multiplikator-Kombi @{ev['odds']:.2f}: {ev['profit']:+.2f} Fr.")
        for l in ev["legs"]:
            sm = {"WON": "ok", "LOST": "x", "VOID": "-", "OPEN": "offen"}[l["state"]]
            score = f"{l['hg']}:{l['ag']}" if l["hg"] is not None else "-"
            lines.append(f"   {l['home']} - {l['away']}: {paper.pick_label(l['pick'])} ({score}, {sm})")
        if ev["saved"]:
            lines.append(f"   {mk('🏦', '')} {_fr(ev['saved'])} Fr. aufs Sparkonto gelegt.")
        if ev["run_status"] == "LOST":
            lines.append(f"   Topf verloren. Sparkonto: {_fr(ev['savings'])} Fr. Versuch beendet, "
                         f"ein neuer kann im Dashboard gestartet werden.")
        elif ev["run_status"] == "REACHED":
            lines.append(f"   {mk('🏆', '')} ZIEL ERREICHT: {_fr(ev['pot'] + ev['savings'])} Fr. "
                         f"(Ziel {_fr(ev['target'])} Fr.)")
        else:
            lines.append(f"   Topf {_fr(ev['pot'])} Fr. · Sparkonto {_fr(ev['savings'])} Fr. · "
                         f"Ziel {_fr(ev['target'])} Fr.")
    if placed:
        lines.append(f"{mk('🚀 ', '')}Multiplikator: neue Kombi mit {len(placed['legs'])} Tipps, "
                     f"Einsatz {_fr(placed['stake'])} Fr. (ganzer Topf)")
        for i, l in enumerate(placed["legs"], 1):
            ko_dt = bot.parse_date(l["utc"]).astimezone(LOCAL_TZ)
            ko = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][ko_dt.weekday()] + ko_dt.strftime(" %H:%M")
            lines.append(f"{i}) {l['league']} · {ko}\n   {l['home']} – {l['away']}: "
                         f"{paper.pick_label(l['pick'])} (@{l['odds']:.2f}, {l['prob']:.0%})")
        lines.append(f"Kombi-Quote @{placed['odds']:.2f} · Modell gesamt {placed['prob']:.0%} · "
                     f"bei Treffer {_fr(placed['stake'] * placed['odds'])} Fr. im Topf")
        lines.append(f"Sparkonto {_fr(placed['savings'])} Fr. · Treffer seit letzter Sparrunde "
                     f"{placed['streak']}/{SAVE_EVERY} · Ziel {_fr(placed['target'])} Fr.")
    return "\n".join(lines)

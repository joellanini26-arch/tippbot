# Tippbot: daygame.py
"""
Tagesduell (Minigame im Dashboard): Jeden Tag ein Spiel, du gegen den Bot.

- Am Morgen (ab DAY_HOUR Uhr Schweizer Zeit) waehlt der Bot das spannendste Spiel des Tages:
  das mit den staerksten Teams laut Modell (Summe der Teamstaerken).
- Gleichzeitig legt er seinen Resultat-Tipp fest. Spiel und Bot-Tipp bleiben danach unveraendert.
- Den Tipp des Menschen speichert das Dashboard im Browser (localStorage).
"""
from datetime import datetime, timedelta, timezone

import bot

DAY_HOUR = 6            # ab dieser Uhrzeit (Schweizer Zeit) wird gewaehlt
MIN_LEAD_MINUTES = 30   # Anpfiff fruehestens so viele Minuten nach der Wahl
MIN_GAMES = 8           # beide Teams brauchen so viele Spiele in den Daten
HISTORY_DAYS = 90       # so viele Tage gehen ans Dashboard


def init(con):
    con.execute("""
    CREATE TABLE IF NOT EXISTS day_game (
        day TEXT PRIMARY KEY, match_id INTEGER, bot_score TEXT, bot_prob REAL, chosen_at TEXT
    )""")
    cols = [r[1] for r in con.execute("PRAGMA table_info(day_game)")]
    for col in ("bot_1x2", "bot_ou"):                  # Sieger- und Over/Under-Tipp des Bots
        if col not in cols:
            con.execute(f"ALTER TABLE day_game ADD COLUMN {col} TEXT")
    con.commit()


def bot_tips(p):
    """Tipps des Bots fuers Tagesduell: Sieger (1/X/2), Over/Under 2.5 (O/U), Resultat."""
    return p["tip_1x2"], ("O" if p["p_o25"] >= 0.5 else "U"), p["tip_score"], p["score_prob"]


def complete_today(con, now):
    """Spiel schon gewaehlt, aber ohne Sieger/Over-Under-Tipp (aeltere Version): vor Anpfiff nachtragen."""
    import paper
    day = now.astimezone(paper.LOCAL_TZ).strftime("%Y-%m-%d")
    row = con.execute("""SELECT g.match_id, m.league, m.home, m.away, m.utc_date FROM day_game g
                         JOIN matches m ON m.id=g.match_id WHERE g.day=? AND g.bot_1x2 IS NULL""", (day,)).fetchone()
    if not row or bot.parse_date(row[4]) <= now:
        return
    mid, league, home, away, _ = row
    try:
        p = bot.predict(paper.get_model(con, league), home, away)
    except (bot.NotEnoughData, KeyError):
        return
    w, ou, _, _ = bot_tips(p)
    con.execute("UPDATE day_game SET bot_1x2=?, bot_ou=? WHERE day=?", (w, ou, day))
    con.commit()


def pick(con, now=None):
    """Waehlt das Spiel des Tages, falls noch nicht geschehen. Gibt den neuen Eintrag zurueck oder None."""
    import paper
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(paper.LOCAL_TZ)
    if local.hour < DAY_HOUR:
        return None
    day = local.strftime("%Y-%m-%d")
    if con.execute("SELECT 1 FROM day_game WHERE day=?", (day,)).fetchone():
        complete_today(con, now)
        return None
    best = None
    for code in bot.LEAGUES:
        try:
            model = paper.get_model(con, code)
        except bot.NotEnoughData:
            continue
        games = model.get("n_games", {})
        for mid, d, home, away in con.execute(
                "SELECT id, utc_date, home, away FROM matches "
                "WHERE league=? AND status IN ('SCHEDULED','TIMED')", (code,)).fetchall():
            if d.endswith("T00:00:00Z"):
                continue
            ko = bot.parse_date(d)
            if ko.astimezone(paper.LOCAL_TZ).strftime("%Y-%m-%d") != day or ko < now + timedelta(minutes=MIN_LEAD_MINUTES):
                continue
            if home not in model["att"] or away not in model["att"]:
                continue
            if min(games.get(home, 0), games.get(away, 0)) < MIN_GAMES:
                continue
            strength = sum(model["att"][t] / model["dfn"][t] for t in (home, away))
            if not best or strength > best[0]:
                best = (strength, mid) + bot_tips(bot.predict(model, home, away))
    # Auch an Tagen ohne Spiel eintragen, damit nicht stuendlich neu gesucht wird
    _, mid, w, ou, score, prob = best or (None,) * 6
    con.execute("INSERT INTO day_game (day, match_id, bot_score, bot_prob, chosen_at, bot_1x2, bot_ou) "
                "VALUES (?,?,?,?,?,?,?)", (day, mid, score, prob, now.isoformat(), w, ou))
    con.commit()
    return {"day": day, "match_id": mid, "bot_score": score} if mid else None


def data(con, now=None):
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    out = []
    for day, mid, score, prob, w, ou, utc, league, home, away, status, hg, ag in con.execute("""
            SELECT g.day, g.match_id, g.bot_score, g.bot_prob, g.bot_1x2, g.bot_ou, m.utc_date, m.league,
                   m.home, m.away, m.status, m.home_goals, m.away_goals
            FROM day_game g JOIN matches m ON m.id = g.match_id
            WHERE g.day >= ? ORDER BY g.day""", (since,)):
        out.append({"day": day, "id": mid, "bot": score, "bot_w": w, "bot_ou": ou, "bot_prob": prob,
                    "utc": utc, "league": league,
                    "home": home, "away": away,
                    "status": "void" if status in ("POSTPONED", "CANCELLED", "SUSPENDED", "AWARDED")
                    else "done" if status == "FINISHED" and hg is not None else "open",
                    "hg": hg, "ag": ag})
    return out

"""
Tippbot Schritt 1+2: Daten holen, in SQLite speichern, Poisson-Modell, Tipps.
Unterstuetzt mehrere Ligen (siehe LEAGUES unten).

Start:   python bot.py            (Tipps fuer die naechsten 3 Tage)
         python bot.py --days 7   (naechste 7 Tage)
         python bot.py --full     (auch Vorsaison neu laden)
Benoetigt: pip install requests
Umgebungsvariable: FOOTBALLDATA_KEY
"""
import argparse
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tippbot.db")
API_URL = "https://api.football-data.org/v4/competitions/{code}/matches"

# Ligen: Code -> Name. Weitere Ligen einfach einkommentieren (alle im Gratis-Tarif).
LEAGUES = {
    "PL": "Premier League",
    "PD": "La Liga",
    "BL1": "Bundesliga",
    "SA": "Serie A",
    "FL1": "Ligue 1",
    # "ELC": "Championship",
    # "DED": "Eredivisie",
    # "PPL": "Primeira Liga",
}

REQUEST_PAUSE = 7        # Sekunden zwischen API-Aufrufen (Limit: 10 pro Minute)
HALF_LIFE_DAYS = 120     # Spiele werden mit der Zeit weniger wichtig
PRIOR_GAMES = 4          # Schrumpfung Richtung Durchschnitt (gegen Ausreisser)
MAX_GOALS = 8


class NotEnoughData(Exception):
    pass


def current_season():
    """Startjahr der laufenden Saison (ab Juli beginnt eine neue)."""
    now = datetime.now()
    return now.year if now.month >= 7 else now.year - 1


# ---------- Datenbank ----------
def init_db(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS matches (
        id INTEGER PRIMARY KEY,
        season INTEGER, matchday INTEGER,
        utc_date TEXT, status TEXT,
        home TEXT, away TEXT,
        home_goals INTEGER, away_goals INTEGER,
        league TEXT DEFAULT 'PL'
    );
    CREATE TABLE IF NOT EXISTS tips (
        match_id INTEGER PRIMARY KEY,
        created_at TEXT,
        p_home REAL, p_draw REAL, p_away REAL,
        exp_home REAL, exp_away REAL,
        tip_1x2 TEXT, tip_score TEXT
    );
    """)
    # Alte Datenbank (nur Premier League) erweitern
    cols = [r[1] for r in con.execute("PRAGMA table_info(matches)")]
    if "league" not in cols:
        con.execute("ALTER TABLE matches ADD COLUMN league TEXT DEFAULT 'PL'")
    con.commit()


# ---------- Datenabruf ----------
def _get(url, headers, season):
    r = requests.get(url, headers=headers, params={"season": season}, timeout=30)
    if r.status_code == 429:
        print("  Limit erreicht, warte 60 Sekunden ...")
        time.sleep(60)
        r = requests.get(url, headers=headers, params={"season": season}, timeout=30)
    return r


def fetch_matches(con, full=False):
    key = os.environ.get("FOOTBALLDATA_KEY")
    if not key:
        raise SystemExit("Umgebungsvariable FOOTBALLDATA_KEY ist nicht gesetzt.")
    headers = {"X-Auth-Token": key}
    season = current_season()
    calls = 0
    for code, name in LEAGUES.items():
        seasons = [season]
        have_prev = con.execute("SELECT COUNT(*) FROM matches WHERE league=? AND season=?",
                                (code, season - 1)).fetchone()[0]
        if full or not have_prev:
            seasons.insert(0, season - 1)   # Vorsaison nur einmal laden
        for s in seasons:
            if calls:
                time.sleep(REQUEST_PAUSE)
            calls += 1
            try:
                r = _get(API_URL.format(code=code), headers, s)
            except requests.RequestException as e:
                print(f"{name} {s}: Verbindungsfehler ({type(e).__name__}), uebersprungen")
                continue
            if r.status_code != 200:
                hint = " (nicht im Gratis-Tarif?)" if r.status_code == 403 else ""
                print(f"{name} {s}: API-Fehler {r.status_code}{hint}, uebersprungen")
                continue
            rows = []
            for m in r.json().get("matches", []):
                ft = m["score"]["fullTime"]
                rows.append((
                    m["id"], s, m.get("matchday"), m["utcDate"], m["status"],
                    m["homeTeam"].get("shortName") or m["homeTeam"]["name"],
                    m["awayTeam"].get("shortName") or m["awayTeam"]["name"],
                    ft["home"], ft["away"], code,
                ))
            con.executemany(
                "INSERT OR REPLACE INTO matches (id, season, matchday, utc_date, status, "
                "home, away, home_goals, away_goals, league) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            con.commit()
            print(f"{name} {s}: {len(rows)} Spiele gespeichert")


# ---------- Modell ----------
def parse_date(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fit_model(con, league="PL", now=None):
    """Angriffs-/Abwehrstaerke je Team einer Liga, Heimvorteil ueber Ligadurchschnitt."""
    now = now or datetime.now(timezone.utc)
    rows = con.execute(
        "SELECT utc_date, home, away, home_goals, away_goals FROM matches "
        "WHERE status='FINISHED' AND home_goals IS NOT NULL AND league=?", (league,)
    ).fetchall()
    if len(rows) < 30:
        raise NotEnoughData(f"Zu wenig Spieldaten fuer {league}.")

    games = []
    for d, h, a, hg, ag in rows:
        age = (now - parse_date(d)).days
        w = 0.5 ** (max(age, 0) / HALF_LIFE_DAYS)
        games.append((h, a, hg, ag, w))

    wsum = sum(g[4] for g in games)
    mu_home = sum(g[2] * g[4] for g in games) / wsum
    mu_away = sum(g[3] * g[4] for g in games) / wsum

    teams = {g[0] for g in games} | {g[1] for g in games}
    att = {t: 1.0 for t in teams}
    dfn = {t: 1.0 for t in teams}

    for _ in range(30):  # iterative Anpassung
        new_att, new_dfn = {}, {}
        for t in teams:
            sc = ex = 0.0   # erzielte Tore vs. erwartete (Angriff)
            co = exd = 0.0  # kassierte Tore vs. erwartete (Abwehr)
            for h, a, hg, ag, w in games:
                if h == t:
                    sc += w * hg;  ex += w * mu_home * dfn[a]
                    co += w * ag;  exd += w * mu_away * att[a]
                elif a == t:
                    sc += w * ag;  ex += w * mu_away * dfn[h]
                    co += w * hg;  exd += w * mu_home * att[h]
            new_att[t] = (sc + PRIOR_GAMES) / (ex + PRIOR_GAMES)
            new_dfn[t] = (co + PRIOR_GAMES) / (exd + PRIOR_GAMES)
        ma = sum(new_att.values()) / len(teams)
        md = sum(new_dfn.values()) / len(teams)
        att = {t: v / ma for t, v in new_att.items()}
        dfn = {t: v / md for t, v in new_dfn.items()}

    return {"att": att, "dfn": dfn, "mu_home": mu_home, "mu_away": mu_away}


def poisson(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def predict(model, home, away):
    att, dfn = model["att"], model["dfn"]
    lam_h = model["mu_home"] * att[home] * dfn[away]
    lam_a = model["mu_away"] * att[away] * dfn[home]

    grid = {}
    for i in range(MAX_GOALS + 1):
        for j in range(MAX_GOALS + 1):
            grid[(i, j)] = poisson(i, lam_h) * poisson(j, lam_a)
    total = sum(grid.values())
    grid = {k: v / total for k, v in grid.items()}

    p_home = sum(v for (i, j), v in grid.items() if i > j)
    p_draw = sum(v for (i, j), v in grid.items() if i == j)
    p_away = sum(v for (i, j), v in grid.items() if i < j)

    probs = {"1": p_home, "X": p_draw, "2": p_away}
    tip_1x2 = max(probs, key=probs.get)

    # Resultat-Tipp: wahrscheinlichster Endstand, der zum 1X2-Tipp passt
    def outcome(i, j):
        return "1" if i > j else ("X" if i == j else "2")
    best = max((k for k in grid if outcome(*k) == tip_1x2), key=lambda k: grid[k])

    def p_over(line):
        return sum(v for (i, j), v in grid.items() if i + j > line)

    return {
        "p_home": p_home, "p_draw": p_draw, "p_away": p_away,
        "exp_home": lam_h, "exp_away": lam_a,
        "tip_1x2": tip_1x2, "tip_score": f"{best[0]}:{best[1]}",
        "score_prob": grid[best],
        # Wahrscheinlichkeit fuer "Ueber X.5 Tore" (Under = 1 - Over)
        "p_o15": p_over(1.5), "p_o25": p_over(2.5), "p_o35": p_over(3.5),
    }


# ---------- Tipps ----------
def make_tips(con, days):
    now = datetime.now(timezone.utc)
    found = False
    for code, name in LEAGUES.items():
        try:
            model = fit_model(con, code)
        except NotEnoughData:
            print(f"\n{name}: zu wenig Daten, uebersprungen.")
            continue
        upcoming = con.execute(
            "SELECT id, utc_date, home, away FROM matches "
            "WHERE league=? AND status IN ('SCHEDULED','TIMED') ORDER BY utc_date", (code,)
        ).fetchall()
        upcoming = [m for m in upcoming
                    if 0 <= (parse_date(m[1]) - now).total_seconds() <= days * 86400]
        if not upcoming:
            continue
        found = True
        print(f"\n{name} - Tipps fuer die naechsten {days} Tage:\n")
        print(f"{'Datum':<12}{'Spiel':<36}{'1':>6}{'X':>6}{'2':>6}  Tipp  Resultat  Tore (Over 2.5)")
        print("-" * 98)
        for mid, d, home, away in upcoming:
            if home not in model["att"] or away not in model["att"]:
                continue
            p = predict(model, home, away)
            local = parse_date(d).astimezone()
            print(f"{local:%d.%m. %H:%M} {home + ' - ' + away:<35}"
                  f"{p['p_home']:>6.0%}{p['p_draw']:>6.0%}{p['p_away']:>6.0%}"
                  f"   {p['tip_1x2']}    {p['tip_score']:<9}"
                  f"{p['exp_home'] + p['exp_away']:.1f}  ({p['p_o25']:.0%})")
            con.execute(
                "INSERT OR REPLACE INTO tips VALUES (?,?,?,?,?,?,?,?,?)",
                (mid, now.isoformat(), p["p_home"], p["p_draw"], p["p_away"],
                 p["exp_home"], p["exp_away"], p["tip_1x2"], p["tip_score"]))
        con.commit()
    if not found:
        print(f"Keine Spiele in den naechsten {days} Tagen.")
    else:
        print("\nHinweis: Fussball ist zufallsgepraegt. Das sind Wahrscheinlichkeiten, keine Sicherheiten.")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")   # Sonderzeichen in Teamnamen sicher ausgeben
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--no-fetch", action="store_true", help="Keine neuen Daten holen")
    ap.add_argument("--full", action="store_true", help="Auch die Vorsaison neu laden")
    args = ap.parse_args()

    con = sqlite3.connect(DB_PATH)
    init_db(con)
    if not args.no_fetch:
        fetch_matches(con, full=args.full)
    make_tips(con, args.days)
    con.close()


if __name__ == "__main__":
    main()

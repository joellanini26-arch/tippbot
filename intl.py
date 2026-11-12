# Tippbot: intl.py
"""
Laenderspiele und Nations League aus dem offenen Datensatz
martj42/international_results (CC0): https://github.com/martj42/international_results

- Nations-League-Spiele (UEFA Nations League) werden als Liga "NL" gespeichert,
  kommende Spiele ohne Resultat erhalten den Status TIMED.
- Alle anderen Laenderspiele (Quali, Freundschaftsspiele, ...) werden als Liga "INT"
  nur als Trainingsdaten fuer die Teamstaerken gespeichert.
- Spiele auf neutralem Boden werden uebersprungen (das Modell kennt Heimvorteil).
- Der Datensatz enthaelt nur das Datum, keine Anstosszeit: Zeit wird als 00:00 UTC gespeichert.
"""
import csv
import hashlib
import io
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import requests

URL = "https://raw.githubusercontent.com/martj42/international_results/master/results.csv"
SINCE = "2022-01-01"
NL_NAME = "UEFA Nations League"

# Zusaetzlicher Spielplan mit Anstosszeiten (UTC), von Hand gepflegt: nl_fixtures.csv
FIXTURES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nl_fixtures.csv")
ALIASES = {"Türkiye": "Turkey", "Turkiye": "Turkey", "Czechia": "Czech Republic",
           "Ireland": "Republic of Ireland", "Bosnia-Herzegovina": "Bosnia and Herzegovina",
           "Macedonia": "North Macedonia"}


def _score(v):
    v = (v or "").strip()
    return int(v) if v.isdigit() else None


def _id(*parts):
    h = hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return 10 ** 12 + int(h, 16) % 10 ** 12   # weit weg von den IDs der API


def _fetch_dataset(con):
    try:
        r = requests.get(URL, timeout=60)
    except requests.RequestException as e:
        print(f"Nations League: Verbindungsfehler ({type(e).__name__}), uebersprungen")
        return
    if r.status_code != 200:
        print(f"Nations League: Datensatz nicht erreichbar (Status {r.status_code}), uebersprungen")
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows, n_nl, n_up = [], 0, 0
    for row in csv.DictReader(io.StringIO(r.text)):
        d = (row.get("date") or "").strip()
        if len(d) != 10 or d < SINCE:
            continue
        if (row.get("neutral") or "").strip().upper() == "TRUE":
            continue
        home = (row.get("home_team") or "").strip()
        away = (row.get("away_team") or "").strip()
        if not home or not away:
            continue
        hg, ag = _score(row.get("home_score")), _score(row.get("away_score"))
        is_nl = (row.get("tournament") or "").strip() == NL_NAME
        year, month = int(d[:4]), int(d[5:7])
        season = year if month >= 7 else year - 1
        if hg is not None and ag is not None:
            status = "FINISHED"
        elif is_nl:
            status = "TIMED"
            n_up += d >= today
        else:
            continue   # kommende Freundschaftsspiele etc. interessieren nicht
        if is_nl:
            league, mid = "NL", _id(home, away, str(season), "NL")
            n_nl += 1
        else:
            league, mid = "INT", _id(home, away, d)
        rows.append((mid, season, None, f"{d}T00:00:00Z", status, home, away, hg, ag, league))

    if not rows:
        print("Nations League: keine Daten im Datensatz gefunden")
        return
    # Ein Resultat, das schon da ist (z.B. von TheSportsDB), nicht durch "noch offen" ueberschreiben
    finished = {r[0] for r in con.execute("SELECT id FROM matches WHERE status='FINISHED'")}
    rows = [r for r in rows if not (r[4] != "FINISHED" and r[0] in finished)]
    con.executemany(
        "INSERT OR REPLACE INTO matches (id, season, matchday, utc_date, status, home, away, "
        "home_goals, away_goals, league) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    print(f"Nations League: {n_nl} NL-Spiele (davon {n_up} kommende), "
          f"{len(rows) - n_nl} weitere Laenderspiele als Trainingsdaten")


def _name(x):
    x = (x or "").strip()
    return ALIASES.get(x, x)


def load_manual_fixtures(con):
    """Spiele mit Anstosszeit aus nl_fixtures.csv (Spalten: date,time_utc,home_team,away_team).
    Schon beendete Spiele (Resultat aus dem Datensatz) werden nicht ueberschrieben."""
    if not os.path.exists(FIXTURES_FILE):
        return
    rows, teams = [], set()
    with open(FIXTURES_FILE, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            d = (row.get("date") or "").strip()
            home, away = _name(row.get("home_team")), _name(row.get("away_team"))
            try:
                hh, mm = (row.get("time_utc") or "00:00").strip().split(":")[:2]
                utc = f"{d}T{int(hh):02d}:{int(mm):02d}:00Z"
                year, month = int(d[:4]), int(d[5:7])
            except ValueError:
                continue
            if len(d) != 10 or not home or not away:
                continue
            season = year if month >= 7 else year - 1
            mid = _id(home, away, str(season), "NL")
            old = con.execute("SELECT status FROM matches WHERE id=?", (mid,)).fetchone()
            if old and old[0] == "FINISHED":
                continue
            rows.append((mid, season, None, utc, "TIMED", home, away, None, None, "NL"))
            teams.update((home, away))
    if not rows:
        return
    con.executemany(
        "INSERT OR REPLACE INTO matches (id, season, matchday, utc_date, status, home, away, "
        "home_goals, away_goals, league) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    known = {r[0] for r in con.execute(
        "SELECT home FROM matches WHERE status='FINISHED' AND league IN ('NL','INT') "
        "UNION SELECT away FROM matches WHERE status='FINISHED' AND league IN ('NL','INT')")}
    missing = sorted(teams - known)
    print(f"Nations League: {len(rows)} Spiele mit Anstosszeit aus nl_fixtures.csv geladen")
    if missing:
        print("  Achtung, Teamnamen ohne Spielhistorie im Datensatz (werden nicht getippt): "
              + ", ".join(missing))


def reconcile(con):
    """Falls der Datensatz ein Resultat unter leicht anderem Datum/ID fuehrt: auf den Spielplan-Eintrag
    uebertragen, damit die Wetten abgerechnet werden."""
    rows = con.execute("""
        SELECT t.id, f.id, f.home_goals, f.away_goals FROM matches t JOIN matches f
          ON f.league='NL' AND f.status='FINISHED' AND f.home=t.home AND f.away=t.away AND f.id != t.id
         AND ABS(julianday(substr(f.utc_date,1,10)) - julianday(substr(t.utc_date,1,10))) <= 3
        WHERE t.league='NL' AND t.status='TIMED' AND f.home_goals IS NOT NULL""").fetchall()
    for tid, fid, hg, ag in rows:
        con.execute("UPDATE matches SET status='FINISHED', home_goals=?, away_goals=? WHERE id=?",
                    (hg, ag, tid))
        con.execute("DELETE FROM matches WHERE id=?", (fid,))
    if rows:
        con.commit()


# ---------- Schnelle Resultate von TheSportsDB ----------
# Der offene Datensatz wird von Hand und oft erst Tage spaeter nachgefuehrt. Damit Wetten auf
# Nations-League-Spiele kurz nach Spielende abgerechnet werden, holt der Bot die Resultate zusaetzlich
# von TheSportsDB (offizielle Gratis-Schnittstelle, oeffentlicher Schluessel "123", 30 Abfragen/Min.).
SPORTSDB_URL = "https://www.thesportsdb.com/api/v1/json/{key}/{endpoint}"
SPORTSDB_NL_ID = 4490                      # UEFA Nations League bei TheSportsDB
SPORTSDB_FINAL = {"match finished", "ft", "finished"}            # nach 90 Minuten entschieden
SPORTSDB_VOID = {"aet", "pen", "after extra time", "after penalties", "postponed", "match postponed",
                 "cancelled", "canceled", "match cancelled", "abandoned", "match abandoned"}
RESULT_DAYS_BACK = 7


def _norm(name):
    name = _name(name)
    name = unicodedata.normalize("NFD", name).encode("ascii", "ignore").decode().lower()
    return " ".join(w for w in re.sub(r"[^a-z0-9]+", " ", name).split() if w not in ("fc", "the"))


def _same(a, b):
    a, b = _norm(a), _norm(b)
    return a == b or SequenceMatcher(None, a, b).ratio() >= 0.85


def _sportsdb(endpoint, params):
    key = os.environ.get("SPORTSDB_KEY") or "123"
    try:
        r = requests.get(SPORTSDB_URL.format(key=key, endpoint=endpoint), params=params, timeout=30)
        if r.status_code != 200:
            print(f"TheSportsDB {endpoint}: Status {r.status_code}")
            return []
        j = r.json() or {}
        events = j.get("events") or j.get("event") or j.get("results") or []
    except (requests.RequestException, ValueError) as e:
        print(f"TheSportsDB {endpoint}: Fehler ({type(e).__name__})")
        return []
    out = []
    for ev in events:
        def score(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        out.append({"home": ev.get("strHomeTeam") or "", "away": ev.get("strAwayTeam") or "",
                    "league": ev.get("strLeague") or "", "hg": score(ev.get("intHomeScore")),
                    "ag": score(ev.get("intAwayScore")), "status": (ev.get("strStatus") or "").strip().lower()})
    return out


def _sportsdb_day(day):
    return _sportsdb("eventsday.php", {"d": day.isoformat(), "s": "Soccer"})


def _sportsdb_last_nl():
    return _sportsdb("eventspastleague.php", {"id": SPORTSDB_NL_ID})


def _sportsdb_search(home, away, day):
    found = []
    for h, a in {(home, away), (_display(home), _display(away))}:
        found += _sportsdb("searchevents.php", {"e": f"{h}_vs_{a}".replace(" ", "_"), "d": day.isoformat()})
        if found:
            break
    return found


DISPLAY = {"Turkey": "Türkiye", "Czech Republic": "Czechia"}


def _display(name):
    return DISPLAY.get(name, name)


def online_results(con, now=None):
    """Traegt Resultate fuer Nations-League-Spiele ein, die schon angepfiffen sind."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=RESULT_DAYS_BACK)).strftime("%Y-%m-%d")
    open_games = con.execute(
        "SELECT id, utc_date, home, away FROM matches WHERE league='NL' AND status IN ('TIMED','SCHEDULED') "
        "AND utc_date >= ? AND utc_date < ?", (since, now.strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchall()
    if not open_games:
        return
    days = sorted({datetime.strptime(d[:10], "%Y-%m-%d").date() for _, d, _, _ in open_games})
    events = _sportsdb_last_nl()                          # letzte Spiele der Nations League
    for day in days:
        events += _sportsdb_day(day)
    events = [e for e in events if "nations" in e["league"].lower() or not e["league"]]
    n_done = n_void = 0
    for mid, d, home, away in open_games:
        e = next((x for x in events if _same(home, x["home"]) and _same(away, x["away"])), None)
        if not e:                                          # einzeln suchen
            day = datetime.strptime(d[:10], "%Y-%m-%d").date()
            e = next((x for x in _sportsdb_search(home, away, day)
                      if _same(home, x["home"]) and _same(away, x["away"])), None)
            if e:
                events.append(e)
        if not e:
            continue
        if e["status"] in SPORTSDB_VOID:
            con.execute("UPDATE matches SET status='CANCELLED' WHERE id=?", (mid,))
            n_void += 1
        elif e["hg"] is not None and e["ag"] is not None and (
                e["status"] in SPORTSDB_FINAL
                or (not e["status"] and now - bot_parse(d) > timedelta(hours=3))):
            con.execute("UPDATE matches SET status='FINISHED', home_goals=?, away_goals=? WHERE id=?",
                        (e["hg"], e["ag"], mid))
            n_done += 1
    con.commit()
    missing = len(open_games) - n_done - n_void
    print(f"Nations League (TheSportsDB): {n_done} Resultate eingetragen, {n_void} ungueltig, "
          f"{missing} noch ohne Resultat ({len(events)} Spiele geliefert)")
    if missing:   # zur Kontrolle: was geliefert wurde, aber nicht passte
        for e in events[:30]:
            print(f"  geliefert: {e['home']} - {e['away']} {e['hg']}:{e['ag']} [{e['status']}] {e['league']}")


def bot_parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fetch(con):
    _fetch_dataset(con)          # Resultate (und evtl. Spielplan) aus dem offenen Datensatz
    load_manual_fixtures(con)    # Spielplan mit Anstosszeiten
    reconcile(con)
    online_results(con)          # schnelle Resultate fuer bereits gespielte Spiele

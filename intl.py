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
from datetime import datetime, timezone

import requests

URL = "https://raw.githubusercontent.com/martj42/international_results/master/results.csv"
SINCE = "2022-01-01"
NL_NAME = "UEFA Nations League"


def _score(v):
    v = (v or "").strip()
    return int(v) if v.isdigit() else None


def _id(*parts):
    h = hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return 10 ** 12 + int(h, 16) % 10 ** 12   # weit weg von den IDs der API


def fetch(con):
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
    con.executemany(
        "INSERT OR REPLACE INTO matches (id, season, matchday, utc_date, status, home, away, "
        "home_goals, away_goals, league) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    print(f"Nations League: {n_nl} NL-Spiele (davon {n_up} kommende), "
          f"{len(rows) - n_nl} weitere Laenderspiele als Trainingsdaten")

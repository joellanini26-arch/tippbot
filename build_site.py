# Tippbot: build_site.py
"""
Erzeugt das Handy-Dashboard als einzelne statische Seite: site/index.html
Liest nur aus tippbot.db. Laeuft in GitHub Actions nach jedem Bot-Lauf,
funktioniert aber auch lokal:  python build_site.py
"""
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

import bot
import calib
import duel
import multi
import paper

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "site")
STATIC_DIR = os.path.join(HERE, "static")   # App-Symbole, Manifest, Service Worker
UPCOMING_DAYS = 14
MAX_BETS = 300


# Landesflaggen fuer Nationalteams (ISO-Code fuer flagcdn.com)
FLAGS = {
    "Albania": "al", "Andorra": "ad", "Armenia": "am", "Austria": "at", "Azerbaijan": "az", "Belarus": "by",
    "Belgium": "be", "Bosnia and Herzegovina": "ba", "Bulgaria": "bg", "Croatia": "hr", "Cyprus": "cy",
    "Czech Republic": "cz", "Denmark": "dk", "England": "gb-eng", "Estonia": "ee", "Faroe Islands": "fo",
    "Finland": "fi", "France": "fr", "Georgia": "ge", "Germany": "de", "Gibraltar": "gi", "Greece": "gr",
    "Hungary": "hu", "Iceland": "is", "Israel": "il", "Italy": "it", "Kazakhstan": "kz", "Kosovo": "xk",
    "Latvia": "lv", "Liechtenstein": "li", "Lithuania": "lt", "Luxembourg": "lu", "Malta": "mt",
    "Moldova": "md", "Montenegro": "me", "Netherlands": "nl", "North Macedonia": "mk",
    "Northern Ireland": "gb-nir", "Norway": "no", "Poland": "pl", "Portugal": "pt",
    "Republic of Ireland": "ie", "Romania": "ro", "San Marino": "sm", "Scotland": "gb-sct", "Serbia": "rs",
    "Slovakia": "sk", "Slovenia": "si", "Spain": "es", "Sweden": "se", "Switzerland": "ch", "Turkey": "tr",
    "Ukraine": "ua", "Wales": "gb-wls",
}


def crest_map(con):
    """Teamname -> Bild-URL (Vereinswappen von football-data.org, Flaggen fuer Nationalteams).
    Auch unter dem englischen ESPN-Namen, den Duell und Live-Anzeige verwenden."""
    out = {team: url for team, url in con.execute("SELECT team, url FROM crests") if url}
    out.update({team: f"https://flagcdn.com/w80/{iso}.png" for team, iso in FLAGS.items()})
    for team, url in list(out.items()):
        out.setdefault(duel.espn(team), url)
    return out


def build_data(con):
    now = datetime.now(timezone.utc)
    season = bot.current_season()
    data = {
        "generated": now.isoformat(),
        "start_balance": paper.START_BALANCE,
        "thresholds": {"1X2": paper.MIN_PROB_1X2, "OU": paper.MIN_PROB_OU, "SCORE": paper.MIN_PROB_SCORE},
        "ou_line": paper.OU_LINE,
        "leagues": bot.LEAGUES,
        "no_table": sorted(bot.NO_TABLE),
        "upcoming": [], "tables": {}, "strength": {},
    }

    odds, stakes = {}, {}
    for mid, kind, o, st in con.execute("SELECT match_id, kind, odds, stake FROM bets"):
        odds.setdefault(mid, {})[kind] = o
        stakes.setdefault(mid, {})[kind] = st
    fac = calib.factors(con)
    data["calib"] = {"factors": fac, "buckets": calib.buckets(con), "prior": calib.PRIOR}

    over_key = f"p_o{int(paper.OU_LINE * 10)}"
    for code in bot.LEAGUES:
        # Tabelle der laufenden Saison
        rows = con.execute(
            "SELECT home, away, home_goals, away_goals FROM matches "
            "WHERE league=? AND season=? AND status='FINISHED' AND home_goals IS NOT NULL",
            (code, season)).fetchall()
        t = {}
        for h, a, hg, ag in rows:
            for team, gf, ga in ((h, hg, ag), (a, ag, hg)):
                r = t.setdefault(team, dict(team=team, sp=0, s=0, u=0, n=0, gf=0, ga=0))
                r["sp"] += 1; r["gf"] += gf; r["ga"] += ga
                r["s" if gf > ga else "u" if gf == ga else "n"] += 1
        table = list(t.values())
        for r in table:
            r["td"] = r["gf"] - r["ga"]
            r["pkt"] = r["s"] * 3 + r["u"]
        table.sort(key=lambda r: (-r["pkt"], -r["td"], -r["gf"]))
        data["tables"][code] = table

        # Modell
        try:
            model = bot.fit_model(con, code)
        except bot.NotEnoughData:
            continue
        current = {r[0] for r in con.execute(
            "SELECT home FROM matches WHERE league=? AND season=? UNION "
            "SELECT away FROM matches WHERE league=? AND season=?",
            (code, season, code, season))} or set(model["att"])
        data["strength"][code] = sorted(
            [{"team": tm, "att": model["att"][tm], "dfn": model["dfn"][tm],
              "total": model["att"][tm] / model["dfn"][tm]} for tm in model["att"] if tm in current],
            key=lambda r: -r["total"])

        # Kommende Spiele
        up = con.execute(
            "SELECT id, utc_date, home, away FROM matches WHERE league=? "
            "AND status IN ('SCHEDULED','TIMED') ORDER BY utc_date", (code,)).fetchall()
        for mid, d, home, away in up:
            secs = (bot.parse_date(d) - now).total_seconds()
            if not (0 <= secs <= UPCOMING_DAYS * 86400):
                continue
            if home not in model["att"] or away not in model["att"]:
                continue
            p = bot.predict(model, home, away)
            games = model.get("n_games", {})
            enough = min(games.get(home, 0), games.get(away, 0)) >= paper.MIN_GAMES
            sel = [[k, pk, pr, pc, stakes.get(mid, {}).get(k, st)]
                   for k, pk, pr, pc, st in paper.selected_bets(p, fac)] if enough else []
            data["upcoming"].append({
                "sel": sel,
                "id": mid, "league": code, "utc": d, "home": home, "away": away,
                "p1": p["p_home"], "px": p["p_draw"], "p2": p["p_away"],
                "eh": p["exp_home"], "ea": p["exp_away"],
                "tip": p["tip_1x2"], "score": p["tip_score"], "po": p[over_key],
                "odds": odds.get(mid, {}),
            })
    data["upcoming"].sort(key=lambda r: r["utc"])

    # Abgerechnete Wetten, pro Tag/Liga/Art zusammengefasst
    data["daily"] = [
        {"day": r[0], "league": r[1], "kind": r[2], "n": r[3] or 0, "won": r[4] or 0,
         "staked": r[5] or 0, "profit": r[6] or 0}
        for r in con.execute("""
            SELECT substr(m.utc_date,1,10), m.league, b.kind,
                   SUM(b.status IN ('WON','LOST')), SUM(b.status='WON'),
                   SUM(CASE WHEN b.status IN ('WON','LOST') THEN b.stake ELSE 0 END),
                   SUM(COALESCE(b.profit,0))
            FROM bets b JOIN matches m ON m.id=b.match_id
            WHERE b.status != 'OPEN'
            GROUP BY 1,2,3 ORDER BY 1""")]
    data["open"] = [
        {"league": r[0], "n": r[1], "stake": r[2] or 0}
        for r in con.execute("""
            SELECT m.league, COUNT(*), SUM(b.stake)
            FROM bets b JOIN matches m ON m.id=b.match_id
            WHERE b.status='OPEN' GROUP BY m.league""")]
    # Bet of the day (Kombiwetten)
    def combo_legs(cid):
        legs = []
        for mid, pick, lodds, prob, home, away, utc, league, hg, ag, mstatus in con.execute("""
                SELECT l.match_id, l.pick, l.odds, l.prob, m.home, m.away, m.utc_date, m.league,
                       m.home_goals, m.away_goals, m.status
                FROM combo_legs l JOIN matches m ON m.id=l.match_id
                WHERE l.combo_id=? ORDER BY m.utc_date""", (cid,)).fetchall():
            if mstatus in ("POSTPONED", "CANCELLED"):
                state = "VOID"
            elif mstatus == "FINISHED" and hg is not None:
                state = "WON" if paper.is_won("BOTD", pick, hg, ag) else "LOST"
            else:
                state = "OPEN"
            legs.append({"league": league, "home": home, "away": away, "utc": utc, "pick": pick,
                         "odds": lodds, "prob": prob, "state": state,
                         "hg": hg, "ag": ag})
        return legs

    combos = []
    for cid, day, stake, odds, prob, status, profit in con.execute(
            "SELECT id, day, stake, odds, prob, status, profit FROM combos ORDER BY id DESC LIMIT 60"):
        legs = combo_legs(cid)
        combos.append({"day": day, "stake": stake, "odds": odds, "prob": prob, "status": status,
                       "profit": profit, "legs": legs, "league": "MIX",
                       "utc": legs[0]["utc"] if legs else day + "T00:00:00Z"})
    data["botd"] = combos[0] if combos else None
    for c in con.execute(
            "SELECT day, status, COUNT(*), SUM(stake), SUM(COALESCE(profit,0)) FROM combos "
            "WHERE status!='OPEN' GROUP BY day, status"):
        settled = c[1] in ("WON", "LOST")
        data["daily"].append({"day": c[0], "league": "MIX", "kind": "BOTD",
                              "n": c[2] if settled else 0, "won": c[2] if c[1] == "WON" else 0,
                              "staked": (c[3] or 0) if settled else 0, "profit": c[4] or 0})
    data["daily"].sort(key=lambda r: r["day"])
    n_open, stake_open = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(stake),0) FROM combos WHERE status='OPEN'").fetchone()
    if n_open:
        data["open"].append({"league": "MIX", "n": n_open, "stake": stake_open})
    data["bets"] = [
        {"league": r[0], "utc": r[1], "home": r[2], "away": r[3], "kind": r[4], "pick": r[5],
         "stake": r[6], "odds": r[7], "status": r[8], "profit": r[9], "hg": r[10], "ag": r[11]}
        for r in con.execute("""
            SELECT m.league, m.utc_date, m.home, m.away, b.kind, b.pick, b.stake, b.odds,
                   b.status, b.profit, m.home_goals, m.away_goals
            FROM bets b JOIN matches m ON m.id=b.match_id
            ORDER BY m.utc_date DESC, b.match_id, b.kind LIMIT ?""", (MAX_BETS,))]
    for c in combos:
        data["bets"].append({"league": "MIX", "utc": c["utc"], "home": "Kombi", "away": "",
                             "kind": "BOTD", "pick": "", "stake": c["stake"], "odds": c["odds"],
                             "status": c["status"], "profit": c["profit"], "hg": None, "ag": None,
                             "legs": c["legs"]})
    data["bets"].sort(key=lambda b: b["utc"], reverse=True)
    data["bets"] = data["bets"][:MAX_BETS]
    data["multi"] = multi.data(con)
    data["repo"] = os.environ.get("GITHUB_REPOSITORY", "joellanini26-arch/tippbot")
    return data


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    con = sqlite3.connect(bot.DB_PATH)
    bot.init_db(con)
    paper.init_bets(con)
    multi.init(con)
    duel.init(con)
    data = build_data(con)
    duel_data = duel.duel_json(con)
    data["duel"] = duel_data["challenge"]
    data["duel_partner"] = duel.PARTNER_URL
    data["espn_names"] = duel.ESPN_NAMES          # fuer die Live-Resultate im Browser
    data["crests"] = crest_map(con)
    con.close()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.replace("__DATA__", payload)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    # Tippduell: oeffentliche Datei fuer den Vergleich mit GoalOracle (Format tippduell/1)
    with open(os.path.join(OUT_DIR, "duel.json"), "w", encoding="utf-8") as f:
        json.dump(duel_data, f, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT_DIR, "duel-config.json"), "w", encoding="utf-8") as f:
        json.dump({"partner_url": duel.PARTNER_URL, "updated": duel_data["updated"]}, f)
    if os.path.isdir(STATIC_DIR):
        shutil.copytree(STATIC_DIR, OUT_DIR, dirs_exist_ok=True)
    print(f"Dashboard geschrieben: {len(html) // 1024} KB, "
          f"{len(data['upcoming'])} kommende Spiele, {len(data['bets'])} Wetten")


with open(os.path.join(HERE, "dashboard.html"), encoding="utf-8") as _f:
    TEMPLATE = _f.read()          # Vorlage des Dashboards (HTML/CSS/JS), __DATA__ wird ersetzt


if __name__ == "__main__":
    main()

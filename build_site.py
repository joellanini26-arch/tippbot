# Tippbot: build_site.py
"""
Erzeugt das Handy-Dashboard als einzelne statische Seite: site/index.html
Liest nur aus tippbot.db. Laeuft in GitHub Actions nach jedem Bot-Lauf,
funktioniert aber auch lokal:  python build_site.py
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

import bot
import multi
import paper

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site")
UPCOMING_DAYS = 14
MAX_BETS = 300


def build_data(con):
    now = datetime.now(timezone.utc)
    season = bot.current_season()
    data = {
        "generated": now.isoformat(),
        "start_balance": paper.START_BALANCE,
        "ou_line": paper.OU_LINE,
        "leagues": bot.LEAGUES,
        "no_table": sorted(bot.NO_TABLE),
        "upcoming": [], "tables": {}, "strength": {},
    }

    odds = {}
    for mid, kind, o in con.execute("SELECT match_id, kind, odds FROM bets"):
        odds.setdefault(mid, {})[kind] = o

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
            data["upcoming"].append({
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
    data = build_data(con)
    con.close()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.replace("__DATA__", payload)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Dashboard geschrieben: {len(html) // 1024} KB, "
          f"{len(data['upcoming'])} kommende Spiele, {len(data['bets'])} Wetten")


TEMPLATE = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f172a">
<title>Tippbot</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%E2%9A%BD%3C/text%3E%3C/svg%3E">
<style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#111827;--mute:#6b7280;--line:#e5e7eb;--acc:#2563eb;--pos:#15803d;--neg:#b91c1c;--chip:#eef2ff;--b1:#2563eb;--bx:#9ca3af;--b2:#f59e0b}
@media (prefers-color-scheme:dark){:root{--bg:#0b1020;--card:#151b2e;--fg:#e5e7eb;--mute:#94a3b8;--line:#263048;--acc:#60a5fa;--pos:#4ade80;--neg:#f87171;--chip:#1e2a4a;--b1:#60a5fa;--bx:#64748b;--b2:#fbbf24}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 12px 0}
h1{font-size:18px;margin:0 0 2px}
.sub{color:var(--mute);font-size:12px;margin-bottom:8px}
.row{display:flex;gap:8px;align-items:center}
select{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 8px;max-width:100%}
nav{display:flex;gap:2px;overflow-x:auto;margin-top:8px;scrollbar-width:none}
nav button{flex:1 0 auto;font:inherit;color:var(--mute);background:none;border:0;border-bottom:2px solid transparent;padding:8px 12px;cursor:pointer}
nav button.on{color:var(--acc);border-color:var(--acc);font-weight:600}
main{padding:12px;max-width:820px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:10px}
.meta{display:flex;justify-content:space-between;color:var(--mute);font-size:12px}
.teams{font-weight:600;margin:4px 0 8px}
.bar{display:flex;height:8px;border-radius:4px;overflow:hidden;background:var(--line)}
.bar i{display:block;height:100%}
.b1{background:var(--b1)}.bx{background:var(--bx)}.b2{background:var(--b2)}
.probs{display:flex;justify-content:space-between;font-size:12px;color:var(--mute);margin:4px 0 8px}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{background:var(--chip);border-radius:999px;padding:3px 10px;font-size:13px}
.chip b{font-weight:700}
.chip small{color:var(--mute)}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:10px}
@media(min-width:560px){.grid{grid-template-columns:repeat(3,1fr)}}
.metric{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px}
.metric span{display:block;color:var(--mute);font-size:12px}
.metric b{font-size:20px}
.pos{color:var(--pos)}.neg{color:var(--neg)}
.empty{color:var(--mute);text-align:center;padding:24px 8px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:6px 6px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th:nth-child(2),td:nth-child(2){text-align:left;width:100%}
th:first-child,td:first-child{text-align:right;color:var(--mute)}
th{color:var(--mute);font-weight:500;font-size:12px}
.scroll{overflow-x:auto}
.seg{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap}
.seg button{font:inherit;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:999px;padding:4px 12px;cursor:pointer}
.seg button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.bet{display:flex;justify-content:space-between;gap:8px;align-items:center}
.bet .l{min-width:0}.bet .t{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bet .s{color:var(--mute);font-size:12px}
.bet .r{text-align:right;white-space:nowrap}
.badge{font-size:11px;border-radius:6px;padding:1px 6px;background:var(--line);color:var(--mute)}
.note{color:var(--mute);font-size:12px;margin:8px 0}
svg text{fill:var(--mute);font-size:11px}
.prog{height:10px;border-radius:5px;background:var(--line);overflow:hidden;margin:6px 0}
.prog i{display:block;height:100%;background:var(--acc)}
.form{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:8px 0}
.form label{font-size:12px;color:var(--mute);display:flex;flex-direction:column;gap:4px;min-width:0}
.form input{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:6px 8px;width:100%;min-width:0}
.btn{font:inherit;font-weight:600;background:var(--acc);color:#fff;border:0;border-radius:8px;padding:8px 14px;cursor:pointer}
.btn:disabled{opacity:.6;cursor:default}
.link{font:inherit;font-size:12px;background:none;border:0;color:var(--acc);padding:0;cursor:pointer;text-decoration:underline}
h2{font-size:15px;margin:16px 0 8px}
</style>
</head>
<body>
<header>
  <h1>⚽ Tippbot</h1>
  <div class="sub" id="stand"></div>
  <div class="row"><select id="lg" aria-label="Liga"></select></div>
  <nav id="tabs"></nav>
</header>
<main id="view"></main>
<script>
const D = __DATA__;
const TABS = [["tips","Tipps"],["multi","Multiplikator"],["konto","Konto"],["wetten","Wetten"],["tabelle","Tabelle"],["modell","Modell"]];
const KIND = {"1X2":"Tipp","SCORE":"Resultat","OU":"Over/Under","BOTD":"Bet of the day"};
const PICKS = {"1":"Heimsieg","2":"Auswärtssieg","1X":"Heimsieg oder Unentschieden","X2":"Auswärtssieg oder Unentschieden"};
const pickLabel = p => p.startsWith("Over") ? "Über " + p.split(" ")[1] + " Tore" : p.startsWith("Under") ? "Unter " + p.split(" ")[1] + " Tore" : (PICKS[p] || p);
const STATUS = {OPEN:"offen",WON:"gewonnen",LOST:"verloren",VOID:"storniert"};
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const dt = iso => iso.endsWith("T00:00:00Z")
  ? new Date(iso).toLocaleDateString("de-CH",{timeZone:"UTC",weekday:"short",day:"2-digit",month:"2-digit"})
  : new Date(iso).toLocaleString("de-CH",{timeZone:"Europe/Zurich",weekday:"short",day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"});
const pct = v => v == null ? "–" : Math.round(v*100) + "%";
const chf = v => (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(2);
const cls = v => v > 0 ? "pos" : v < 0 ? "neg" : "";
const lname = c => D.leagues[c] || c;
let store = {}; try { store = window.localStorage; } catch (e) {}
const get = (k, d) => { try { return store.getItem(k) || d; } catch (e) { return d; } };
const set = (k, v) => { try { store.setItem(k, v); } catch (e) {} };
let state = {league: get("league","ALL"), tab: (location.hash || "").slice(1) || "tips", kind: "ALL"};
if (state.league !== "ALL" && !D.leagues[state.league]) state.league = "ALL";
if (!TABS.some(t => t[0] === state.tab)) state.tab = "tips";
const inLeague = r => state.league === "ALL" || r.league === state.league;
const oneLeague = () => state.league !== "ALL" ? state.league : Object.keys(D.leagues)[0];

const LEGMARK = {WON: "✅", LOST: "❌", VOID: "↩️", OPEN: "⏳"};
function botdCard() {
  const b = D.botd;
  if (!b || !inLeague(b)) return "";
  const open = b.status === "OPEN";
  const res = open ? `<span class="badge">offen</span>` : `<b class="${cls(b.profit)}">${chf(b.profit || 0)} CHF</b> · ${STATUS[b.status]}`;
  const legs = b.legs.map(l => `<div class="s">${LEGMARK[l.state]} ${esc(l.home)} – ${esc(l.away)}: <b>${esc(pickLabel(l.pick))}</b> <small>@${l.odds.toFixed(2)} · ${pct(l.prob)}</small>${l.hg != null ? " · " + l.hg + ":" + l.ag : ""}</div>`).join("");
  return `<div class="card" style="border-color:var(--acc)">
    <div class="meta"><span>⭐ Bet of the day · Kombi mit ${b.legs.length} Tipps</span><span>${dt(b.utc)}</span></div>
    <div style="margin:6px 0">${legs}</div>
    <div class="chips"><span class="chip">Kombi-Quote <b>@${b.odds.toFixed(2)}</b></span>
      <span class="chip">Modell gesamt <b>${pct(b.prob)}</b></span>
      <span class="chip">Einsatz <b>${b.stake.toFixed(0)}</b> CHF</span></div>
    <div class="probs"><span>${res}</span></div></div>`;
}

function tips() {
  const rows = D.upcoming.filter(inLeague);
  const top = botdCard();
  if (!rows.length) return top + '<div class="empty">Keine kommenden Spiele gefunden.</div>';
  const line = D.ou_line;
  return top + rows.map(r => {
    const o = r.odds || {};
    const q = k => o[k] ? ` <small>@${o[k].toFixed(2)}</small>` : "";
    const over = r.po >= 0.5;
    return `<div class="card">
      <div class="meta"><span>${esc(lname(r.league))}</span><span>${dt(r.utc)}</span></div>
      <div class="teams">${esc(r.home)} – ${esc(r.away)}</div>
      <div class="bar"><i class="b1" style="width:${r.p1*100}%"></i><i class="bx" style="width:${r.px*100}%"></i><i class="b2" style="width:${r.p2*100}%"></i></div>
      <div class="probs"><span>1 · ${pct(r.p1)}</span><span>X · ${pct(r.px)}</span><span>2 · ${pct(r.p2)}</span></div>
      <div class="chips">
        <span class="chip">Tipp <b>${r.tip}</b>${q("1X2")}</span>
        <span class="chip">Resultat <b>${esc(r.score)}</b>${q("SCORE")}</span>
        <span class="chip">${over ? "Over" : "Under"} ${line} <b>${pct(over ? r.po : 1 - r.po)}</b>${q("OU")}</span>
        <span class="chip">Ø Tore <b>${(r.eh + r.ea).toFixed(1)}</b></span>
      </div></div>`;
  }).join("") + '<div class="note">Wahrscheinlichkeiten aus dem Poisson-Modell. Quoten erscheinen, sobald die Fake-Wette platziert wurde.</div>';
}

function konto() {
  const days = D.daily.filter(inLeague);
  const open = D.open.filter(inLeague).reduce((a, r) => ({n: a.n + r.n, stake: a.stake + r.stake}), {n: 0, stake: 0});
  const total = days.reduce((a, r) => a + r.profit, 0);
  const staked = days.reduce((a, r) => a + r.staked, 0);
  const hit = k => { const x = days.filter(r => r.kind === k); const n = x.reduce((a, r) => a + r.n, 0); const w = x.reduce((a, r) => a + r.won, 0); return [n, n ? w / n : null]; };
  const [n1, h1] = hit("1X2"), [n2, h2] = hit("SCORE"), [n3, h3] = hit("OU"), [n4, h4] = hit("BOTD");
  const byDay = {};
  days.forEach(r => byDay[r.day] = (byDay[r.day] || 0) + r.profit);
  let bal = D.start_balance; const pts = [["Start", bal]];
  Object.keys(byDay).sort().forEach(d => { bal += byDay[d]; pts.push([d, bal]); });
  const m = (l, v, c) => `<div class="metric"><span>${l}</span><b class="${c || ""}">${v}</b></div>`;
  return `<div class="grid">
    ${m("Kontostand", (D.start_balance + total).toFixed(2) + " CHF")}
    ${m("Gewinn/Verlust", chf(total) + " CHF", cls(total))}
    ${m("Offene Wetten", open.n + " (" + open.stake.toFixed(0) + " CHF)")}
    ${m(`Treffer Tipp (${n1})`, pct(h1))}
    ${m(`Treffer Resultat (${n2})`, pct(h2))}
    ${m(`Treffer Over/Under (${n3})`, pct(h3))}
    ${m(`Treffer Bet of the day (${n4})`, pct(h4))}
    ${m("Rendite", staked ? pct(total / staked) : "–", cls(total))}
  </div>` + chart(pts) +
  '<div class="note">Fake-Geld. Die Quoten stammen aus dem Modell abzüglich Marge und sagen daher nichts über echte Gewinnchancen gegen Buchmacher aus.</div>';
}

function chart(pts) {
  if (pts.length < 2) return '<div class="card empty">Der Kontoverlauf erscheint nach den ersten abgerechneten Wetten.</div>';
  const W = 600, H = 220, pl = 44, pr = 10, pt = 12, pb = 24;
  const vals = pts.map(p => p[1]);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi - lo < 1) { lo -= 5; hi += 5; }
  const pad = (hi - lo) * 0.1; lo -= pad; hi += pad;
  const x = i => pl + (W - pl - pr) * (pts.length === 1 ? 0 : i / (pts.length - 1));
  const y = v => pt + (H - pt - pb) * (1 - (v - lo) / (hi - lo));
  const path = pts.map((p, i) => (i ? "L" : "M") + x(i).toFixed(1) + " " + y(p[1]).toFixed(1)).join(" ");
  const base = y(D.start_balance);
  const last = pts[pts.length - 1][1];
  const col = last >= D.start_balance ? "var(--pos)" : "var(--neg)";
  return `<div class="card"><svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Kontoverlauf">
    <line x1="${pl}" x2="${W - pr}" y1="${base}" y2="${base}" stroke="var(--line)" stroke-dasharray="4 4"/>
    <text x="${pl - 6}" y="${y(hi - pad) + 4}" text-anchor="end">${(hi - pad).toFixed(0)}</text>
    <text x="${pl - 6}" y="${y(lo + pad) + 4}" text-anchor="end">${(lo + pad).toFixed(0)}</text>
    <text x="${pl}" y="${H - 6}">${esc(pts[1][0].slice(5).split("-").reverse().join("."))}</text>
    <text x="${W - pr}" y="${H - 6}" text-anchor="end">${esc(pts[pts.length - 1][0].slice(5).split("-").reverse().join("."))}</text>
    <path d="${path}" fill="none" stroke="${col}" stroke-width="2.5" stroke-linejoin="round"/>
    <circle cx="${x(pts.length - 1)}" cy="${y(last)}" r="4" fill="${col}"/>
  </svg></div>`;
}

function wetten() {
  const kinds = ["ALL", "1X2", "SCORE", "OU", "BOTD"];
  const seg = '<div class="seg">' + kinds.map(k => `<button data-kind="${k}" class="${state.kind === k ? "on" : ""}">${k === "ALL" ? "Alle" : KIND[k]}</button>`).join("") + "</div>";
  const rows = D.bets.filter(r => inLeague(r) && (state.kind === "ALL" || r.kind === state.kind));
  if (!rows.length) return seg + '<div class="empty">Noch keine Wetten.</div>';
  return seg + rows.map(r => {
    if (r.legs) {
      const pr = r.status === "OPEN" ? '<span class="badge">offen</span>' : `<b class="${cls(r.profit)}">${chf(r.profit || 0)}</b>`;
      return `<div class="card bet"><div class="l"><div class="t">⭐ Bet of the day · Kombi</div>
        <div class="s">${dt(r.utc)} · @${r.odds.toFixed(2)} · ${r.stake.toFixed(2)} CHF</div>` +
        r.legs.map(l => `<div class="s">${LEGMARK[l.state]} ${esc(l.home)} – ${esc(l.away)}: ${esc(pickLabel(l.pick))}${l.hg != null ? " (" + l.hg + ":" + l.ag + ")" : ""}</div>`).join("") +
        `</div><div class="r">${pr}<div class="s">${STATUS[r.status] || r.status}</div></div></div>`;
    }
    const fin = r.hg != null ? ` · ${r.hg}:${r.ag}` : "";
    const pr = r.status === "OPEN" ? '<span class="badge">offen</span>' : `<b class="${cls(r.profit)}">${chf(r.profit || 0)}</b>`;
    return `<div class="card bet"><div class="l"><div class="t">${esc(r.home)} – ${esc(r.away)}</div>
      <div class="s">${dt(r.utc)} · ${esc(lname(r.league))}</div>
      <div class="s">${KIND[r.kind]} <b>${esc(r.kind === "BOTD" ? pickLabel(r.pick) : r.pick)}</b> @${r.odds.toFixed(2)} · ${r.stake.toFixed(2)} CHF${fin}</div></div>
      <div class="r">${pr}<div class="s">${STATUS[r.status] || r.status}</div></div></div>`;
  }).join("") + `<div class="note">Angezeigt werden die letzten ${D.bets.length} Wetten.</div>`;
}

function tabelle() {
  const code = oneLeague();
  if ((D.no_table || []).includes(code)) return '<div class="empty">Für diese Liga gibt es keine Tabelle (die Gruppen sind in den Daten nicht enthalten).</div>';
  const t = D.tables[code] || [];
  if (!t.length) return '<div class="empty">Noch keine beendeten Spiele für diese Liga.</div>';
  return `<div class="note">${esc(lname(code))}</div><div class="card scroll"><table><thead><tr><th>#</th><th>Team</th><th>Sp</th><th>S</th><th>U</th><th>N</th><th>Tore</th><th>TD</th><th>Pkt</th></tr></thead><tbody>` +
    t.map((r, i) => `<tr><td>${i + 1}</td><td>${esc(r.team)}</td><td>${r.sp}</td><td>${r.s}</td><td>${r.u}</td><td>${r.n}</td><td>${r.gf}:${r.ga}</td><td>${r.td > 0 ? "+" : ""}${r.td}</td><td><b>${r.pkt}</b></td></tr>`).join("") + "</tbody></table></div>";
}

function modell() {
  const code = oneLeague();
  const t = D.strength[code] || [];
  if (!t.length) return '<div class="empty">Noch zu wenig Daten für das Modell.</div>';
  return `<div class="note">${esc(lname(code))}: Teamstärken (1.00 = Ligadurchschnitt). Höherer Angriff und niedrigere Abwehr sind besser. Gesamt = Angriff / Abwehr.</div><div class="card scroll"><table><thead><tr><th>#</th><th>Team</th><th>Angriff</th><th>Abwehr</th><th>Gesamt</th></tr></thead><tbody>` +
    t.map((r, i) => `<tr><td>${i + 1}</td><td>${esc(r.team)}</td><td>${r.att.toFixed(2)}</td><td>${r.dfn.toFixed(2)}</td><td><b>${r.total.toFixed(2)}</b></td></tr>`).join("") + "</tbody></table></div>";
}

const RUNSTATUS = {ACTIVE: "läuft", REACHED: "Ziel erreicht 🏆", LOST: "verloren", EXPIRED: "Zeit abgelaufen", CANCELLED: "abgebrochen"};
const fr = v => Number(v).toLocaleString("de-CH", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const day = iso => new Date(iso).toLocaleDateString("de-CH", {timeZone: "Europe/Zurich", day: "2-digit", month: "2-digit", year: "2-digit"});
let multiMsg = "";

function multiBetCard(b, i) {
  const open = b.status === "OPEN";
  const res = open ? '<span class="badge">offen</span>'
    : b.status === "CANCELLED" ? '<span class="badge">verfallen</span>'
    : `<b class="${cls(b.profit)}">${chf(b.profit || 0)} Fr.</b>`;
  const legs = b.legs.map(l => `<div class="s">${LEGMARK[l.state]} ${dt(l.utc)} · ${esc(l.home)} – ${esc(l.away)}: <b>${esc(pickLabel(l.pick))}</b> <small>@${l.odds.toFixed(2)} · ${pct(l.prob)}</small>${l.hg != null ? " · " + l.hg + ":" + l.ag : ""}</div>`).join("");
  const after = b.pot_after != null ? ` → Topf <b>${fr(b.pot_after)}</b>` : open ? ` → bei Treffer <b>${fr(b.stake * b.odds)}</b>` : "";
  const saved = b.saved ? `<div class="s">🏦 ${fr(b.saved)} Fr. aufs Sparkonto</div>` : "";
  return `<div class="card"${open ? ' style="border-color:var(--acc)"' : ""}>
    <div class="bet"><div class="l"><div class="t">Stufe ${i + 1} · Kombi @${b.odds.toFixed(2)} · ${pct(b.prob)}</div>
    <div class="s">Einsatz ${fr(b.stake)} Fr.${after}</div></div><div class="r">${res}</div></div>
    <div style="margin-top:6px">${legs}</div>${saved}</div>`;
}

function multi() {
  const M = D.multi || {runs: []};
  const runs = M.runs;
  const cur = runs.find(r => r.status === "ACTIVE") || runs[0];
  const m = (l, v, c) => `<div class="metric"><span>${l}</span><b class="${c || ""}">${v}</b></div>`;
  let h = '<div class="note">Der Bot setzt jeweils den ganzen Topf auf eine Kombi mit Quote um 2.0 und setzt den Gewinn weiter. ' +
    `Nach je ${M.rules ? M.rules.save_every : 3} Treffern gehen ${M.rules ? Math.round(M.rules.save_share * 100) : 50} % des Gewinns aufs Sparkonto. Eine verlorene Kombi kostet den Topf und beendet den Versuch. Fake-Geld.</div>`;
  if (cur) {
    const total = cur.pot + cur.savings;
    const prog = total > cur.start ? Math.min(1, Math.log(total / cur.start) / Math.log(cur.target / cur.start)) : 0;
    const left = Math.max(0, (new Date(cur.ends_at) - new Date(D.generated)) / 86400000);
    h += `<div class="card"><div class="meta"><span>Versuch #${cur.id} · ${RUNSTATUS[cur.status]}</span><span>${day(cur.created_at)} – ${day(cur.ends_at)}</span></div>
      <div class="teams">${fr(total)} Fr. von ${fr(cur.target)} Fr.</div>
      <div class="prog"><i style="width:${(prog * 100).toFixed(1)}%"></i></div>
      <div class="probs"><span>×${(total / cur.start).toFixed(1)}</span><span>Ziel ×${(cur.target / cur.start).toFixed(0)}</span></div></div>
      <div class="grid">
      ${m("Topf (wird gesetzt)", fr(cur.pot) + " Fr.")}
      ${m("Sparkonto", fr(cur.savings) + " Fr.")}
      ${m("Startkapital", fr(cur.start) + " Fr.")}
      ${cur.status === "ACTIVE" ? m("Verbleibend", left >= 1 ? Math.ceil(left) + " Tage" : Math.round(left * 24) + " Std.") : m("Ergebnis", chf(total - cur.start) + " Fr.", cls(total - cur.start))}
      ${m("Treffer bis Sparrunde", cur.streak + " / " + (M.rules ? M.rules.save_every : 3))}
      ${m("Treffer gesamt", cur.wins)}
      </div>`;
    if (cur.status === "ACTIVE" && cur.need != null)
      h += `<div class="note">Noch etwa <b>${cur.need} Treffer in Folge</b> nötig (bei Quote ~2.0). Bei rund 45 % Trefferchance pro Kombi entspricht das einer Chance von etwa ${cur.chance >= 0.01 ? Math.round(cur.chance * 100) + " %" : "1 zu " + Math.round(1 / cur.chance).toLocaleString("de-CH")}.</div>`;
    if (cur.status === "ACTIVE" && !cur.bets.some(b => b.status === "OPEN"))
      h += '<div class="card empty">Gerade keine Kombi offen. Der Bot sucht bei jedem Lauf (stündlich) nach Spielen in den nächsten 36 Stunden, die eine Kombi um 2.0 ergeben.</div>';
    const bets = cur.bets.map(multiBetCard).reverse();
    if (bets.length) h += "<h2>Kombis in diesem Versuch</h2>" + bets.join("");
  }
  const d = M.defaults || {start: 100, target: 5000, days: 28};
  h += `<h2>Neuer Versuch</h2><div class="card">
    <div class="form">
      <label>Startkapital (Fr.)<input id="m-start" type="number" inputmode="decimal" min="1" step="any" value="${d.start}"></label>
      <label>Ziel (Fr.)<input id="m-target" type="number" inputmode="decimal" min="1" step="any" value="${d.target}"></label>
      <label>Dauer (Tage)<input id="m-days" type="number" inputmode="numeric" min="1" step="1" value="${d.days}"></label>
    </div>
    <div class="row" style="flex-wrap:wrap"><button class="btn" id="m-go">Neuen Versuch starten</button>
      <button class="link" id="m-token">GitHub-Token ändern</button></div>
    <div class="note" id="m-msg">${esc(multiMsg)}</div>
    <div class="note">Startet den Bot auf GitHub mit diesen Werten. Ein laufender Versuch wird dabei abgebrochen. Nach etwa 2 bis 5 Minuten erscheint der neue Versuch hier.
      Alternativ: <a href="https://github.com/${esc(D.repo)}/actions/workflows/tippbot.yml" target="_blank" rel="noopener">im Actions-Tab</a> „Run workflow“ wählen und die Felder ausfüllen.</div></div>`;
  if (runs.length)
    h += `<h2>Alle Versuche</h2><div class="card scroll"><table><thead><tr><th>#</th><th>Zeitraum</th><th>Start</th><th>Ziel</th><th>Ende</th><th>Status</th></tr></thead><tbody>` +
      runs.map(r => `<tr><td>${r.id}</td><td>${day(r.created_at).slice(0, 6)} · ${r.days} T.</td><td>${fr(r.start)}</td><td>${fr(r.target)}</td><td><b class="${cls(r.pot + r.savings - r.start)}">${fr(r.pot + r.savings)}</b></td><td>${RUNSTATUS[r.status] || r.status}</td></tr>`).join("") +
      "</tbody></table></div>";
  return h;
}

function askToken() {
  const t = prompt("Für den Start braucht das Dashboard einmalig ein GitHub-Token (Fine-grained, nur für dieses Repository, Berechtigung „Actions: Read and write“). Es wird nur in diesem Browser gespeichert.", "");
  if (t && t.trim()) { set("gh_token", t.trim()); return t.trim(); }
  return null;
}

async function startMulti() {
  const s = parseFloat($("#m-start").value), t = parseFloat($("#m-target").value), d = parseInt($("#m-days").value, 10);
  const msg = x => { multiMsg = x; const e = $("#m-msg"); if (e) e.textContent = x; };
  if (!(s > 0) || !(t > s) || !(d > 0)) return msg("Bitte gültige Werte eingeben: Ziel muss grösser als das Startkapital sein, Dauer mindestens 1 Tag.");
  const active = (D.multi.runs || []).some(r => r.status === "ACTIVE");
  if (!confirm(`Neuer Versuch: ${fr(s)} Fr. → ${fr(t)} Fr. in ${d} Tagen.` + (active ? "\nDer laufende Versuch wird abgebrochen." : "") + "\nStarten?")) return;
  const tok = get("gh_token", "") || askToken();
  if (!tok) return msg("Ohne GitHub-Token kann das Dashboard den Bot nicht starten.");
  const btn = $("#m-go"); btn.disabled = true; msg("Wird gestartet …");
  try {
    const r = await fetch(`https://api.github.com/repos/${D.repo}/actions/workflows/tippbot.yml/dispatches`, {
      method: "POST",
      headers: {Authorization: "Bearer " + tok, Accept: "application/vnd.github+json"},
      body: JSON.stringify({ref: "main", inputs: {multi_start: String(s), multi_target: String(t), multi_days: String(d)}})});
    if (r.status === 204) msg("Gestartet. Der neue Versuch erscheint in etwa 2 bis 5 Minuten (Seite neu laden).");
    else if (r.status === 401 || r.status === 403 || r.status === 404) { set("gh_token", ""); msg(`GitHub hat den Start abgelehnt (${r.status}). Das Token fehlt, ist abgelaufen oder hat nicht die Berechtigung „Actions: Read and write“ für dieses Repository. Beim nächsten Klick wird es neu abgefragt.`); }
    else msg("Fehler beim Starten: " + r.status + " " + (await r.text()).slice(0, 200));
  } catch (e) { msg("Keine Verbindung zu GitHub: " + e.message); }
  btn.disabled = false;
}

const VIEWS = {tips, multi, konto, wetten, tabelle, modell};
function render() {
  $("#tabs").innerHTML = TABS.map(t => `<button data-tab="${t[0]}" class="${state.tab === t[0] ? "on" : ""}">${t[1]}</button>`).join("");
  $("#view").innerHTML = VIEWS[state.tab]();
}
$("#lg").innerHTML = '<option value="ALL">Alle Ligen</option>' + Object.keys(D.leagues).map(c => `<option value="${esc(c)}">${esc(D.leagues[c])}</option>`).join("");
$("#lg").value = state.league;
$("#lg").addEventListener("change", e => { state.league = e.target.value; set("league", state.league); render(); });
$("#tabs").addEventListener("click", e => { const b = e.target.closest("button"); if (!b) return; state.tab = b.dataset.tab; history.replaceState(null, "", "#" + state.tab); render(); window.scrollTo(0, 0); });
$("#view").addEventListener("click", e => {
  if (e.target.closest("#m-go")) return startMulti();
  if (e.target.closest("#m-token")) { askToken(); return; }
  const b = e.target.closest("button[data-kind]"); if (!b) return; state.kind = b.dataset.kind; render(); });
$("#stand").textContent = "Stand: " + new Date(D.generated).toLocaleString("de-CH", {timeZone: "Europe/Zurich", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"}) + " · Fake-Geld, keine echten Wetten";
render();
setTimeout(() => location.reload(), 10 * 60 * 1000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()

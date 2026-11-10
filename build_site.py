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
    data["bets"] = [
        {"league": r[0], "utc": r[1], "home": r[2], "away": r[3], "kind": r[4], "pick": r[5],
         "stake": r[6], "odds": r[7], "status": r[8], "profit": r[9], "hg": r[10], "ag": r[11]}
        for r in con.execute("""
            SELECT m.league, m.utc_date, m.home, m.away, b.kind, b.pick, b.stake, b.odds,
                   b.status, b.profit, m.home_goals, m.away_goals
            FROM bets b JOIN matches m ON m.id=b.match_id
            ORDER BY m.utc_date DESC, b.match_id, b.kind LIMIT ?""", (MAX_BETS,))]
    return data


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    con = sqlite3.connect(bot.DB_PATH)
    bot.init_db(con)
    paper.init_bets(con)
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
const TABS = [["tips","Tipps"],["konto","Konto"],["wetten","Wetten"],["tabelle","Tabelle"],["modell","Modell"]];
const KIND = {"1X2":"Tipp","SCORE":"Resultat","OU":"Over/Under"};
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

function tips() {
  const rows = D.upcoming.filter(inLeague);
  if (!rows.length) return '<div class="empty">Keine kommenden Spiele gefunden.</div>';
  const line = D.ou_line;
  return rows.map(r => {
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
  const [n1, h1] = hit("1X2"), [n2, h2] = hit("SCORE"), [n3, h3] = hit("OU");
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
  const kinds = ["ALL", "1X2", "SCORE", "OU"];
  const seg = '<div class="seg">' + kinds.map(k => `<button data-kind="${k}" class="${state.kind === k ? "on" : ""}">${k === "ALL" ? "Alle" : KIND[k]}</button>`).join("") + "</div>";
  const rows = D.bets.filter(r => inLeague(r) && (state.kind === "ALL" || r.kind === state.kind));
  if (!rows.length) return seg + '<div class="empty">Noch keine Wetten.</div>';
  return seg + rows.map(r => {
    const fin = r.hg != null ? ` · ${r.hg}:${r.ag}` : "";
    const pr = r.status === "OPEN" ? '<span class="badge">offen</span>' : `<b class="${cls(r.profit)}">${chf(r.profit || 0)}</b>`;
    return `<div class="card bet"><div class="l"><div class="t">${esc(r.home)} – ${esc(r.away)}</div>
      <div class="s">${dt(r.utc)} · ${esc(lname(r.league))}</div>
      <div class="s">${KIND[r.kind]} <b>${esc(r.pick)}</b> @${r.odds.toFixed(2)} · ${r.stake.toFixed(2)} CHF${fin}</div></div>
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

const VIEWS = {tips, konto, wetten, tabelle, modell};
function render() {
  $("#tabs").innerHTML = TABS.map(t => `<button data-tab="${t[0]}" class="${state.tab === t[0] ? "on" : ""}">${t[1]}</button>`).join("");
  $("#view").innerHTML = VIEWS[state.tab]();
}
$("#lg").innerHTML = '<option value="ALL">Alle Ligen</option>' + Object.keys(D.leagues).map(c => `<option value="${esc(c)}">${esc(D.leagues[c])}</option>`).join("");
$("#lg").value = state.league;
$("#lg").addEventListener("change", e => { state.league = e.target.value; set("league", state.league); render(); });
$("#tabs").addEventListener("click", e => { const b = e.target.closest("button"); if (!b) return; state.tab = b.dataset.tab; history.replaceState(null, "", "#" + state.tab); render(); window.scrollTo(0, 0); });
$("#view").addEventListener("click", e => { const b = e.target.closest("button[data-kind]"); if (!b) return; state.kind = b.dataset.kind; render(); });
$("#stand").textContent = "Stand: " + new Date(D.generated).toLocaleString("de-CH", {timeZone: "Europe/Zurich", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"}) + " · Fake-Geld, keine echten Wetten";
render();
setTimeout(() => location.reload(), 10 * 60 * 1000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
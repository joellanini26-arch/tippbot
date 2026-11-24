# Tippbot: tgfmt.py
"""
Telegram-Nachrichten, uebersichtlich formatiert (Telegram-HTML: <b>, <i>, <code>).
Jedes Thema ist eine eigene Nachricht: Abrechnung, neue Tipps, Bet of the day, Challenges.
"""
import html
from datetime import timedelta

import bot

WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
LINE = "──────────────"
MARK = {"WON": "✅", "LOST": "❌", "VOID": "↩️", "OPEN": "⏳"}


def h(s):
    return html.escape(str(s), quote=False)


def fr(v):
    return f"{v:,.2f}".replace(",", "'")


def signed(v):
    return ("+" if v >= 0 else "−") + fr(abs(v))


def local(utc):
    import paper
    return bot.parse_date(utc).astimezone(paper.LOCAL_TZ)


def when(utc):
    t = local(utc)
    import paper
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).astimezone(paper.LOCAL_TZ).date()
    day = "heute" if t.date() == today else "morgen" if t.date() == today + timedelta(days=1) \
        else f"{WEEKDAYS[t.weekday()]} {t:%d.%m.}"
    return f"{day} {t:%H:%M}"


def tip_text(kind, pick, home, away):
    """Lesbarer Tipp: 'Sieg Arsenal', 'Über 2.5 Tore', 'Resultat 2:1'."""
    if kind == "SCORE":
        return f"Resultat {pick}"
    if pick.startswith("Over"):
        return f"Über {pick.split()[1]} Tore"
    if pick.startswith("Under"):
        return f"Unter {pick.split()[1]} Tore"
    return {"1": f"Sieg {home}", "2": f"Sieg {away}", "X": "Unentschieden",
            "1X": f"{home} oder X", "X2": f"{away} oder X", "12": "kein Unentschieden"}.get(pick, pick)


def combo_title(kind, label):
    return {"CRAZY": "🤪 Crazy Bet", "CUSTOM": f"📌 {label or 'Eigene Wette'}"}.get(kind, "⭐ Bet of the day")


# ---------- Abrechnung ----------
def settled(results, acc):
    out = ["📊 <b>Abrechnung</b>", ""]
    games, combos = {}, []
    for r in results:
        if r.get("combo"):
            combos.append(r)
        else:
            games.setdefault((r["home"], r["away"], r["actual"]), []).append(r)
    for (home, away, score), bets in games.items():
        out.append(f"<b>{h(home)} – {h(away)}</b>  <code>{h(score)}</code>")
        for b in bets:
            out.append(f"{MARK[b['status']]} {h(tip_text(b['kind'], b['pick'], home, away))} "
                       f"<i>@{b['odds']:.2f}</i>  <b>{signed(b['profit'])}</b>")
        out.append("")
    for c in combos:
        out.append(f"{MARK[c['status']]} <b>{h(combo_title(c.get('ckind'), c.get('label')))}</b> "
                   f"<i>@{c['odds']:.2f}</i>  <b>{signed(c['profit'])}</b>")
        for l in c["legs"]:
            m = {"WON": "✓", "LOST": "✗", "VOID": "–", "OPEN": "·"}[l["state"]]
            out.append(f"   {m} {h(l['home'])} – {h(l['away'])}: {h(tip_text('', l['pick'], l['home'], l['away']))}"
                       f" <code>{h(l['score'])}</code>")
        out.append("")
    won = sum(r["status"] == "WON" for r in results)
    lost = sum(r["status"] == "LOST" for r in results)
    total = sum(r["profit"] for r in results)
    out += [LINE, f"Σ <b>{signed(total)} CHF</b>   {won} ✅  {lost} ❌",
            f"💰 Kontostand <b>{fr(acc['balance'])} CHF</b>",
            f"<i>Offen: {acc['open']} Wetten · {fr(acc['open_stake'])} CHF Einsatz</i>"]
    return "\n".join(out)


# ---------- Neue Tipps ----------
def new_tips(placed):
    """placed: [(liga, heim, gast, art, tipp, einsatz, quote, utc)]"""
    by_league = {}
    for league, home, away, kind, pick, stake, odds, utc in placed:
        by_league.setdefault(league, {}).setdefault((utc, home, away), []).append((kind, pick, stake, odds))
    n = len(placed)
    total = sum(p[5] for p in placed)
    out = [f"⚽ <b>Neue Tipps</b>  <i>{n} Wetten · {fr(total)} CHF Einsatz</i>"]
    order = {"1X2": 0, "OU": 1, "SCORE": 2}
    for league, games in by_league.items():
        out += ["", f"🏆 <b>{h(league)}</b>"]
        for (utc, home, away), bets in sorted(games.items()):
            out.append(f"<b>{h(home)} – {h(away)}</b>  <i>{when(utc)}</i>")
            for kind, pick, stake, odds in sorted(bets, key=lambda b: order.get(b[0], 9)):
                out.append(f"   ▸ {h(tip_text(kind, pick, home, away))} <i>@{odds:.2f}</i> · {stake:g} CHF")
    return "\n".join(out)


# ---------- Bet of the day ----------
def botd(best, record, stake, rules):
    won, lost, profit = record
    rec = f"<i>Bilanz: {won} ✅ {lost} ❌ · {signed(profit)} CHF</i>"
    if not best:
        return f"⭐ <b>Bet of the day</b>\nHeute keine Kombi, die sicher genug ist ({h(rules)}).\n{rec}"
    out = [f"⭐ <b>Bet of the day</b>  <i>{len(best['legs'])}er-Kombi</i>", ""]
    for l in best["legs"]:
        out.append(f"<b>{h(l['home'])} – {h(l['away'])}</b>  <i>{when(l['utc'])}</i>")
        out.append(f"   ▸ {h(tip_text('', l['pick'], l['home'], l['away']))} <i>@{l['odds']:.2f} · {l['prob']:.0%}</i>")
    out += ["", LINE, f"Quote <b>@{best['odds']:.2f}</b> · Chance {best['prob']:.0%}",
            f"Einsatz {stake:.0f} CHF → bei Treffer <b>{signed(stake * (best['odds'] - 1))} CHF</b>", rec]
    return "\n".join(out)


# ---------- Uebrige Texte (Challenges, Crazy Bet ...) ----------
def block(text):
    """Bestehender Klartext: escapen, erste Zeile fett."""
    out = []
    for i, x in enumerate(text.strip().split("\n")):
        e = h(x)
        # Ueberschriften: erste Zeile und Zeilen, die mit einem Emoji beginnen
        out.append(f"<b>{e}</b>" if i == 0 or (x[:1] and not x[:1].isspace() and not x[:1].isalnum()) else e)
    return "\n".join(out)

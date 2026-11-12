# Tippbot: calib.py
"""
Selbstkontrolle: Treffen Tipps mit "70 %" wirklich etwa 70 %?

- Vergleicht pro Wettart die Modell-Wahrscheinlichkeit der abgerechneten Wetten mit der
  tatsaechlichen Trefferquote.
- Daraus entsteht ein Korrekturfaktor (z.B. 0.93 = Modell ist etwas zu optimistisch).
  Er wird erst nach und nach wirksam (Schrumpfung Richtung 1.0) und ist begrenzt.
- paper.py prueft die Schwellen und bemisst die Einsaetze mit der korrigierten Wahrscheinlichkeit.
"""
PRIOR = 30            # so viele "virtuelle" Wetten mit Faktor 1.0 (gegen Zufallsschwankungen)
F_MIN, F_MAX = 0.80, 1.20
KINDS = ("1X2", "OU", "SCORE")


def ensure_prob_column(con, margin):
    """Spalte prob in bets anlegen; alte Wetten aus der Quote zurueckrechnen."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(bets)")]
    if "prob" not in cols:
        con.execute("ALTER TABLE bets ADD COLUMN prob REAL")
    con.execute("UPDATE bets SET prob = MIN(0.99, (1 - ?) / odds) WHERE prob IS NULL AND odds > 0", (margin,))
    con.commit()


def factors(con):
    """{kind: {"n", "hits", "pred", "rate", "factor"}} aus allen abgerechneten Wetten."""
    out = {}
    for kind in KINDS:
        n, hits, pred = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(status='WON'),0), COALESCE(SUM(prob),0) FROM bets "
            "WHERE kind=? AND status IN ('WON','LOST') AND prob IS NOT NULL", (kind,)).fetchone()
        mean = pred / n if n else None
        if n:
            f = (hits + PRIOR * mean) / ((n + PRIOR) * mean)
            f = max(F_MIN, min(F_MAX, f))
        else:
            f = 1.0
        out[kind] = {"n": n, "hits": int(hits), "pred": mean, "rate": hits / n if n else None,
                     "factor": round(f, 3)}
    return out


BUCKETS = {"1X2": [0.0, 0.5, 0.6, 0.7, 0.8, 1.01], "OU": [0.5, 0.6, 0.7, 0.8, 1.01],
           "SCORE": [0.0, 0.12, 0.15, 0.2, 1.01]}


def buckets(con):
    """Trefferquote pro Sicherheitsstufe fuers Dashboard."""
    rows = []
    for kind in KINDS:
        edges = BUCKETS[kind]
        for lo, hi in zip(edges, edges[1:]):
            n, hits, pred = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(status='WON'),0), AVG(prob) FROM bets "
                "WHERE kind=? AND status IN ('WON','LOST') AND prob >= ? AND prob < ?",
                (kind, lo, hi)).fetchone()
            if n:
                rows.append({"kind": kind, "lo": lo, "hi": min(hi, 1.0), "n": n,
                             "pred": pred, "rate": hits / n})
    return rows

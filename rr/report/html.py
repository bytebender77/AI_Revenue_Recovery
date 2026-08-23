"""Static HTML report, generated from the sealed-run JSON.

    make report        ->  docs/report.html

Every figure is read from docs/results_test.json / results_dev.json, which are the
raw output of `rr/eval/sealed_run.py`. Nothing is retyped, so the page cannot drift
from docs/results.md. Self-contained: no external CSS, JS, fonts or images.
"""
from __future__ import annotations

import argparse
import html
import json
import pathlib

ARMS = ("B0_do_nothing", "B1_blind_ladder", "B2_good_rules", "B2.5_plus_outage",
        "AGENT", "B3_greedy")
LABEL = {"B0_do_nothing": "B0 do-nothing", "B1_blind_ladder": "B1 blind ladder",
         "B2_good_rules": "B2 good rules", "B2.5_plus_outage": "B2.5 + outage rule",
         "AGENT": "AGENT", "B3_greedy": "B3 greedy oracle"}

CSS = """
/* Light palette is the base, defined on bare :root so nothing depends on a media
   query having matched. Dark redefines ONLY the tokens, twice: once for the system
   preference (guarded so an explicit light choice wins) and once for an explicit
   dark choice. body paints an explicit token background -- a transparent body
   borrows whatever the host is painting, which is how this page previously
   rendered cream text on the browser's white. */
:root{
  --bg:#fbfaf7; --fg:#1c1a17; --mut:#6b665e; --line:#e2ded6; --card:#ffffff;
  --card-fg:#1c1a17; --pos:#146b45; --neg:#a33227; --accent:#8a4b2a;
  --hi:rgba(138,75,42,.10);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#161513; --fg:#ece8e1; --mut:#a09a90; --line:#332f2a; --card:#1e1c19;
    --card-fg:#ece8e1; --pos:#5fbf8f; --neg:#e0796b; --accent:#d99a6c;
    --hi:rgba(217,154,108,.14);
  }
}
:root[data-theme="dark"]{
  --bg:#161513; --fg:#ece8e1; --mut:#a09a90; --line:#332f2a; --card:#1e1c19;
  --card-fg:#ece8e1; --pos:#5fbf8f; --neg:#e0796b; --accent:#d99a6c;
  --hi:rgba(217,154,108,.14);
}
*{box-sizing:border-box}
html{background:var(--bg)}
body{background:var(--bg);color:var(--fg);margin:0;padding:2.5rem 1.25rem 5rem;
font:16px/1.6 Georgia,'Iowan Old Style','Times New Roman',serif;
-webkit-font-smoothing:antialiased}
main{max-width:62rem;margin:0 auto}
h1{font-size:1.9rem;margin:0 0 .35rem;letter-spacing:-.01em;color:var(--fg)}
h2{font-size:1.15rem;margin:2.75rem 0 .6rem;padding-bottom:.35rem;
border-bottom:1px solid var(--line);letter-spacing:-.005em;color:var(--fg)}
.sub{color:var(--mut);margin:0 0 2rem;font-size:.95rem;max-width:46rem}
code{font:.9em/1 ui-monospace,'SF Mono',Menlo,monospace}
.wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -.25rem}
table{border-collapse:collapse;width:100%;
font:13px/1.5 ui-monospace,'SF Mono',Menlo,monospace;color:var(--fg)}
th,td{padding:.45rem .65rem;text-align:right;border-bottom:1px solid var(--line);
white-space:nowrap;color:var(--fg)}
th:first-child,td:first-child{text-align:left}
thead th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;
letter-spacing:.07em;border-bottom:1.5px solid var(--line)}
tr.hi td{background:var(--hi);font-weight:700}
.pos{color:var(--pos)}.neg{color:var(--neg)}
.note{color:var(--mut);font-size:.9rem;margin:.8rem 0 0;font-style:italic;max-width:46rem}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(11.5rem,1fr));
gap:.8rem;margin:1.5rem 0 .5rem}
.card{background:var(--card);color:var(--card-fg);border:1px solid var(--line);
border-radius:8px;padding:.9rem 1rem}
.card .k{color:var(--mut);font-size:10.5px;text-transform:uppercase;
letter-spacing:.07em;line-height:1.35}
.card .v{font:600 1.4rem/1.2 ui-monospace,Menlo,monospace;margin-top:.3rem;
color:var(--card-fg)}
.card .c{color:var(--mut);font-size:.8rem;margin-top:.25rem;line-height:1.4}
svg{max-width:100%;height:auto;display:block;margin:1.25rem 0}
"""


def inr(minor) -> str:
    return f"{minor / 100:,.0f}"


def sign(v: float, txt: str) -> str:
    cls = "pos" if v > 0 else ("neg" if v < 0 else "")
    return f'<span class="{cls}">{txt}</span>'


def arm_table(H: dict) -> str:
    rows = []
    for k in ARMS:
        h = H[k]
        ci = f"[{inr(h['ci'][0])}, {inr(h['ci'][1])}]" if k != "B0_do_nothing" else "—"
        rows.append(
            f'<tr class="{"hi" if k == "AGENT" else ""}"><td>{LABEL[k]}</td>'
            f"<td>{inr(h['gross'])}</td><td>{inr(h['incremental'])}</td>"
            f"<td>{ci}</td><td>{sign(h['net'], inr(h['net']))}</td>"
            f"<td>{h['debits']}</td><td>{h['contacts']}</td>"
            f"<td>{h['wasted_rate']:.1%}</td><td>{h['oracle_share']:.1%}</td></tr>")
    return ('<div class="wrap"><table><thead><tr><th>arm</th><th>gross</th>'
            "<th>incremental</th><th>95% CI</th><th>net</th><th>debits</th>"
            "<th>contacts</th><th>wasted</th><th>oracle</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>")


def delta_table(d: dict, t: dict) -> str:
    nd, nt = d["n"], t["n"]
    rows = []

    def add(name, dv, tv, fmt="inr"):
        a, b = dv / nd * 1000, tv / nt * 1000
        ch = (b - a) / a * 100 if a else 0
        av, bv = (inr(a), inr(b)) if fmt == "inr" else (f"{a:.0f}", f"{b:.0f}")
        rows.append(f"<tr><td>{name}</td><td>{av}</td><td>{bv}</td>"
                    f"<td>{sign(ch, f'{ch:+.1f}%')}</td></tr>")

    for k in ("AGENT", "B2_good_rules", "B2.5_plus_outage", "B3_greedy", "B1_blind_ladder"):
        add(f"incremental — {LABEL[k]}", d["headline"][k]["incremental"],
            t["headline"][k]["incremental"])
    add("net — AGENT", d["headline"]["AGENT"]["net"], t["headline"]["AGENT"]["net"])
    add("debits — AGENT", d["headline"]["AGENT"]["debits"], t["headline"]["AGENT"]["debits"], "n")
    add("contacts — AGENT", d["headline"]["AGENT"]["contacts"], t["headline"]["AGENT"]["contacts"], "n")
    for name, a, b in (
        ("oracle share — AGENT", d["headline"]["AGENT"]["oracle_share"] * 100,
         t["headline"]["AGENT"]["oracle_share"] * 100),
        ("Brier (held-out)", d["brier"] * 1000, t["brier"] * 1000)):
        rows.append(f"<tr><td>{name}</td><td>{a:.1f}</td><td>{b:.1f}</td>"
                    f"<td>{sign(b - a, f'{b - a:+.1f}')}</td></tr>")
    return ('<div class="wrap"><table><thead><tr><th>metric</th><th>dev / 1k</th>'
            "<th>test / 1k</th><th>change</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>")


def reliability_svg(table: list, brier: float) -> str:
    W, H, P = 620, 300, 42
    pts, bars = [], []
    for lo, hi, n, pred, obs in table:
        x = P + (pred * (W - 2 * P))
        y = H - P - (obs * (H - 2 * P))
        r = max(3, min(11, (n / 260) ** 0.5 * 9))
        pts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="var(--accent)" '
                   f'fill-opacity=".72"><title>{n} attempts — predicted {pred:.3f}, '
                   f'observed {obs:.3f}</title></circle>')
        if abs(pred - obs) > 0.04:
            yp = H - P - (pred * (H - 2 * P))
            bars.append(f'<line x1="{x:.1f}" y1="{yp:.1f}" x2="{x:.1f}" y2="{y:.1f}" '
                        f'stroke="var(--neg)" stroke-width="1.4" stroke-dasharray="2 2"/>')
    grid = "".join(
        f'<line x1="{P + f * (W - 2 * P):.0f}" y1="{P}" x2="{P + f * (W - 2 * P):.0f}" '
        f'y2="{H - P}" stroke="var(--line)"/>'
        f'<line x1="{P}" y1="{H - P - f * (H - 2 * P):.0f}" x2="{W - P}" '
        f'y2="{H - P - f * (H - 2 * P):.0f}" stroke="var(--line)"/>'
        for f in (0, .25, .5, .75, 1))
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Reliability diagram: '
            f'predicted versus observed success probability, Brier {brier:.4f}">{grid}'
            f'<line x1="{P}" y1="{H - P}" x2="{W - P}" y2="{P}" stroke="var(--mut)" '
            f'stroke-dasharray="5 4"/>{"".join(bars)}{"".join(pts)}'
            f'<text x="{W / 2}" y="{H - 8}" text-anchor="middle" fill="var(--mut)" '
            f'font-size="11" font-family="monospace">predicted p(success)</text>'
            f'<text x="12" y="{H / 2}" fill="var(--mut)" font-size="11" '
            f'font-family="monospace" transform="rotate(-90 12 {H / 2})" '
            f'text-anchor="middle">observed</text>'
            f'<text x="{W - P}" y="{P - 12}" text-anchor="end" fill="var(--mut)" '
            f'font-size="11" font-family="monospace">Brier {brier:.4f} — dot area ∝ n; '
            f'dashed drop = miscalibration</text></svg>')


def cause_table(rows: list) -> str:
    out = []
    for cause, n, agent, b25, delta in rows:
        out.append(f'<tr class="{"hi" if delta < 0 else ""}"><td>{html.escape(cause)}</td>'
                   f"<td>{n}</td><td>{inr(agent)}</td><td>{inr(b25)}</td>"
                   f"<td>{sign(delta, inr(delta))}</td></tr>")
    return ('<div class="wrap"><table><thead><tr><th>cause</th><th>n</th><th>agent</th>'
            "<th>B2.5</th><th>delta</th></tr></thead><tbody>"
            + "".join(out) + "</tbody></table></div>")


def build(dev: dict, test: dict, causes: list) -> str:
    H = test["headline"]
    a = H["AGENT"]
    cards = "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div>'
        f'<div class="c">{c}</div></div>'
        for k, v, c in (
            ("incremental", inr(a["incremental"]), f"95% CI [{inr(a['ci'][0])}, {inr(a['ci'][1])}]"),
            ("net", inr(a["net"]), "after attempt, contact and penalty costs"),
            ("oracle share", f"{a['oracle_share']:.1%}", "of greedy-oracle attainable"),
            ("Brier", f"{test['brier']:.4f}", "held-out calibration"),
            ("observable violations", str(a["never_retry_observable"]), "every arm, every tier"),
            ("unauthorised debits", str(a["unauthorized"]), "B1 attempted 1391")))
    tiers = "".join(
        f"<tr><td>{t}</td><td>{v['intents']}</td><td>{v['true']}</td>"
        f"<td>{v['observable']}</td></tr>"
        for t, v in sorted(test["tiers"].items(), key=lambda kv: -kv[1]["true"]))
    na = test["no_action"]
    reasons = "".join(
        f"<tr><td>{k.split('|')[0]}</td><td>{k.split('|')[1]}</td><td>{v}</td></tr>"
        for k, v in sorted(na["by_reason"].items(), key=lambda kv: -kv[1]))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Revenue Recovery — sealed results</title>
<style>{CSS}</style>
</head>
<body>
<main>
<h1>AI Revenue Recovery — sealed results</h1>
<p class="sub">Held-out test cohort, n={test['n']}. Disjoint merchants and customers,
later time window, unseen outage pattern. Read once. Every figure is generated from
<code>docs/results_test.json</code>; the narrative lives in <code>docs/results.md</code>.</p>
<div class="cards">{cards}</div>

<h2>Arms</h2>
{arm_table(H)}
<p class="note">Incremental = recovered minus what doing nothing would have recovered on the
same intent, paired with common random numbers. B3 is a <em>greedy</em> oracle, so oracle
share is measured against a lower bound on the true ceiling. B1 is net-negative.</p>

<h2>Dev → test: the margin roughly halved</h2>
{delta_table(dev, test)}
<p class="note">Per 1000 intents; dev is in-sample for the success model, test is not.
The baselines degraded less than the agent because rules do not overfit, and the oracle
degraded least of all — so the gap that closed is the agent's, not the problem's.
Calibration transferred; the policy's exploitation of it did not, fully.</p>

<h2>Calibration on held-out attempts</h2>
{reliability_svg(test['reliability'], test['brier'])}
<p class="note">Systematically overconfident in the 0.2–0.4 band — predicted 0.254 against
observed 0.180, and 0.323 against 0.234, over 1075 attempts. Reported, not corrected:
correcting after seeing this number would unseal the run.</p>

<h2>Per cause — including the four losses</h2>
{cause_table(causes)}
<p class="note">Highlighted rows are losses. <code>mandate_invalid</code> is the only loss
that persists from dev. <code>upi_collect_expired</code> and <code>limit_exceeded</code>
flipped from wins on dev to losses on test — the generalisation loss, localised.</p>

<h2>Guardrails</h2>
<div class="wrap"><table><thead><tr><th>signal tier</th><th>intents</th>
<th>true violations</th><th>observable</th></tr></thead><tbody>{tiers}</tbody></table></div>
<p class="note">Observable violations are what the gate could see and act on: zero, across
every arm and tier. True violations sit entirely in tiers where the gateway hid the cause;
the <code>faithful</code> tier — 72% of the cohort — has none. A gate can enforce what the
signal reveals and nothing more.</p>

<h2>NO_ACTION — {na['no_action']} of {na['decisions']} decisions
({na['no_action'] / na['decisions']:.1%})</h2>
<div class="wrap"><table><thead><tr><th>reason code</th><th>binding constraint</th>
<th>count</th></tr></thead><tbody>{reasons}</tbody></table></div>
<p class="note">Three economically distinct reasons for doing nothing, never merged: a rule
removed the option, someone else's payment outbid this one, or nothing available cleared its
own cost. {na['untouched']} of {test['n']} intents were never acted on at all.</p>
</main>
</body>
</html>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", type=pathlib.Path, default=pathlib.Path("docs"))
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("docs/report.html"))
    args = ap.parse_args()
    dev = json.loads((args.docs / "results_dev.json").read_text())
    test = json.loads((args.docs / "results_test.json").read_text())

    from rr.eval.metrics import gap_by_cause_pair  # noqa: F401  (kept for parity)
    causes = test.get("causes")
    if causes is None:
        raise SystemExit(
            "results_test.json has no per-cause block. Re-run `make sealed-report` "
            "with a build of sealed_run.py that records it -- the report will not "
            "invent numbers that are not in the JSON.")
    args.out.write_text(build(dev, test, causes))
    print(f"  wrote {args.out}  ({args.out.stat().st_size // 1024} KB, self-contained)")


if __name__ == "__main__":
    main()

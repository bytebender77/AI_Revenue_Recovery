"""Audit API. `GET /audit/{payment_intent_id}` renders what `make audit` shows.

Read-only by construction: every statement is a SELECT, and the app opens its
connection with `autocommit=True` so nothing it does can write. The point of the
endpoint is that a judge can pick any payment and reconstruct the decision without
reading application code — what was known, what was permitted, what was considered
with scores, what bound the choice, what executed, what happened, under which
versions.

    make serve      ->  http://localhost:8080/audit/dev_pi_000040
"""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from rr.db.conn import url as db_url

app = FastAPI(title="AI Revenue Recovery — audit", docs_url="/docs")


def _rows(sql: str, args: tuple) -> list[dict]:
    import psycopg
    with psycopg.connect(db_url(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def reconstruct(pid: str, run_id: Optional[str] = None) -> dict[str, Any]:
    where = "AND run_id = %s" if run_id else ""
    args = ((pid, run_id) if run_id else (pid,))

    event = _rows("""SELECT run_id, payment_intent_id, arm, amount_minor, currency,
                            method, regime, mandate_id, gateway_code, gateway_reason,
                            gateway_source, gateway_step, gateway_description,
                            failed_at_h, ingested_at
                     FROM failure_event WHERE payment_intent_id=%s ORDER BY id LIMIT 1""",
                  (pid,))
    if not event:
        raise HTTPException(404, f"no failure_event for {pid}")

    decisions = _rows(f"""SELECT id, run_id, decision_seq, slot, arm, chosen_action,
                                 chosen_channel, scheduled_for_h, candidate_set,
                                 score_basis, decision_reason_code, binding_constraint,
                                 taxonomy_version, model_version, policy_version
                          FROM decision WHERE payment_intent_id=%s {where}
                          ORDER BY decision_seq""", args)
    dec_ids = tuple(d["id"] for d in decisions) or (-1,)

    return {
        "payment_intent_id": pid,
        "what_was_known": event[0],
        "what_we_concluded": _rows(
            """SELECT g.failure_cause, g.persistence_class, g.resolver, g.confidence,
                      g.taxonomy_version, g.normalizer_version
               FROM diagnosis g JOIN failure_event f ON f.id=g.failure_event_id
               WHERE f.payment_intent_id=%s ORDER BY g.id LIMIT 4""", (pid,)),
        "what_was_permitted": _rows(
            """SELECT slot, permitted_actions, blocked_actions, rule_evaluations
               FROM eligibility_snapshot WHERE payment_intent_id=%s
               ORDER BY id LIMIT 8""", (pid,)),
        "what_was_decided": decisions,
        "what_executed": _rows(
            """SELECT idempotency_key, adapter, action_type, channel, fired_at_h,
                      revalidation_result, execution_status, outcome, p_used
               FROM attempt WHERE decision_id = ANY(%s) ORDER BY id""", (list(dec_ids),)),
        "what_happened": _rows(
            """SELECT run_id, arm, terminal_state, recovered_amount_minor,
                      recovered_at_h, attribution, debits, contacts
               FROM outcome WHERE payment_intent_id=%s""", (pid,)),
        "explanation": _rows(
            """SELECT e.text, e.source, e.reject_reason
               FROM decision_explanation e WHERE e.decision_id = ANY(%s)""",
            (list(dec_ids),)),
        "ledger": _rows(
            """SELECT seq, event, actor, left(prev_hash,10) AS prev,
                      left(entry_hash,10) AS entry
               FROM ledger_entry WHERE payment_intent_id=%s ORDER BY seq LIMIT 40""",
            (pid,)),
    }


@app.get("/health")
def health() -> dict:
    return {"ok": True, "db": db_url().rsplit("@", 1)[-1]}


@app.get("/audit/{pid}")
def audit(pid: str, run_id: Optional[str] = None) -> dict:
    return reconstruct(pid, run_id)


@app.get("/audit/{pid}/html", response_class=HTMLResponse)
def audit_html(pid: str, run_id: Optional[str] = None) -> str:
    d = reconstruct(pid, run_id)
    e, dec = d["what_was_known"], (d["what_was_decided"] or [{}])[0]
    perm = (d["what_was_permitted"] or [{}])[0]
    out = (d["what_happened"] or [{}])[0]

    cands = sorted(dec.get("candidate_set") or [],
                   key=lambda c: -(c.get("score") or 0))
    seen, rows = set(), []
    for c in cands:                      # best row per (action, channel)
        k = (c.get("action"), c.get("channel"))
        if k in seen:
            continue
        seen.add(k)
        ev = c.get("evidence") or {}
        cls = ' class="hi"' if c.get("chosen") else ""
        rows.append(
            f"<tr{cls}><td>{c.get('action')}</td><td>{c.get('channel') or ''}</td>"
            f"<td>{c.get('score')}</td><td>{ev.get('p_mean', '')}</td>"
            f"<td>{ev.get('observations', '')}</td>"
            f"<td>{'yes' if c.get('permitted') else 'NO'}</td>"
            f"<td>{c.get('blocked_by') or ''}</td></tr>")

    att = "".join(
        f"<tr><td>{a['idempotency_key']}</td><td>{a['action_type']}</td>"
        f"<td>{a['channel'] or ''}</td><td>{a['revalidation_result']}</td>"
        f"<td>{a['execution_status']}</td><td>{a['outcome'] or ''}</td></tr>"
        for a in d["what_executed"])

    return f"""<style>
body{{font:14px/1.6 Georgia,serif;max-width:58rem;margin:2rem auto;padding:0 1rem;
background:#fbfaf7;color:#1c1a17}}
@media(prefers-color-scheme:dark){{body{{background:#161513;color:#ece8e1}}
td,th{{border-color:#332f2a!important}}}}
h1{{font-size:1.4rem;margin:0 0 .2rem}} h2{{font-size:1rem;margin:1.8rem 0 .4rem;
border-bottom:1px solid #e2ded6;padding-bottom:.3rem}}
table{{border-collapse:collapse;width:100%;font:12px/1.4 ui-monospace,Menlo,monospace}}
td,th{{padding:.35rem .5rem;border-bottom:1px solid #e2ded6;text-align:left}}
tr.hi td{{background:rgba(138,75,42,.11);font-weight:700}}
.k{{color:#6b665e}} .box{{background:rgba(138,75,42,.07);padding:.7rem .9rem;
border-radius:6px;margin:.6rem 0}}
</style>
<h1>{pid}</h1>
<p class="k">{e.get('regime')} · {e.get('method')} · INR {(e.get('amount_minor') or 0)/100:,.2f}
· run <code>{dec.get('run_id')}</code></p>

<h2>1 · what was known</h2>
<div class="box"><code>{e.get('gateway_code')} / {e.get('gateway_reason')} /
{e.get('gateway_source')} / {e.get('gateway_step')}</code><br>
&ldquo;{e.get('gateway_description')}&rdquo;</div>

<h2>2 · what was permitted</h2>
<div class="box">permitted: <code>{perm.get('permitted_actions')}</code><br>
blocked: <code>{perm.get('blocked_actions')}</code></div>

<h2>3 · what was considered (best row per action)</h2>
<table><tr><th>action</th><th>chan</th><th>EV (INR)</th><th>p_success</th>
<th>obs</th><th>permitted</th><th>blocked by</th></tr>{''.join(rows)}</table>

<h2>4 · what bound the choice</h2>
<div class="box">chose <b>{dec.get('chosen_action')}</b>
{('via ' + dec['chosen_channel']) if dec.get('chosen_channel') else ''} ·
reason <code>{dec.get('decision_reason_code')}</code> ·
binding <code>{dec.get('binding_constraint') or 'none'}</code><br>
<span class="k">{dec.get('policy_version')} · {dec.get('model_version')} ·
{dec.get('score_basis')}</span></div>

<h2>5 · what executed</h2>
<table><tr><th>idempotency key</th><th>action</th><th>chan</th>
<th>revalidation</th><th>status</th><th>outcome</th></tr>{att}</table>

<h2>6 · what happened</h2>
<div class="box">{out.get('terminal_state')} ·
INR {(out.get('recovered_amount_minor') or 0)/100:,.2f} ·
attribution <b>{out.get('attribution')}</b> ·
{out.get('debits')} debits · {out.get('contacts')} contacts</div>

<h2>7 · ledger ({len(d['ledger'])} entries, hash-chained)</h2>
<table><tr><th>seq</th><th>event</th><th>actor</th><th>prev</th><th>entry</th></tr>
{''.join(f"<tr><td>{l['seq']}</td><td>{l['event']}</td><td>{l['actor']}</td>"
         f"<td>{l['prev']}…</td><td>{l['entry']}…</td></tr>" for l in d['ledger'])}
</table>"""

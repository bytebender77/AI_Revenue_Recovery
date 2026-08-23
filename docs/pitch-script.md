# 5-minute pitch script

Read aloud. `[PAUSE]` = stop talking, one beat. `[SCREEN]` = the picture changes.
Every spoken number carries its source line in `docs/results.md`.

**Sealed vs dev:** figures marked **SEALED** are the held-out test cohort, n=3000,
read once. Figures marked **DEV** are in-sample. Say the label out loud where it is
written — do not let a dev number sound held-out.

---

## Beat 1 — what I'm not claiming (45s)

**[SCREEN] `docs/report.html`, scrolled to the Arms table.**

### Version A — plainer (99 words, ~42s)

> This recovers failed subscription payments. Before any numbers, the thing I am not
> claiming.
>
> On the sealed test cohort, the agent recovered three point five million rupees.
> `results.md:259` **SEALED**
>
> That is not the number I report. Most of those payments would have come back on
> their own. Customers top up. Banks come back. If I count those, I am taking credit
> for the calendar.
>
> [PAUSE]
>
> So I run a control arm and subtract it. The number I report is one point seven
> million. `results.md:259` **SEALED**
>
> The gap is one point seven million rupees I could have claimed and don't.

### Version B — sharper (103 words, ~44s)

> Every recovery tool quotes a big number. Here is mine, and here is why I don't use
> it.
>
> Sealed test cohort, three thousand payments, read exactly once. The agent recovered
> three point five million rupees. `results.md:259` **SEALED**
>
> I don't report that. Most of those were going to recover anyway — the customer tops
> up on payday, the bank comes back. Counting them is taking credit for the calendar.
>
> [PAUSE]
>
> There's a randomised control arm. I subtract it. What's left is one point seven
> million. `results.md:259` **SEALED**
>
> The difference is one point seven million rupees of credit I'm throwing away. That's
> the honest half.

---

## Beat 2 — one payment, end to end (90s)

**[SCREEN] Terminal: `make serve`. Then browser:
`http://localhost:8080/audit/dev_pi_000040/html`.**
Renders seven sections: what was known, what was permitted, what was considered with
scores, what bound the choice, what executed, what happened, the ledger.

> ⚠️ **These figures are NOT in results.md.** results.md is cohort-level; this is one
> intent. Written source: `docs/shipping-config.md` §Demo intent. It is a **DEV**
> payment. Say "dev" out loud, as scripted. Everything here is also on screen as you
> speak, so you are reading the page, not quoting from memory.

(191 words, ~82s)

> One payment. This is a dev intent, not the sealed set. I picked it because it is the
> whole argument in one row.
>
> **[SCREEN] scroll to §1 what was known**
>
> Eighteen hundred rupees. A UPI checkout that failed. One-time payment — the customer
> tapped pay, it didn't go through.
>
> **[SCREEN] scroll to §3 what was considered**
>
> The policy's best option was to retry the card. It scored that at a hundred and
> forty-two rupees, from a cell with nineteen thousand observations. It was confident.
>
> [PAUSE]
>
> It wasn't allowed to. Rule R001. A one-time checkout carries no standing mandate, so
> there is no authority to debit again. Look at the row — the option is priced, and
> marked not permitted, with the rule that removed it.
>
> That's the design. The gate runs before the policy and hands it the legal moves. The
> policy never chose this and got blocked. It never had it.
>
> The alternate card went the same way. Escalation was below the value floor. One legal
> move left — an email, fifty-five rupees.
>
> **[SCREEN] scroll to §5 what executed**
>
> Then at send time it re-checked. The customer had already paid, ten minutes earlier.
>
> [PAUSE]
>
> It cancelled the email. Zero debits. Zero contacts. Recovered, marked organic — not
> credited to the agent.
>
> It wanted three things it wasn't allowed to do, took the one it was, then didn't do
> that either.

---

## Beat 3 — the sealed table (60s)

**[SCREEN] `docs/report.html`, Arms table and the dev→test panel together.**

### Version A — plainer (137 words, ~59s)

> The sealed cohort. Different merchants, different customers, later time window, an
> outage pattern the model never saw. Read once.
>
> The agent beats a good rules baseline by two hundred and forty thousand rupees,
> confidence interval seventy-one to four hundred and fourteen thousand.
> `results.md:42` **SEALED**
>
> And the margin roughly halved out of sample. On dev that lead was a hundred and
> twenty-nine thousand per thousand intents. On test, fifty-eight. Down fifty-five
> percent. `results.md:77` — dev **DEV**, test **SEALED**
>
> [PAUSE]
>
> Share of what the oracle could get: ninety-four percent on dev, eighty-two on test.
> `results.md:78`
>
> The baselines dropped less. Seven percent against my sixteen. `results.md:94` Rules
> don't overfit. Some of my dev performance was that cohort, not the method.
>
> It still wins. It wins by less than dev said.

### Version B — sharper (139 words, ~60s)

> Sealed cohort. Disjoint merchants and customers, a later window, an outage shape the
> model never trained on. Read exactly once — there's a receipt file that refuses a
> second run.
>
> Two results, same breath.
>
> The agent beats a competent rules baseline by two hundred and forty thousand rupees,
> interval seventy-one to four hundred and fourteen thousand `results.md:42` **SEALED**
> — and its margin over the better baseline fell fifty-five percent from dev to test.
> `results.md:77`
>
> [PAUSE]
>
> Oracle share went ninety-four to eighty-two. `results.md:78` The baselines only lost
> seven percent where I lost sixteen. `results.md:94`
>
> Rules don't overfit. Part of my dev lead was that cohort, not the method.
>
> I'm leading with the drop because you'd find it in ten minutes, and because a
> submission that only shows the dev number is telling you the wrong thing.

---

## Beat 4 — the rule that stopped working (45s)

**[SCREEN] `docs/results.md` §6, then §3 (per-cause table, `issuer_downtime` row).**

(103 words, ~44s)

> One comparison I'd point at.
>
> I wrote a hand-tuned outage rule for the baseline. N failures on one issuer, back
> off. On dev it worked — a hundred and three thousand, clearly significant.
> `results.md:213` **DEV**
>
> On the sealed set it stopped being significant. Interval crosses zero.
> `results.md:214` **SEALED**
>
> [PAUSE]
>
> The learned layer's outage win held. `issuer_downtime`, plus a hundred and eleven
> thousand, the biggest single cause. `results.md:128` **SEALED**
>
> Same problem. The threshold was fitted to one outage shape and the shape changed. The
> policy infers timing from elapsed-time structure, so it moved.
>
> That's the argument for the learned layer, and it's one comparison, not a law.

---

## Beat 5 — a bug the tests didn't catch (30s)

**[SCREEN] The audit page §5 "what executed" — attempt rows with idempotency key and
revalidation result.**

> ⚠️ No numbers here on purpose — this story has none in results.md.

### Version A — plainer (72 words, ~31s)

> One bug worth admitting.
>
> I wired the demo path straight to the database and skipped my own executor. So
> nothing was checking payment state at send time, and no attempt rows were being
> written.
>
> The full suite stayed green. Every test passed.
>
> [PAUSE]
>
> I found it reading the audit output and noticing a table was empty. Then I fixed the
> tests, not just the code.

### Version B — sharper (76 words, ~33s)

> One I got wrong.
>
> I hooked the demo path directly to the database and quietly bypassed my own executor.
> That killed the send-time re-check and wrote zero attempt rows — the exact mechanism
> I've just spent ninety seconds telling you about.
>
> Tests stayed green. All of them.
>
> [PAUSE]
>
> I caught it reading audit output and thinking, that table shouldn't be empty. Green
> tests aren't evidence. Reading what your system actually wrote is.

---

## Beat 6 — doing nothing, on purpose (30s)

**[SCREEN] `docs/report.html`, the NO_ACTION panel.**

(71 words, ~30s)

> Last thing.
>
> The agent never touches twenty-two point eight percent of payments. `results.md:223`
> **SEALED**
>
> And it records three different reasons for that, kept separate.
>
> A rule removed the option. Someone else's payment outbid it for a human agent. Or
> nothing on the table covered its own cost. `results.md:239`
>
> [PAUSE]
>
> Not-acting is priced at zero and competes. If it wins, that's a decision, and the row
> says which of the three it was.

---

## Timing

| beat | version | words | at 140 wpm |
|---|---|---|---|
| 1 | A plainer | 99 | 42s |
| 1 | B sharper | 103 | 44s |
| 2 | — | 191 | 82s |
| 3 | A plainer | 137 | 59s |
| 3 | B sharper | 139 | 60s |
| 4 | — | 103 | 44s |
| 5 | A plainer | 72 | 31s |
| 5 | B sharper | 76 | 33s |
| 6 | — | 71 | 30s |

**All-A: 673 words → 4m 48s speech.**
**All-B: 683 words → 4m 53s speech.**

Nine `[PAUSE]` marks at ~1.5s each ≈ 14s. **All-A lands ~5m 02s, all-B ~5m 07s.**

If you need to cut: beat 4's last line ("and it's one comparison, not a law") and
beat 2's "It was confident." are the two safest drops. That's ~6 seconds.

Read slower than feels natural on the numbers. 140 wpm is an average that includes
the pauses; digits eat more time than words.

# anima-blackbox · Agent Black Box (dual-ledger claim/action reconciler)

**In one line**: physically split what an agent *claims it will do* from what it *actually did* into two append-only ledgers, then expose the gap with an externally replayable reconciler. This is not a better log — it turns "walking the talk" into something a machine can decide.

Pure Python standard library, zero dependencies, zero production changes, reference implementation. MIT licensed.

> Status: **reference implementation**. Not a production system, not wired to a real execution chain, no OS-level account isolation. See [Boundaries](#boundaries).

[简体中文](README.md)

**Architecture & governance docs**: [Architecture whitepaper](docs/ARCHITECTURE.md) · [Aviation black box lessons](docs/AVIATION_LESSONS.md) · [Storage permission gate](docs/STORAGE_GATE.md)

---

## Thirty seconds, no pip / no network

```bash
git clone https://github.com/deanhan2026-lang/anima-blackbox.git && cd anima-blackbox
python3 -X utf8 selftest_b0.py     # 24/24 PASS
python3 -X utf8 canon_vectors.py   # canonical vectors ALL PASS
python3 -X utf8 chain.py           # segmented chain check 5/5 PASS
```

**Expected**: `24/24 PASS` / `ALL PASS` / `5/5 PASS`.

Verify the release bundle: `sha256sum -c MANIFEST.sha256` (macOS: `shasum -a 256 -c`).

---

## Why two ledgers, not one

A single log — however tamper-evident — can only prove "the record was not edited". It cannot prove "the subject did what it said", because claims and actions share one store: break in once, and both can be realigned to match.

This implementation keeps them apart:

| Ledger | Written by | Records | Independence |
|---|---|---|---|
| **A · Claim ledger** (`claim_log.jsonl`) | the audited subject itself | plan / intent / self_report | **weak**: "cannot be edited after writing" (append-only + chain + external anchoring), not "was true when written" |
| **B · Execution ledger** (`execution_log.jsonl`) | an independent sidecar process | action / consequence | **strong**: the subject has no write permission; it can only send messages |

**The gap is the signal**: said-but-not-done, done-but-not-said, out-of-scope parameters, outcome mismatch — all invisible in a single-ledger system.

---

## Architecture

```
subject ──write──> A (claims)  ─┐
                                ├─> reconciler ──> verdict unit ──> independently replayable
sidecar ──write──> B (actions) ─┘       ^ read-only, no append
watchdog ── detects sidecar loss / missing log segments
```

Key design choices:

- **The reconciler is a downstream consumer, not a component of the black box.** The black box produces only the two ledgers and an event stream — never a verdict. Anyone can write their own reconciler against the same rules for cross-checking, instead of "replaying the reconciler we wrote".
- **The reconciler emits a replayable verdict unit, not a prose report** (isomorphic to the DVEF-001 format): `inputs` / `params` / `output`, each with a digest; `params` is a closed enumeration with no timestamps or randomness. An external party can replay digests and verdict legality from the unit alone.
- **Three verdicts**: `MATCH` / `MISMATCH` (score = number of gaps) / `INCONCLUSIVE` (missing ledger, broken chain, unresolvable scope). **An empty ledger is never "no gap".**
- **Four gap classes**: `MISMATCH_SET`, `MISMATCH_SCOPE`, `MISMATCH_CONSEQUENCE_STRUCTURED` are strongly replayable and enter the verdict; `MISMATCH_CONSEQUENCE_NARRATIVE` (prose-only outcome) is weakly replayable and can only yield `INCONCLUSIVE`. `evidence_strength` enters `inputs_digest`, preventing "strong and weak evidence reaching the same verdict" from being mistaken for equal trustworthiness.
- **Segmented chain verification**: legacy entries use JSON insertion order (equivalent to JS `JSON.stringify`), new entries use sorted keys; `legacy_till` is supplied explicitly by the caller, never guessed. A negative control is included: verifying a legacy ledger with the wrong mode must fail — a checker that only ever passes is not a checker.

---

## Files

| File | Role |
|---|---|
| `chain.py` | append-only hash chain primitive (seq / prev_hash / entry_hash) + segmented verification |
| `ledgers.py` | dual-ledger writes, writer_token permission separation, closed field enums |
| `reconciler.py` | claim/action gap reconciliation → replayable verdict unit (seal / replay) |
| `watchdog.py` | sidecar-loss detection, missing-segment marking |
| `canon_vectors.py` / `.json` | cross-implementation canonical alignment vectors (key order / non-ASCII / escapes / numbers) |
| `selftest_b0.py` | full self-test covering every criterion below |
| `MANIFEST.sha256` | release bundle integrity manifest |

---

## Acceptance criteria (each covered by self-test)

| Criterion | Coverage | Result |
|---|---|---|
| 1. Three gap classes detected | SAID_NOT_DONE / DONE_NOT_SAID / PARAM_OUT_OF_SCOPE + MATCH + empty-ledger INCONCLUSIVE | PASS |
| 2. sidecar killed → Watchdog marks missing segment | heartbeat declares seq=3, ledger stops at 1 → `E-SEGMENT-MISSING, missing_from=2`; subject cannot forge heartbeat | PASS |
| 3. Event stream mechanically maps to inputs/params | params carry no timestamp / randomness | PASS |
| 4. Process-level permission separation | subject writes A but not B; reconciler has no write access; cross-ledger writes 5/5 rejected | PASS |
| 5. Gap is a verdict unit; negative sample detected | seal three states + digests; replay consistent; forged gap=0 → `E-OUTPUT-DIGEST` | PASS |
| Weak evidence cannot enter a strong verdict | narrative-only outcome gap → `INCONCLUSIVE`; `evidence_strength` in inputs_digest; narrative excluded from stored digest | PASS |
| Chain integrity / tampering / scope-by-reference / field enums / anchoring digest | tampered entry_hash fails recomputation; forged scope rejected; head digests anchorable | PASS |

**Two real bugs the self-test caught and we fixed** (recorded honestly): ① the test script passed `scope` to the wrong function (test bug, not product); ② **a real interface mismatch** — `authority_level` hidden inside `content` caused out-of-scope execution to go undetected; fixed by promoting it to a top-level structured field included in the chain digest. Lesson: an authorization level must be a discriminable top-level field, not buried in content.

---

## Boundaries

1. **Reference implementation, not a production system**: no real agent execution chain; events are constructed by the self-test.
2. **Process isolation is simulated**: permissions via `writer_token`; real OS account / file permission isolation is out of scope.
3. **Ledger A is inherently weaker than B**: A is the subject's own account; "cannot be edited after writing" ≠ "was true when written". This detects self-deception only if declarations actually land in an immutable ledger.
4. **Finite depth**: `replay()` has two levels — without a scope registry it performs digest-level checks only (`CL1-digest-consistency`), proving "not tampered with" but **not** that the verdict is correct; with the same-version registry it actually re-runs the reconciliation and compares outcomes (`CL2-semantic`), which is the only level that qualifies as a replayable verdict unit. The registry is given by digest, never in plaintext; external parties must hold the same version themselves. We do not claim to solve "who verifies the verifier".
5. **Anchoring is an interface, not an implementation**: head digests are anchorable, but the bundle contains no real external anchoring / dispatch.
6. **Tamper-evident ≠ true**: a hash chain guarantees internal consistency only; it cannot stop two ends colluding to rewrite and rebuild the whole chain. External anchoring and independent recomputation nodes are the source of external validity — neither is provided here.
7. **Tamper-proof ≠ forgery-proof**: append-only prevents editing the past (tamper), not fabricating that something happened (forge) — a record can be complete and the chain valid while "the subject was aware of this event" never occurred. `attestation.py` provides a sandbox prototype: writes carry the writer's own conclusion summary; delegated signatures are marked and downgraded to weak evidence. System-level distinction of real credential holders is out of scope (requires OS account isolation).
8. **Records, not judgments**: the system can prove each action occurred, not the intent behind a combination of actions; verification-chain trust depends on independence, not verifier capability; coverage depends on deployment constraints (the recorded subject must not be able to leave the recording scope). The stronger the subject, the sharper the line between "recording" and "judging".

**Runnable criterion checkers** (read-only tools, never write): `verify_b1.py` checks "the executing subject cannot write the log directory" (4/4 self-test); `verify_mechanism.py` checks that the write path is mechanical with no discretion — determinism, reserved-field rejection, no self-sourced clock, no skipped events, closed enums, tamper detection (9/9 self-test). `attestation.py`'s forge-protection prototype: 9/9 self-test. **These prove only that this reference implementation and the checking means work — not that any external node is mechanical or has achieved B1**; mechanical ≠ trustworthy: it proves fixed rules, not true inputs.

---

## License

MIT © 2026 ANIMASTELLAR TECHNOLOGY (SHENZHEN) CO., LTD. See [LICENSE](LICENSE).

Related recomputable format: [DVEF-001](https://github.com/deanhan2026-lang/dvef-001).

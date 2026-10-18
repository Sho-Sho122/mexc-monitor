# v1.1 FINAL RECONCILED implementation map

The approved spec is stored byte-for-byte in this directory. User approval in the
implementation task supersedes its historical “not yet authorized” status note;
the frozen source document has not been rewritten.

| Spec | Implementation | Verification |
|---|---|---|
| §0–4, β1 isolation / no live orders | Separate `model511_adaptive` package, read-only legacy imports; parameterized Exit copy | Existing 87-test suite; beta1 Reference parity; tracked-file diff |
| §5–7, three clocks / anchor / scan identity | `config`, `scheduler`, `runner` | Fixed due slots, catch-up, duplicate scan/Gate tests |
| §8–17, integrity and research layers | `source_reader`, `analysis`, `statistics` | Real four-strategy and archived Exit CSV smoke test; source errors remain blockers |
| §14–15, 60 Exit variants / plateau | `source_reader.exit_analysis`; neighbor cells and per-strategy diagnostic reports | 60 variants counted as one source trade in fixture; no automatic cell selection |
| §18–24, all trials / graveyard / no fine search | `registry.trial`, `register`, `reject`, `candidate_count` | Unknown counts and unlogged losers unresolved; duplicates and rejected records retained |
| §25–28, freeze / future-only start | `registry.freeze`, `forward` | Canonical hash, new identity/clock, unavailable/outcome features rejected |
| §29–36, Gate / repeated reviews / multiplicity | `gate`, `statistics.holm` | Minimum evidence, unresolved multiplicity, data blocks, human-only promotion, max one beta |
| §37–41, beta registry / cumulative vs post-beta | `gate.promote`, `reporting`, `forward.cohort` | Same hash inherits evidence, post-beta uses entry boundary, no double counting |
| §41, costs before Practice/Final | `forward`, `gate.practice`, CLI prospective settings | Missing Fee/Slippage/quotes => NULL; later settings do not fill historic Net |
| §42–48, robustness / cluster statistics | `statistics` | LONG/SHORT merged per 30-minute time cluster; day and seven-day diagnostics; no subjective automatic veto |
| §49–51, Final | `final_confirmation`, holdout filtering in `runner`/`forward` | Daily cutoff, pending Reference, first ready decision, delayed-job replay, seal, no retry/reuse |
| §52–53, qualification report | `final_confirmation` notification payload | Only formal PASS generates `PRACTICE_CANDIDATE_READY`; no automatic sending |
| §54–56, Micro Live / decay | Explicitly outside this implementation's execution capabilities | No order functions; decay diagnostics remain INSUFFICIENT until separate Live rules exist |
| §57–61, authoritative state / features / provenance | `storage`, `source_reader`, feature registration CLI | Atomic failure recovery, projection rebuild, schema/spec rejection, availability timestamps |
| §62–65, roles / audit / no main logic rewrite | Registry requests, approval audit, independent workflow save | Explicit human approval; temp Git index updates only Adaptive data path |
| §66–69, CLI / workflows / reports | `__main__`, `reporting`, independent Actions files | `--as-of` forbidden for Forward; legacy workflow untouched; opt-in activation |
| §70–75, anti-contamination tests | `tests/model511_adaptive` | Boundary, identity, timing, costs, approval, state, workflow-isolation suites |
| §76–82, frozen implementation policy | Copied spec, this map, operations guide, tests | No performance-based tuning or changes to β1 / legacy strategy logic |

## Implementation choices (not fitted research criteria)

- Bootstrap: deterministic seed 511, 2,000 draws, event-weighted percentile 95% CI.
- Multiplicity: permitted Holm fallback with a null-centered time-cluster bootstrap
  p-value and the full recorded candidate count. No new promotion p threshold.
- Model filters are explicit, registered conjunctions; the program does not discover
  thresholds or rank historical winners. Human Discovery can register hypotheses
  after recording every trial. Zero hypotheses remains a successful scan.
- Source observations without defined categories are numeric diagnostics or UNKNOWN;
  no new category boundary is inferred from results.
- New Adaptive schema initializes alongside β1. Unsupported versions fail explicitly;
  no destructive migration of beta1 state is provided or necessary.

## Operational limitations

- The initial local verification lacked beta1 Forward state. The later audit of
  saved commit `a4eda44fdf6e4e34b0e659682eac3ad71dc6fb26` confirms that state exists,
  but still finds 1,531 invalid scan times and zero Net-evaluable beta1 results.
  This remains blocked Discovery, not a live Adaptive Forward qualification.
- Real quote collection and actual GitHub Actions execution have not been activated
  by the local test run. Synthetic recorded-market tests cover Forward settlement.
- Fee/Slippage settings remain a separate approval task. No production setting was
  written, no historic Net was imputed, and no model was actually promoted.
- Formal daily JST time is mandatory at Final start, with no guessed default.
- CI PASS is software verification, not empirical qualification of a trading model.

## Publication persistence hardening

`persistence` prepares all formal input groups from one commit, archives Adaptive
output outside the checkout, validates state/projections, and performs full-group
compare-and-swap publication with transport reconciliation. `workflow_save` rejects
the obsolete unprepared API. Existing `research_persistence.py` is unchanged.
The independent copy deliberately owns Adaptive writes only; future fixes shared
with Research persistence must be reviewed and tested in both implementations.

`test_persistence_safety.py` covers concurrency, stale checkout, failed transport,
partial preparation/projection/archive writes, corrupt Recovery, quarantine replay,
CLI exit codes and both orders/races with Research and beta1 saves. The workflow
requires a successful Recovery upload before publication and always attempts final
audit upload. Service outage/runner destruction can still prevent an artifact
upload; no implementation can guarantee storage after the runner is lost.

# 511 Adaptive v1.1 FINAL — operations

This is an independent shadow research package. It never edits the four strategies,
Exit Research, `model511`, HYBRID or LAB. It cannot submit orders. Do not enable the
workflow merely because the software tests pass: input and execution blockers still apply.

## Initial activation and clocks

Python 3.11+ and the existing requirements are sufficient. From the repository root:

```sh
python -m model511_adaptive init
python -m model511_adaptive tick --fetch-market
python -m model511_adaptive report
python -m model511_adaptive validate-state
```

`init` records the current real JST anchor once, and refuses to overwrite state.
`tick` imports the frozen beta1 events, records Adaptive membership before network
retrieval, settles Adaptive shadow trades, runs due 5-day scans and 15-day Gates,
and runs due Final evaluations. Public market GETs are the only exchange access.
`update`, `scan`, `gate`, `final` can run each component separately.
Formal Forward rejects `--as-of`. Scans/Gates use anchor-based scheduled cutoffs,
not a period selected after inspecting profit. Delayed scheduled jobs keep both
scheduled cutoff and actual processing time. Final replays missed daily cutoffs
in order using only evidence already available at those cutoffs, with a workflow
delay/outage audit, so a later profitable cohort cannot replace the first ready one.

The input beta1 state defaults to `511_data/forward/511_state.json`. Missing,
corrupt, incompatible state is not reconstructed as live Forward from old CSV.
Source CSVs and archived Exit CSVs are read-only; identical archive rows are
deduplicated. Conflicting records, malformed timestamps, missing required sources
and state/projection mismatches prevent new research. Missing diagnostic values
are shown, without fabricated severity thresholds. Naive timestamps in legacy
CSV are interpreted by its existing JST schema; Adaptive timestamps require offsets.

## BACKFILL and offline replay

`BLOCK_RESEARCH` is a shared transition precondition, including direct calls to
Freeze, Promotion, Forward update and Final. Transition CLI commands refresh and
persist source integrity before proceeding. Reports, validation and diagnostic
scans remain available. A fresh PASS assessment does not erase a blocked latest
scan: rerun a successful diagnostic scan after repairing the actual inputs.

Use a separate directory and a recorded cutoff:

```sh
python -m model511_adaptive init --mode BACKFILL --as-of 2026-09-21T00:00:00+09:00 --state-dir work/adaptive-replay
python -m model511_adaptive scan --mode BACKFILL --as-of 2026-09-26T00:00:00+09:00 --state-dir work/adaptive-replay
```

For offline shadow tests, also supply a **BACKFILL** beta1 source via `--beta-dir`
and recorded market JSON via `--market-file`. `--fetch-market` is forbidden in
BACKFILL. Historical scans are Discovery, never live Forward. BACKFILL cannot
promote or start Final. Do not move its state into the Forward directory.

## Hypotheses and human decisions

The scan does not manufacture a winning hypothesis. Zero new hypotheses is a
successful outcome. Read the diagnostic report, record **all** examined candidates
using `record-trial --request request.json`, then use `register --request hypothesis.json`.
The CLI validates required request fields; the exact schemas are in
`registry.trial` and `registry.register`. Each request has a caller-supplied
idempotency ID. Reusing it with changed content fails.

Trial fields include scan/family IDs, dataset start/end, features, parameters,
candidate family size/index, result summary, selection flag/reason and count
quality (`EXACT` or `UNKNOWN`). All family indices must be recorded; otherwise
multiple testing remains unresolved. Failed, unselected, rejected and duplicate
hypotheses remain in the authoritative ledger. Hypothesis requests include parent,
full model JSON, a single major conceptual change, economic rationale and threshold
rationale. No threshold search or automatic historical Exit selection is implemented.

```sh
python -m model511_adaptive freeze --id HYP-000001
python -m model511_adaptive gate
python -m model511_adaptive approve --gate-id GATE-000001 --id CH-000001 --request human-approval.json
```

An approval file must contain `actor`, `reason` and `confirmed_human: true`, and
must represent an actual human decision. Do not let an agent generate approval
without explicit human authorization. Only Eligible candidates can be selected.
One Gate can create at most one beta; no selection leaves `NO_CHANGE`. Repeated
approval with the same record is idempotent. Different approvals cannot replace it.

The model schema has `entry` (conjunction of field/op/value predicates), `exit`
(`sl_pct`, `rr`, `max_hours`), `feature_schema_version`, `universe: ALL_OBSERVED`,
`reference_policy: BETA1_BOUNDARIES`, `cost_policy: OBSERVED_EXPECTED_NOTIONAL`.
Allowed operations are eq/in/ge/le. Only declared event features may be used;
outcome fields are forbidden. Exit values are exactly the specified 60 variants.
Conditions are canonicalized before hashing. Frozen conditions cannot be edited.

`register-feature --request feature.json` requires a name and definition, records
the actual availability time and increments feature schema version. Registration
does not synthesize a historical observation: future model events must actually
contain the feature, and unavailable values make membership ineligible with a reason.
Known raw RSI/EMA/MACD/ATR/Volume/BTC observations can be linked only through the
exact beta1 reference row hash. Asset class is inherited from trusted source data;
names are never classified. UNKNOWN is retained.

## Forward and costs

The first complete five-minute bucket **after** Freeze starts Forward. Promotion
does not reset it for the same hash. Cumulative and post-promotion-entry summaries
are separate, overlapping reports and must not be added as independent samples.
Late event changes do not rewrite membership; new late arrivals behind the
watermark are audited. Resolved Reference/Execution results are immutable.

Fee/Slippage configuration is a **separate operational approval**:

```sh
python -m model511_adaptive set-cost-config --request official-cost-settings.json
```

The file requires `source`, `approved_by`, and `settings` containing the official
`fee_bps_entry`, `fee_bps_exit`, `slippage_bps_entry`, `slippage_bps_exit`. No defaults
are supplied. Explicit documented zero is allowed; missing is not zero. Settings
are frozen per entry, so a later configuration never fills historic missing Net.
Practice/Final are blocked until settings and their required 100% Net completeness
are satisfied; historic incomplete trades cannot simply be hidden.

Gross uses the Reference entry/exit and frozen SL risk unit. Spread is the signed
reference-to-observed-quote execution impact (including delayed market movement),
not an invented pure spread width. Fee and Slippage are independent notional costs.
Adaptive scales the beta1 cost components to its own frozen SL risk and subtracts
each once. Missing quote or required component leaves official Net NULL. Quote
times and delay are retained. BACKFILL never estimates missing exit quotes.

## Statistics and Final

Gate: 10 Forward days / 150 resolved events / 60 time clusters / 7 active days.
Practice prequalification: 28 days since Freeze / 400 / 150 / 20, Net completeness
100%, positive Net EV and positive lower 30-minute bootstrap CI.
Final: at least 14 days / 150 / 60 / 10, Net completeness 100%, no pending Reference.
None of these thresholds is fitted to data.

Formal clusters combine LONG, SHORT and all symbols within the same 30-minute JST
bucket. Bootstrap uses 2,000 draws with seed 511, event-weighted means and percentile
95% intervals; these implementation settings are stored with Final. Day-cluster CI
and seven-day/symbol/direction/regime diagnostics do not create extra veto rules.
Gross/Net DD and streak bases are explicitly identified.

The initial multiple-comparison implementation uses the spec's Holm fallback,
instead of joint max-statistic resampling across unequal candidate histories.
Null-centered cluster-bootstrap one-sided p-values are Holm adjusted across the
full recorded candidate count; untested candidates are counted conservatively.
At Gate only, p-values describe the Net-evaluable subset with its coverage shown;
they do not turn partial-coverage EV into official full-cohort Net EV. The Practice/
Final 100% threshold is not silently imposed on Gate as an additional criterion.
Incomplete trial accounting or unavailable p-values keeps
`MULTIPLE_TESTING_UNRESOLVED`. No adjusted p-value is an automatic promotion ranking
or a newly invented significance threshold. Gate repetition is logged and is not
claimed to be alpha-spending or a sequential significance test.

```sh
python -m model511_adaptive start-final --id CH-000001 --daily-time-jst HH:MM:SS
python -m model511_adaptive final
```

Supply an actual human-fixed JST time before starting; `HH:MM:SS` is a placeholder,
not a default. Final uses `start <= entry < scheduled cutoff`, excluding pre-Final
entries even if they close later. A pending Reference cannot be dropped to PASS.
Delayed membership/materialization does not remove a matching frozen-model Entry
from this population. Missing evidence remains pending; outcome information first
available after cutoff is masked at that cutoff. These temporary population rows
do not rewrite stored Forward membership or evidence.
Final also requires the source's completed five-minute bucket watermark to cover
the cutoff. Missing or stale coverage yields `ENTRY_POPULATION_INCOMPLETE`, even
when all currently materialized trades have resolved. Older state without this
watermark waits for a valid Forward update; completeness is never inferred from
trade counts.
Each model retains its own frozen 3/6/12/24-hour deadline. Missing prices are not
invented when a deadline passes. Resolved UNKNOWN is distinct from pending.
Before readiness, reports expose readiness counts without repeatedly testing Final
significance. On the first ready cutoff, Net EV > 0 and CI lower > 0 yield PASS,
otherwise FAIL; the formal decision is sealed once. PASS/FAIL cannot be extended
or retried for that same model. Reserved Final periods are excluded from Discovery,
Gate selection evidence and other models' Final periods.
`PRACTICE_CANDIDATE_READY` is generated as a local state/report record, not sent to
third parties. It never enables live trading. Live decay thresholds remain unset
and diagnostics say INSUFFICIENT; Micro Live requires its own specification.

## State, restart, failure recovery

Adaptive state now uses schema 2. Reading schema 1 performs a deterministic
structural migration with an audit and original-state hash; it never fabricates
timestamps or historical outcomes. Persist this migrated state with the normal
locked writer. Restarting before or after persistence is idempotent. Unknown future
schemas and implicit downgrades are rejected.

Authoritative source history, including its start and completed-bucket coverage,
defines certification Entry populations. A seen ID does not prove that its body
exists. Hash-identical source bodies can restore missing legacy bodies; missing or
conflicting source records remain BLOCK/PENDING. Recovery records list restored
and unresolved IDs. Do not erase missing IDs to make the denominator smaller.
Historical membership materialization does not supply source completeness.

Every frozen model has a canonical effective semantic snapshot: Entry, referenced
immutable Feature definitions/versions/availability, Universe, Direction predicates,
Exit/holding deadline, and decision-engine identities. Evaluation never resolves a
frozen model through the current Feature Registry. Feature availability is semantic;
report timestamps, display metadata, paths and Registry insertion order are not.
An identical feature re-registration is a no-op; a different definition under the
same name or a built-in name collision is an error. Use a distinct versioned name
and a new Challenger for changed semantics. Changing a decision engine invalidates
the old model's semantic proof rather than silently transferring evidence.

Legacy models without sufficient frozen semantic provenance stay unresolved; the
current Registry is never used to invent their old definitions. Historical sealed
Final decisions with unresolved semantics cannot be reused as current certification.
This is a software migration, not approval to relabel old evidence with a new hash.

Final, Practice, Gate and human Promotion share the completeness guard: frozen
identity, source window coverage, missing Entries, temporal availability, pending
Reference outcomes, BLOCK_RESEARCH and Net provenance are checked. Final/Practice
require the existing 100% resolved-Gross Net coverage. Gate retains its existing
reported Net-evaluable subset rule; this fix does not impose a new Gate coverage
threshold. A reported non-NULL Net must have costs, execution provenance and correct
single subtraction. Missing information never defaults to zero.

Mutable after Freeze: diagnostic labels, reports, audit, prospective execution
settings for future Entries and independent new feature registrations. Immutable:
membership/Feature/Regime/Universe/Direction/Exit/holding semantics and model hash.
Per-entry costs and resolved outcomes remain immutable. Changes affecting frozen
semantics require a new semantic identity and Challenger, never in-place mutation.

`511_adaptive/forward/adaptive_state.json` is authoritative. BACKFILL uses a separate
subdirectory. OS locks prevent concurrent writers; fsync and atomic replacement
commit state before CSV projections. Restart reruns the same command; IDs, due
slots, membership, outcomes, approvals and Final decisions prevent duplication.
`export` rebuilds all projections and the audit log from state. A projection failure
does not roll back committed state. Corrupt or incompatible state is rejected,
never silently initialized or migrated over beta1. Back up before any future schema
migration; this first Adaptive schema initializes alongside beta1 without changing it.

Failures produce `last_failure.json`; Actions also preserves recovery artifacts.
Do not repair a conflict by force pushing, resetting research files or concatenating
JSON states. Validate the authoritative remote Adaptive state, retain the failed
artifact, and rerun against the latest state. Unrecoverable state/provenance requires
an explicit reviewed recovery; do not reset Forward clocks to manufacture evidence.

## GitHub Actions (not activated during local implementation)

`model511_adaptive.yml` is a separate job, triggered after MEXC Research or by its
own timer. It is disabled unless repository variable `ADAPTIVE_511_ENABLED=true`.
Existing `research.yml` is unchanged. Failure cannot cancel or roll back completed
legacy saves. Adaptive first refreshes Research, beta1 and Adaptive inputs from
one fetched commit, retaining that source SHA. It archives output and uploads
Recovery before publication. Saving compares the entire Adaptive path against its
baseline, builds a temporary Git index on the latest remote tree and retries only
unrelated writers. Identical accepted output reconciles a lost push reply without
another commit. Adaptive conflicts stop; stale state is never merged or overwritten.
There is no force push, checkout/reset/restore, or git-config mutation in this helper.
See `511_ADAPTIVE_PUBLICATION_RUNBOOK.md` for quarantine Recovery and Shakedown.
The older `workflow_save.save(root, baseline)` API now fails closed: baseline alone
cannot prove pre-computation refresh or completed Recovery.

Before enabling: validate real inputs, back up state, review the independent
workflow, and verify public data connectivity. Fee configuration is required before
Practice/Final, not before software/diagnostic operation. Local implementation does
not enable the variable, launch Actions, commit, or push.

## Tests

```sh
python -B -W error -m unittest discover -s tests/model511 -v
python -B -W error -m unittest discover -s tests/model511_adaptive -v
python -B -W error -m unittest discover -s tests/persistence -v
```

The independent test workflow runs all three suites (PyYAML is a test dependency).
Persistence tests push only to disposable local bare Git repositories. They do not
access exchange order endpoints, operational state or GitHub.

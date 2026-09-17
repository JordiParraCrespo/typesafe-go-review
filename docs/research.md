# Research: a TypeSafe-powered Go review CLI for xrpl-go

Date: 2026-09-17
Status: research + feasibility experiment (no product code yet)

## 1. Goal

Build a CLI (working name `gorev`) that reviews Go code against the conventions
xrpl-go already follows, gives every function (and every file / package) a
score per rule, and then aggregates those scores into a repository health score
that can gate CI. The scoring itself is done by TypeSafe's System One model
(Jev), which returns calibrated numbers instead of prose, so the aggregation is
pure arithmetic under our control.

## 2. What TypeSafe actually is (verified against docs and a live call)

TypeSafe AI ships **Jev**, a "System One" model. It does not generate text. You
send a `state` (string or JSON) plus a map of typed `questions`; it returns one
typed answer per question, with a probability distribution and a confidence.

- Endpoint: `POST https://api.typesafe.ai/v1/systemone`, header
  `Authorization: Bearer <key>`. Model `jev-latest` (currently `jev-1.13.0`),
  `jev-preview` also listed by `GET /v1/models`.
- Three primitives:
  - **Noul**: yes/no, returns `noul` in 0..1.
  - **Choice**: pick one of N labelled options, returns `choice`,
    `probabilities`, `confidence`.
  - **Score**: ordered rubric of 2..10 described levels, returns `score`
    (probability-weighted position, e.g. 2.63 on a 0..3 scale), `probabilities`,
    `legend`, `confidence`.
- Any number of questions per request, all evaluated in parallel on the same
  state, so batching is nearly free in latency. Questions cannot see each
  other's answers.
- `instructions` and rubric levels may be JSON objects, and instructions can
  reference nested state paths in backticks (`functions.f2`).
- Text only. State size is not documented as limited; rate limits are
  250k tokens/s and 1,200 req/min per account (dynamic).
- Price: $0.042 per million input tokens, output free.
- SDKs: Python (`typesafe-sdk`) and JavaScript (`@typesafe-ai/sdk`). **There is
  no Go SDK.** The HTTP API is tiny, so a Go client is a ~150 line package.
- Errors: 401, 422 (validation, names offending field), 429, 529; retry with
  exponential backoff and honour `retry-after`.
- Official guidance that maps directly onto a code reviewer:
  - "Composite scoring": split a judgement into one Score per dimension, then
    combine with weights in code.
  - "Confidence-gated routing": act automatically above a threshold, flag for
    human review below it.
  - "Speculative fan-out": ask every question up front, decide relevance in code.
  - Rubric levels must describe *situations*, not degrees ("errors ignored or
    panics" beats "bad error handling"); one dimension per Score question.
  - Put all questions and thresholds in a single reviewable file.

Sources: https://docs.typesafe.ai/api, https://docs.typesafe.ai/primitives/score,
https://docs.typesafe.ai/patterns/composite-scoring,
https://docs.typesafe.ai/patterns/confidence-routing,
https://docs.typesafe.ai/confidence, https://docs.typesafe.ai/models,
https://docs.typesafe.ai/agent-skill.

## 3. Feasibility experiment (run today with the provided key)

Files under `experiments/` hold the exact requests and responses. The key is
not stored anywhere in this repo.

**Setup.** State = `{conventions: <one paragraph of xrpl-go rules>, code:
<function source>}`. Questions: three Score rubrics (error handling,
validation completeness, documentation), one Noul (magic numbers), one Choice
(approve / request_changes / needs_human_review).

**Inputs.** (a) the real `Payment.Validate` from
`xrpl/transaction/payment.go`; (b) a degraded copy that panics, uses
`errors.New` inline, skips helpers, ignores a return value, and compares
`Flags & 131072`.

| Question | Real function | Degraded copy |
|---|---|---|
| error_handling (0..3) | 2.89, conf 0.89 | 0.01, conf 0.99 |
| validation_completeness (0..3) | 2.62, conf 0.62 | 0.99, conf 0.84 |
| documentation (0..3) | 2.96, conf 0.96 | 0.11, conf 0.89 |
| uses_magic_numbers (noul) | 0.04 | 0.98 |
| verdict (choice) | approve, conf 0.35 | request_changes, conf 1.0 |
| usage | 1,314 in / 107 out tokens | 1,013 / 108 |

**Batching.** Three functions in one request under `state.functions.{f1,f2,f3}`
with per-function questions: 1,413 input tokens, 0.37 s wall clock, same
verdicts (f1 2.63, f2 0.03, f3 2.91 on error handling; magic-number noul 0.07 /
0.98 / 0.06).

**Takeaways.**
- Separation between compliant and non-compliant code is large and confident.
- The low-confidence `approve` verdict (0.35) on the real function is the
  interesting result: a coarse Choice over a whole function is ambiguous, while
  the atomic Scores are sharp. This confirms the docs' advice: score dimensions
  separately and compute the verdict in code, don't ask the model for it.
- Cost: about 1.2k tokens per function per rule-pack, so scoring **all 1,268
  non-test functions of xrpl-go costs roughly 1.5M tokens, about $0.07**, and
  can run at a few hundred functions per second within rate limits. A PR-scoped
  run is effectively free.
- Real `checkPartialPayment` uses `IsFlagEnabled(tx.Flags, tfPartialPayment)`;
  the model correctly did not flag it, so named-constant detection works from
  source alone.

## 3b. Granularity experiment: slices and atomic questions

Same function, same two rubrics, four ways of framing the state and questions.

| Framing | validation_completeness | error_handling | input tokens |
|---|---|---|---|
| A. whole `payment.go` (300 lines) | 1.96, conf 0.52 | 2.76, conf 0.76 | 2,800 |
| B. function slice only | 2.69, conf 0.69 | 2.59, conf 0.59 | 980 |
| C. slice + sibling slices (struct field list, flag consts) | 2.24, conf 0.61 | 2.66, conf 0.66 | 1,253 |
| D. slice + one Noul per struct field ("does it validate `X`?") | see below | n/a | 1,118 |

Row D returned, per field: Amount 0.99, CredentialIDs 0.98, DeliverMax 0.99,
DeliverMin 0.99, Destination 0.99, Paths 0.98, SendMax 0.99, DomainID 0.98,
**DestinationTag 0.05, InvoiceID 0.04**. That is correct: the real
`Payment.Validate` never checks those two fields. The rubric in row B "felt"
this as a 2.69 with 0.69 confidence; the atomic questions state it as ten
near-certain facts.

Conclusions, which change the design:

1. **Never send whole files.** Confidence drops and tokens triple. The unit is
   the smallest slice that contains the evidence for the question: a function,
   a `const` block, a `var ( Err... )` block, a struct definition, a test table.
2. **Prefer atomic Nouls to graded Scores wherever the rule decomposes.**
   "Validates every field" is really N questions "validates field i", and the
   list of fields comes from the AST for free. Score/(levels-1) becomes a plain
   ratio: checked fields / total fields, with a named finding for each miss.
   Scores stay for the genuinely graded things (doc comment quality, whether an
   error type matches its siblings).
3. **Adding context slices does not help by itself** (row C scored lower than
   B). Add a sibling slice only when a question references it explicitly, as
   the per-field Nouls did with `struct_fields`.
4. Atomic questions also fix the reporting problem: a finding can now say
   "InvoiceID is not validated; add `IsHash256`-style check" at a line, which a
   2.69 never could.

## 3c. Slicing model

| Slice kind | Produced from | Questions it receives |
|---|---|---|
| `func` / `method` | `*ast.FuncDecl` | error handling, helper usage, per-field Nouls (fields from the receiver struct), magic numbers |
| `struct` | `*ast.TypeSpec` with struct type | per-field doc comment, tag policy, pointer-for-optional, BaseTx first |
| `const block` | `*ast.GenDecl` (CONST) | flag naming, typing, doc per const |
| `errors block` | `*ast.GenDecl` (VAR) in `errors.go` | naming, message style, grouping, duplicates vs sibling package |
| `test func` | `*ast.FuncDecl` named `Test*` | table shape, subtest names, require vs assert, ErrorIs |
| `file` | only for layout rules | one type per file, sibling test exists (static, no model) |

A request carries one primary slice plus only the sibling slices that a
question names by path. Questions are generated per slice from the rule's
template plus AST facts (field names, constant names, sibling method names).

## 4. Reference tools and what to borrow

### alibaba/open-code-review (Apache-2.0)
AI PR reviewer from Alibaba's internal system. Hybrid design: deterministic
pipeline (exact file selection, bundling related files into review units,
template-based rule-to-file matching, an external "positioning" module that
fixes line numbers, a "reflection" module that re-checks each comment) around an
LLM agent. Claims higher precision than a general agent at ~1/9 the tokens.
CLI: `ocr review [--from A --to B | --commit X] [--format json]`, `ocr scan`
for whole files, sessions that can be resumed, a "delegate" mode where the host
coding agent brings its own model.

Borrow: diff-scoped vs full-scan modes, rule matching by path before any model
call, JSON output plus sessions, delegate mode as a later option. Their
precision-over-recall stance is the right default for a CI gate.

### shadcn-ui/lint (agent-first linter)
AST-based, no LLM. Its contribution is the *shape of the message*: every error
says what is wrong, what to use instead, and where to change it, with
placeholders filled from the project's own design tokens. Rules are declarative
"contracts" per component pattern. Benchmark: agents reach zero violations in
one correction round and spend 10-48% fewer tokens than with prose rules alone.

Borrow: every finding must carry a fix hint that names the project's own helper
(`use IsAmount(...) from xrpl/transaction`), and rules are data, not code.

### millionco/react-doctor
Deterministic scanner with six categories (state/effects, performance,
architecture, security, accessibility, maintainability), a terminal summary,
`--format json|jsonl` for agents, `doctor.config.ts` for rule selection, an
`install` command that drops a skill into Claude Code / Cursor, and a CI mode
that reports only issues introduced by the PR (baseline diff). Telemetry
reports rule names fired, never code.

Borrow: the health-score presentation, category grouping, the "new issues only"
CI baseline, and the `install` skill so agents learn the rules.

### Positioning of our tool
golangci-lint already covers the mechanical rules xrpl-go enables (govet,
errcheck, staticcheck, gocritic, gosec, revive, unparam, misspell). Our tool
should not duplicate them. Its niche is the *semantic* conventions a linter
cannot express (does this Validate check every field with the right helper, is
the sentinel error named and documented, is the test table-driven and named
like its siblings) and turning them into a number.

## 5. Rule catalogue extracted from xrpl-go

Explicit written rules are thin: `CONTRIBUTING.md` says "follow the project's
coding style", `.golangci.yml` enables ten linters plus gofmt with tests
excluded, and CI runs `make lint` and `make test-ci` on Go 1.24. Everything
else is convention embodied in the code. The catalogue below was extracted
from the codebase and is the seed for `rules/*.yaml`.

### 5.1 Baseline facts about the repo

- Go `1.24.3` in `go.mod`, but `.go-version` says 1.23.0 and CONTRIBUTING says 1.22: three declared versions.
- golangci-lint v2.2.2, linters: govet, errcheck, staticcheck, ineffassign, unused, gocritic, gosec, misspell, unparam, revive (default rule set), gofmt. `run.tests: false`, so **no linter touches the 248 test files**. Not enabled: funlen/gocognit, mnd, errorlint, testifylint, paralleltest, wrapcheck.
- Coverage thresholds in `.testcoverage.yml` are all 0.
- 681 Go files, ~38k non-test LOC, 1,268 non-test top-level funcs, 59 `Validate() (bool, error)` methods, 23 packages with an `errors.go`.
- testify in 159 test files; `require` about 1,030 calls vs `assert` about 156. Sentinel errors are compared by message 47 times (`require.EqualError`) and by identity only 4 times (`ErrorIs`).
- `t.Parallel()` is used zero times; `context.Context` appears in zero public signatures; generics are used zero times; `interface{}` (263) and `any` (~1,000) are mixed, sometimes in one function.

### 5.2 Rule catalogue

Columns: **Check** = STATIC (decided from the AST, no model call) or TS (a TypeSafe Score/Noul question); **Enforced today** = whether golangci-lint already catches it. Weights are a first proposal.

| ID | Category | Rule (as observed in xrpl-go) | Evidence | Check | Enforced today | Weight |
|---|---|---|---|---|---|---|
| LAYOUT-01 | layout | One transaction/ledger-entry type per snake_case file with a sibling `_test.go` | `xrpl/transaction/payment.go` + `payment_test.go`, 131 files in that dir | STATIC | no | 2 |
| LAYOUT-02 | layout | Tests are in-package (`package transaction`), never `_test` packages | every `_test.go` | STATIC | no | 1 |
| LAYOUT-03 | layout | Sentinel errors and error types live in the package's `errors.go` | 23 `errors.go` files | STATIC | no | 2 |
| LAYOUT-04 | layout | Cross-package interfaces live in an `interfaces/` subpackage; mocks and helpers in `testutil/` | `xrpl/websocket/interfaces/request.go`, `xrpl/rpc/testutil/client_mock.go` | STATIC | no | 2 |
| NAME-01 | naming | Every package has a `// Package x ...` comment | 92 files | STATIC | revive package-comments | 1 |
| NAME-02 | naming | Every exported symbol has a doc comment starting with its name, including each `ErrXxx` var | `xrpl/transaction/errors.go` | STATIC | revive exported | 2 |
| NAME-03 | naming | Short receiver derived from the type (`p *Payment`, `tx *BaseTx`), consistent across the type's methods | `payment.go`, `tx.go` | STATIC | revive receiver-naming | 1 |
| NAME-04 | naming | XRPL initialisms keep wire casing (`NFToken`, `AMM`, `XChain`) with a correctly spelled file-level `//revive:disable:var-naming` | ~25 files; malformed variants in `xrpl/common/constants.go:1` | STATIC | partly | 1 |
| NAME-05 | naming | Enum-like string types with `String()` and constants suffixed by kind: `PaymentTx TxType = "Payment"`, `AccountRootEntry EntryType` | `xrpl/transaction/tx_type.go`, `ledger-entry-types/ledger_object.go` | STATIC + TS | no | 2 |
| NAME-06 | naming | Flag constants unexported, prefixed `tf`/`asf`/`lsf`, typed `uint32`, doc-commented | `payment.go:11-18`, `account_set.go:13-49`; exception `types/raw_tx.go:6` | STATIC | no | 3 |
| NAME-07 | naming | Constructors `NewXxx`; bounds `MinXxx`/`MaxXxx`/`XxxSize`; defaults `DefaultXxx` in `xrpl/common/constants.go` | 24 `NewXxx`; `validations_xrpl_objects.go:17-25` | STATIC + TS | no | 1 |
| ERR-01 | errors | Sentinel errors are package-level `var ( ErrXxx = errors.New("lowercase message") )` grouped under topic comments | `xrpl/transaction/errors.go` | STATIC | revive error-naming, error-strings | 3 |
| ERR-02 | errors | No `errors.New` / `fmt.Errorf` literals inside function bodies; reuse a sentinel | violation `xrpl/transaction/tx.go:193` | STATIC | no | 3 |
| ERR-03 | errors | Parameterised errors are `ErrXxx` struct types with `Error()` under a `// Dynamic errors` header, value receivers | `xrpl/rpc/errors.go` (`ClientError` breaks both) | TS | no | 2 |
| ERR-04 | errors | `fmt.Errorf` wraps with `%w` for the sentinel and `%v` for the cause | `xrpl/testutil/flatten.go:14` | STATIC | no (errorlint off) | 2 |
| ERR-05 | errors | Errors are returned, never swallowed; deliberate ignores are `_ = x.Close()`; no `panic` outside unreachable code | one panic in `pkg/crypto/secp256k1.go` | STATIC | errcheck partly | 3 |
| ERR-06 | errors | No unchecked type assertions | violation `ledger-entry-types/ledger_object.go:42` | STATIC | no | 2 |
| TX-01 | transaction | Struct embeds `BaseTx` as first anonymous field; every field doc-commented with `(Optional)` / `API v1:` markers | `payment.go:25-84` | STATIC + TS | no | 3 |
| TX-02 | transaction | JSON tags are `json:",omitempty"` only (field name is the wire name); required fields have no tag; optional scalars are pointers | `payment.go:39,57` | STATIC | no | 2 |
| TX-03 | transaction | `TxType()` with anonymous pointer receiver returns the matching `XxxTx` constant | exception `payment.go:87` value receiver | STATIC | no | 2 |
| TX-04 | transaction | `Flatten()` starts from `BaseTx.Flatten()`, guards each field with a zero check, sets `TransactionType` via `XxxTx.String()` not a literal | 25 files use the literal, e.g. `payment.go:96` | STATIC + TS | no | 2 |
| TX-05 | transaction | One `SetXxxFlag()` per `tf` constant, body `x.Flags \|= tfXxx`, doc-commented | 49 setters, `payment.go:157-179` | STATIC | no | 2 |
| TX-06 | transaction | `Validate() (bool, error)` calls `BaseTx.Validate()` first, validates every field with the designated helper, returns `false, ErrXxx` on failure and `true, nil` at the end | `payment.go:181-237` | TS (rubric from the experiment) | no | 4 |
| TX-07 | transaction | Cross-field rules go in unexported `checkXxx(tx *T) (bool, error)` helpers at the bottom of the file | `payment.go:239` | STATIC + TS | no | 1 |
| VAL-01 | validation | Composite XRPL objects validated through `IsXxx(...) (bool, error)` helpers; addresses always through `addresscodec.IsValidAddress` | `validations_xrpl_objects.go` | TS | no | 3 |
| VAL-02 | validation | Value types expose `IsValid() bool`; flat-map optional fields go through `ValidateOptionalField` | `types/credential_type.go`, `validations_commons.go:8` | STATIC + TS | no | 2 |
| VAL-03 | validation | Flags are tested with `types.IsFlagEnabled(flags, tfXxx)`, never raw literals | `payment.go:249` | STATIC | no (mnd off) | 2 |
| QRY-01 | queries | `XxxRequest` embeds `common.BaseRequest`, explicit snake_case JSON tags with omitempty, implements `Method()`, `APIVersion()`, `Validate() error` | `queries/account/channels.go` | STATIC | no | 2 |
| CLI-01 | client | Config via `NewClientConfig()` + `WithXxx()` value-receiver builders, each doc comment ending in `// Default: ...` | `xrpl/websocket/config.go` | STATIC + TS | no | 1 |
| CLI-02 | client | Retry/timeouts use named constants from `xrpl/common`, `http.StatusXxx` not literals | violation `xrpl/rpc/client.go:70-88` | STATIC | no | 2 |
| LANG-01 | language | Use `any`, not `interface{}`, in new code | `tx.go:138` vs `tx.go:149` | STATIC | no | 1 |
| LANG-02 | language | `nolint` directives name a linter and carry a correct reason; no bare `// nolint` | `tx_type.go:6`, `binary-codec/types/st_object.go` | STATIC | no | 1 |
| TEST-01 | tests | Test funcs named `TestType_Method` | `payment_test.go:11` vs `:16` | STATIC | no | 2 |
| TEST-02 | tests | Table-driven: `tests := []struct{ name; ...; expected; expectedErr }`, `for _, tt := range tests`, `t.Run(tt.name, ...)` | 256 `tests :=`, 103 `tt :=`, 71 `testcases :=` | STATIC + TS | no | 2 |
| TEST-03 | tests | Subtest names prefixed `pass - ` / `fail - ` | 513 / 341 occurrences | STATIC | no | 1 |
| TEST-04 | tests | `require.*` over `assert.*`; sentinel errors asserted with `require.ErrorIs`, not `EqualError` | 47 `EqualError` vs 4 `ErrorIs` | STATIC | no | 2 |
| TEST-05 | tests | Serialisation round-trips use `xrpl/testutil` helpers (`SerializeAndDeserialize`, `CompareFlattenAndExpected`) | `xrpl/testutil/serializer.go` | TS | no | 1 |
| TEST-06 | tests | Every exported method of a transaction type has a test in the sibling file (TxType, Flatten, Validate, each SetXxxFlag) | `payment_test.go` | STATIC | no | 3 |

Roughly two thirds of the rows are STATIC. That is the point: the model is reserved for the rows marked TS, where the question is "does this do the right thing with the project's helpers", which no AST predicate can answer.

### 5.3 Known inconsistencies in xrpl-go (calibration targets)

These are real violations found today. They are useful in two ways: the tool must flag them (recall check), and they show which rules have exceptions the rubric must tolerate.

1. 25 `Flatten()` methods hardcode `"Payment"`-style literals for `TransactionType` (TX-04).
2. `Payment.TxType()` alone uses a value receiver (TX-03).
3. `BaseTx.Validate` returns an inline `errors.New` next to sentinel uses, `tx.go:193` (ERR-02).
4. `rpc.ClientError` is not `ErrXxx`-named and uses a pointer receiver unlike its siblings (ERR-03).
5. About 20 sentinel errors are duplicated between `xrpl/rpc/errors.go` and `xrpl/websocket/errors.go`, some with diverging messages.
6. `TfInnerBatchTxn` is the only exported flag constant (NAME-06).
7. Malformed revive directives (`//revive:disable var-naming`, `// revive:disable:var-naming`) that revive likely ignores (NAME-04).
8. Copy-pasted `//nolint:gosec // G115: Potential hardcoded credentials` where the text describes G101; bare `// nolint` (LANG-02).
9. Literal `503`, `maxRetries := 3`, `1 * time.Second` in `rpc/client.go` (CLI-02).
10. `DefaultFeeCushion` / `DefaultMaxFeeXRP` redefined in `websocket` although `xrpl/common` has them (NAME-07).
11. Unchecked `.(string)` assertion in `ledger_object.go:42` (ERR-06).
12. Test vocabulary drift: `tests`/`tt`/`testcases`, `expected`/`want`/`exp`, `TestPayment_Validate` vs `TestPaymentFlags` in one file (TEST-01, TEST-02).
13. Sentinel errors compared by message 47 times (TEST-04).
14. No `context.Context` in any public client method; no `t.Parallel()` anywhere; no `//go:generate` for the checked-in mockgen scripts. These are design gaps rather than convention breaks and should be opt-in rules.
15. Three validation vocabularies coexist: `IsXxx (bool, error)`, `Validate() error`, `IsValid() bool`, `validateXxx error` (VAL-01/02, QRY-01). The rule must be scoped per package family.

## 6. Proposed architecture

```
gorev review [--diff main..HEAD | ./...] [--format text|json|jsonl|sarif]
       [--fail-under 80] [--min-confidence 0.6] [--rules rules/]
```

Pipeline, one stage per package:

1. **Discover** (`internal/scan`): `go/packages` + `go/ast` walk. Unit of
   review = top-level func / method, plus file-level and package-level units
   (imports, sentinel error block, test file layout). Each unit carries: source
   text, doc comment, receiver/type, signature, file:line span, package path,
   and cheap AST facts (calls `panic`, has `errors.New` literal, returns
   `(bool, error)`, name matches `^Test`, ...). Diff mode intersects units with
   changed line ranges (like open-code-review's file selection).
2. **Match** (`internal/rules`): rules are YAML files. Each rule has `id`,
   `category`, `weight`, `applies_to` (AST predicates and path globs, evaluated
   locally, e.g. `receiver_in: [transaction.*]` and `method: Validate`), a
   `question` (Score levels or Noul, written to TypeSafe's rubric rules),
   `fix_hint` template, and `min_confidence`. Rules that do not apply to a unit
   cost nothing.
3. **Prefilter** (`internal/static`): anything decidable from the AST is decided
   without the model (doc comment missing, receiver name inconsistent, panic in
   non-test code, raw numeric flag literal). These produce score 0 or 1 directly
   with confidence 1.0. TypeSafe is only asked the semantic questions.
4. **Ask** (`internal/typesafe`): small Go client for `/v1/systemone` with
   retry/backoff on 429/529, request batching (several units per request under
   `state.functions.<id>`, questions keyed `<unit>_<rule>`), concurrency
   limiter, on-disk cache keyed by sha256(state+questions+model) so unchanged
   functions are never re-scored.
5. **Score** (`internal/score`): normalise each answer to 0..1
   (`score / (levels-1)`, `1 - noul` for "bad thing present" nouls). Unit score
   = weighted mean over applicable rules. Answers below `min_confidence` are
   excluded from the mean and listed as "needs human look". File / package /
   repo scores roll up by LOC-weighted mean. Health = round(100 * repo score).
   Weights and thresholds live in one file (`rules/weights.yaml`) as TypeSafe
   recommends.
6. **Report** (`internal/report`): terminal (grouped by category, worst units
   first, fix hint on every finding), JSON/JSONL for agents, SARIF for GitHub
   code scanning, and a markdown PR comment. CI mode compares against a
   baseline JSON from the base branch and fails only on regressions
   (react-doctor's "new issues only").
7. **Install** (`gorev install`): writes a SKILL.md / CLAUDE.md fragment listing
   the rules and fix hints so agents write compliant code up front.

Design decisions taken from the experiment:
- Never ask Choice for the verdict; verdicts are thresholds on composite scores.
- One Score per dimension, 3-4 situation-described levels each.
- State is an object `{conventions, unit: {kind, signature, doc, source,
  siblings?}}`; `conventions` is the rule's own prose, not the whole handbook,
  to keep tokens down and the rubric focused.
- Pin `jev-1.13.0` in config once thresholds are calibrated; log the returned
  `model` field.

## 7. Scoring model

Per rule r on unit u: `s(u,r) ∈ [0,1]`, `c(u,r) ∈ [0,1]` (confidence; 1.0 for
static checks; for Noul use `|2·noul − 1|`).

```
unit_score(u)   = Σ_r w_r · s(u,r) / Σ_r w_r          over r with c ≥ min_conf
file_score(f)   = Σ_u loc(u) · unit_score(u) / Σ_u loc(u)
repo_health     = 100 · Σ_f loc(f) · file_score(f) / Σ_f loc(f)
finding(u,r)    = s(u,r) < fail_level_r and c(u,r) ≥ min_conf
uncertain(u,r)  = c(u,r) < min_conf   → listed, never fails CI
```

Severity per rule is declared in YAML (`blocker`, `major`, `minor`), and
`--fail-on blocker` lets CI fail on a single blocker regardless of the health
number. Calibration: keep a `testdata/` corpus of known-good and deliberately
degraded xrpl-go functions (the two from the experiment are the first pair),
run it in `go test`, and assert score ordering and minimum separation, so rubric
edits cannot silently regress.

## 8. Risks and open questions

- **Rubric drift across model versions.** Aliases move; pin the version and
  keep the calibration corpus in CI.
- **Long functions.** Some xrpl-go functions (binary-codec serialisation) are
  hundreds of lines; keep one unit per request there, batch only small ones.
- **Cross-file context.** "Uses the designated helper" needs the helper list;
  put the relevant package's exported helper signatures into `state.context`
  rather than the model guessing.
- **Test-file rules** are where golangci-lint is silent (`tests: false`), so
  they are the highest-value semantic rules and also the least written down.
- **No Go SDK**; we own the client, including retries and the `retry-after`
  header.
- **Precision first.** Default `min_confidence` high (0.7) and surface
  uncertain answers as a separate list, never as failures.

## 10. Further improvements (with two more experiments)

### 10.1 Error-path test coverage, semantically

Question per sentinel a function can return: "does the test table contain a
case expecting `ErrX`, or one that exercises the condition that returns it?"

- `Payment.Validate` returns 4 sentinels; all 4 answered 0.99; grep agrees.
- `Clawback.Validate` returns 3 sentinels; **grep finds none of them in the
  test file**, but TypeSafe answered 0.96 / 0.98 / 0.98. TypeSafe is right:
  the test has "fail - missing Amount", "fail - invalid Amount", "fail -
  Account same as issuer" cases. They just assert `ok == false` without
  naming the error. A grep-based rule would have produced three false
  positives here, and the same grep flags 40+ files across the package.
- Side finding: those tests should assert the sentinel (`require.ErrorIs`),
  which is rule TEST-04. The two rules compose: coverage is fine, precision
  of the assertion is not.

### 10.2 Ideas ranked by expected value

1. **Generate questions from the AST, not from the rule author.** Every
   decomposable rule becomes "for each X in an AST list, one Noul". Lists we
   get for free: struct fields, sentinels returned, `tf` constants (does a
   `SetXxxFlag` exist), exported methods (does a test exist), `errors.go`
   entries (is it used anywhere). The single biggest lever: vague rubrics
   become per-item facts with a line and a fix hint.
2. **Relative scoring against an anchor.** Ask a Choice: "which of A (the
   unit) and B (the package's best-in-class sibling) follows the convention
   better, or are they equivalent?" Relative judgements are easier and drift
   less across model versions. Anchors live in `testdata/anchors/`.
3. **Convention mining.** Run candidate-convention Nouls across every unit in
   a package. 80%+ yes means convention and the rest are violations; 50/50
   means an inconsistency to raise as a decision, not a finding. Builds the
   catalogue automatically; the 25-vs-32 `TransactionType` split would have
   surfaced this way.
4. **Self-consistency ensembles.** Ask each Noul twice with paraphrased
   wording in the same request. Agreement raises confidence; disagreement
   routes to the human list. Documented in TypeSafe's cookbook.
5. **Mutation-based calibration.** Mechanically mutate known-good units (drop
   a field check, inline an error, remove a doc comment) and assert in
   `go test` that the affected rule's score drops by a margin. Measures
   rubric sensitivity per rule and catches wording that stopped
   discriminating after a model bump.
6. **Cascade to a reasoning model only on low confidence.** Static, then
   TypeSafe, then only sub-floor units go to Claude for an explanation and a
   proposed patch. Cost stays near zero.
7. **Cross-package duplicate detection.** For each websocket sentinel, a
   Choice over the rpc sentinel names plus "none". The re-ranking cookbook
   applied to errors; would have found the ~20 duplicates.
8. **Fix loop with an agent.** Findings as JSONL with fix hints, an agent
   applies them, re-score only changed slices from cache. shadcn/lint's
   benchmark shows this converges in one round when messages name the fix.
9. **Doc-comment accuracy against xrpl.org.** Put the reference text for a
   field into the state; ask whether the Go comment agrees. Catches stale
   copies when the protocol changes.
10. **Use the distribution, not just the score.** A bimodal Score has the
    same mean as a solid middle level. Flag bimodal answers as "ambiguous"
    instead of averaging them into health.
11. **Trend and ownership views.** Health per package over commits, and the
    ten units that moved most in a PR.
12. **Pin and log.** Put the versioned `model` from each response into the
    cache key so a model bump invalidates only what it should.

## 11. Trying the ideas at package scale

Built `cmd/slicer` (Go, `go/ast`): it walks a package and emits one slice
per transaction struct with its fields, the source of `Validate`, `Flatten`
and `TxType`, its test functions, the sentinels `Validate` returns, and the
package's `errors.go` sentinels. `experiments/run_ideas.py` then runs six
experiments over all 56 transaction types. Whole run: 223 requests, 282,886
input tokens (about 1.2 cents), 17 seconds with 8 workers. Raw results are in
`experiments/results-ideas-transaction-package.json`.

### E1. AST-generated per-field validation Nouls, all 56 types

208 struct fields, one Noul each: "does `Validate` validate field X?".
31 fields came back below 0.3. Every one I spot-checked is genuinely absent
from its `Validate` body:

| Type | Fields never validated |
|---|---|
| CheckCreate | DestinationTag, Expiration, InvoiceID |
| EscrowFinish | Condition, Fulfillment |
| NFTokenCreateOffer | NFTokenID, Amount, Expiration |
| OracleSet | LastUpdatedTime, URI, AssetClass |
| PaymentChannelCreate | SettleDelay, CancelAfter, DestinationTag |
| PaymentChannelClaim | Balance, Amount |
| TrustSet | QualityIn, QualityOut |
| Payment, AccountDelete, EscrowCreate, XChainClaim | DestinationTag |
| MPTokenAuthorize | MPTokenIssuanceID |
| MPTokenIssuanceCreate | AssetScale |
| NFTokenMint | NFTokenTaxon |
| NFTokenModify | NFTokenID |
| AMMClawback / AMMDeposit / CredentialCreate / XChainCommit | Asset2 / TradingFee / Expiration / OtherChainDestination |

Only 3 of 208 answers disagreed with a naive "field name appears in the
body" check, and those were the indirect cases (validated through a
flattened map by string key). `DestinationTag` as `*uint32` is a false
positive by design; the rule should skip fields whose type admits every
value, which the AST knows.

### E2. Convention mining: how is `TransactionType` set in `Flatten`?

Asked across all 56 `Flatten` bodies in batches of 7. The model said
"constant" for 27, my regex ground truth said 31, and the 4 disagreements
turned out to be **my ground truth being too coarse**. The package actually
has six variants:

| Variant | Files |
|---|---|
| `flattened["TransactionType"] = "Literal"` | 25 |
| `x.TxType().String()` (three map variable names) | 24 |
| `<Const>Tx.String()` | 4 |
| `tx.TransactionType.String()` | 1 |
| `x.TxType()` without `.String()` | 1 |
| no assignment; relies on `BaseTx.Flatten()` | 14 |

Mining found the inconsistency I would have mis-catalogued. The right rule
is "do not set it at all; `BaseTx.Flatten` already does", which makes 42
files redundant and 1 wrong-typed.

### E3. Error-path test coverage, semantic vs grep

126 sentinels returned by `Validate` methods that have a `Test*_Validate`.
Grep for the sentinel name in the test file misses 46 of them; the model says
all 46 are covered by cases that exercise the condition. Verified on a
sample. The model reports **3 real gaps**, all confirmed by reading the tests:

- `MPTokenAuthorize`: no case for `ErrInvalidAccount`.
- `MPTokenIssuanceCreate`: no case for `ErrInvalidTransferFee` or
  `ErrInvalidMPTokenMetadata` (every case uses fee 314 and hex metadata).

### E4. Paraphrase self-consistency

E1 re-asked with two other phrasings: 189 of 208 answers unanimous, 19
split. The splits cluster on `AccountSet` and `DIDSet`, exactly the types
that validate through `ValidateOptionalField(flatten, "Name", ...)` by
string key rather than touching the field directly. The phrasing that
carried explicit `criteria` agreed with E1; the bare phrasing did not. So:
always give Nouls `criteria`, and treat a split as "indirect check, show a
human" rather than as a finding.

### E5. Anchor-relative Choice against `Payment.Validate`

All 55 others lost to Payment. Swapping A and B for six of them flipped the
answer with it (so no position bias), and Payment-vs-Payment answered
"equivalent" at 0.96. The comparison is consistent, but a fixed anchor gives
no gradient when one function dominates. Use it for **before/after of the
same function in a PR** ("is the new Validate at least as complete as the
old?"), not for ranking siblings.

### E6. Duplicate sentinels, websocket against rpc

26 websocket sentinels, a Choice over the 23 rpc names plus "none". All 22
that share a name were matched to it (confidence 0.75 to 0.99) and the 4
websocket-only errors (`ErrNotConnected`, `ErrRequestTimedOut`,
`ErrIncorrectID`, `ErrNotConnectedToServer`) were correctly answered "none"
at 0.93 or higher. That is a ready-made "extract to `xrpl/common`" list.

### What this changes in the plan

1. The slicer is the real discovery stage; keep it and grow it.
2. Rules TX-06, TEST-06 and ERR-01 become AST-generated Noul sets; the
   Score rubric versions are retired.
3. Convention mining becomes `gorev mine`, run before writing any rule.
4. Every Noul carries `criteria`; a paraphrase pair is the default and a
   split routes to the human list.
5. Relative Choice is used only for same-unit before/after in diff mode.
6. Type-aware skipping: fields whose type admits every value are not asked.

## 9. Suggested next steps

1. Scaffold the Go module (`cmd/gorev`, `internal/{scan,rules,static,typesafe,score,report}`).
2. Encode the catalogue in section 5 as `rules/*.yaml` (about 25 rules) with
   the experiment rubrics as the first three.
3. Build the calibration corpus from real xrpl-go units plus mutated copies;
   make `go test ./...` assert separation.
4. Run a full scan of xrpl-go, inspect the bottom 50 units by hand, tune
   weights and rubric wording.
5. Add `--diff` mode and the GitHub Action, then the `install` skill.

# neo — proof-carrying answers over a data lake, grounded in an ontology

Ask a business question in plain English. An LLM writes a **model** of it in **ontology vocabulary** (entities,
fields, relations, never physical tables). Code then does everything else:

1. **validates** the model against the ontology, the asker's access rights, units, time semantics, approved rules
   and query cost
2. **compiles** it to **exact SQL** that runs inside the lake: integer numerator/denominator arithmetic, no
   floating point
3. **proves** the answer with Z3 over the lake's exact aggregates, plus verification queries over every row
4. **recomputes** it with an independent implementation; if the two disagree, the status becomes `mismatch` and
   the answer goes to human review
5. **certifies** it with a certificate rendered by code, and the LLM writes the answer only from that certificate

The LLM never sees raw data and never computes anything.

```
neo ask "How many failed-bank deals happened in the 1980s?"
```

**New here?** [docs/life-of-a-question.html](docs/life-of-a-question.html) walks through every step of a question, in plain words, with a real run as the example (download it and open it in a browser).

## Quick start

```bash
cd neo
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e .
.venv/bin/neo init                    # lake: the deals CSV (../jp_morgan.csv) + FDIC history; publishes the ontology
.venv/bin/neo eval golden/core.yaml   # 7 golden questions, scripted (no API key needed)
export ANTHROPIC_API_KEY=...          # or set llm.default in config/neo.yaml (Ollama, vLLM, OpenAI-compatible, ...)
.venv/bin/neo ask "How likely is JPMorgan to stay profitable if costs rise by \$3B a quarter?"
.venv/bin/neo serve                   # REST API on :8080 (X-User header; put your OIDC gateway in front)
                                      # and the web UI at http://localhost:8080/ (every step of each question)
.venv/bin/python -m pytest            # 107 tests on the real data, no network, ~15 s
```

The data is real only: the deals CSV and FDIC quarterly history. `neo init --fdic-cache DIR` reads saved FDIC API
responses instead of the network (the tests use the ones in `tests/data/fdic`).

## Architecture

```
 channels ─► API (FastAPI, X-User from your gateway) ─► job queue ─► workers
                                                                       │
 ┌──────────────────────────── orchestrator (durable, checkpointed per step) ────────────────────────────┐
 │ pin snapshot + ontology version + rule versions                                                          │
 │ retrieve ─► formalize (LLM) ─► validate ─► compile ─► execute ─► prove ─► review (LLM) ─► certify ─► answer (LLM) ─► route │
 └──────────────────────────────────────────────────────────────────────────────────────────────────────┘
     │               │             │          │          │         │                    │
 ontology service  LLM gateway  validator  compiler  lake engine  proof service   run store + audit + review queue
 (your store,      (routing,    (joins,    (exact    (pinned      (Z3, verification   (replay, metrics,
  linker, lint)     budgets,     fan-out,   integer   snapshot,    queries, refinement, feedback, curation)
                    egress)      units,     SQL)      row policy)  independent check)
                                 access,
                                 rules)
```

| Component | File | What it does |
|---|---|---|
| Catalog and snapshots | [neo/lake/catalog.py](neo/lake/catalog.py) | Versioned tables. A `Snapshot` pins one version of every table for a whole question. `LocalLakehouse` keeps immutable Parquet versions, the same idea as Iceberg/Delta. |
| Query engines | [neo/lake/engines.py](neo/lake/engines.py) | DuckDB locally; `SQLAlchemyEngine` for Trino / Snowflake / Databricks with time travel (`FOR VERSION AS OF`). Dialects for exact integer types and filtered aggregates. |
| Connectors | [neo/lake/connectors.py](neo/lake/connectors.py) | External data comes in as new table versions (the deals CSV, FDIC history). Nothing is fetched during a question. |
| Profiles | [neo/lake/profiles.py](neo/lake/profiles.py) | Exact enum values and ranges per table version, cached. Withheld for row-restricted entities. |
| Ontology model and store | [neo/ontology/model.py](neo/ontology/model.py), [store.py](neo/ontology/store.py) | Terms, entities, fields (physical column or expression, unit, time semantics, classification), join relations with cardinality, constraints, metrics, scenario sets, evidence. Immutable published versions. |
| Linker and retrieval | [neo/ontology/linker.py](neo/ontology/linker.py), [service.py](neo/ontology/service.py) | Links question words to ontology objects (aliases + TF-IDF). Retrieves only the relevant subgraph, join paths and profiles; asks for clarification if nothing links. |
| Lint (publish gate) | [neo/ontology/lint.py](neo/ontology/lint.py) | Every column, join key, unit, expression and reference is checked against the lake before a version can be published. |
| Rule registry | [neo/rules.py](neo/rules.py), [rules/](rules/) | Versioned legal and business rules. Only `approved` rules in force can be used. |
| Identity and policies | [neo/governance/identity.py](neo/governance/identity.py), [config/users.yaml](config/users.yaml) | Roles, clearance (public < internal < confidential < restricted), row policies with user attributes. |
| Audit | [neo/governance/audit.py](neo/governance/audit.py) | Append-only, hash-chained log. `neo audit` detects any edit. |
| Review routing | [neo/governance/review.py](neo/governance/review.py) | Sends answers to human review when they are not a clean proof, were checked only approximately, are high impact, or failed a backtest. |
| Validator | [neo/semantic/validator.py](neo/semantic/validator.py), [units.py](neo/semantic/units.py) | Names, joins, fan-out (double counting), unit algebra, year-to-date and stock/flow, access, rule approval, grounding of `block` constraints, query cost. |
| Exact compiler | [neo/compiler/sql.py](neo/compiler/sql.py) | Model → SQL with numerator/denominator integer arithmetic. Comparisons by sign, overflow raises an error, inexact cases are flagged. |
| Independent checks | [neo/execution/interpreter.py](neo/execution/interpreter.py), [compiler/floatsql.py](neo/compiler/floatsql.py) | An exact Python interpreter over the raw columns, or a separate floating-point compilation for very large universes. Neither shares code with the compiler. |
| Proof service | [neo/proof/prover.py](neo/proof/prover.py), [z3expr.py](neo/proof/z3expr.py) | Per-form strategies (below), backtests, sensitivity sweeps. |
| Certificate | [neo/proof/certificate.py](neo/proof/certificate.py) | Full version (with SQL and evidence) for people; LLM-safe version without row data. |
| LLM gateway | [neo/llm/](neo/llm/) | Task routing, per-user daily token budgets, egress guard, call logging, retries. Providers: Anthropic, any OpenAI-compatible server, scripted, or your own class. |
| Orchestrator | [neo/orchestrator.py](neo/orchestrator.py) | The workflow, checkpointed per step. `resume` continues after a crash; `replay` re-proves a stored model on its pinned snapshot without an LLM. |
| Run store and tracing | [neo/runs.py](neo/runs.py), [neo/observability.py](neo/observability.py) | Runs, step checkpoints, LLM calls, review queue, feedback, curation queue, job queue, metrics. One trace per question. |
| Evaluation | [neo/evaluation.py](neo/evaluation.py), [golden/](golden/) | Golden question sets (scripted or live). `ontology publish --eval` makes a version current only if all cases pass. |
| API and CLI | [neo/api.py](neo/api.py), [neo/cli.py](neo/cli.py) | REST endpoints and the `neo` command. |

## How proofs work at lake scale

Z3 never receives raw rows. Each form has its own strategy:

| Form | The lake computes | Z3 / verification proves |
|---|---|---|
| value, table | exact aggregates (integer SQL) | Z3 derives each value from them and proves `value ≠ v` is unsat |
| extremum | ranked candidates | a query per rank: `COUNT(*)` of rows strictly better equals the number of better candidates (0 for the top) |
| set | members and a count | the count, re-derived independently |
| check (rows) | — | a counterexample query; none means it holds on this snapshot |
| feasibility, optimisation, check (global) | parameters and aggregates | Z3 over the constraints. Constraints over every row (`all_rows(...)`, `count(...) <= k` with variables) are solved directly up to 5,000 rows, and by **counterexample-guided refinement** above that. |

How refinement works:
1. Z3 solves on a working set of rows.
2. A lake query checks the candidate against every row.
3. Violating rows are added to the working set, and the loop repeats.

Why the result holds for all rows:
- Infeasibility, or "nothing beyond the optimum", proved on a subset holds for all rows, because a subset only relaxes the constraints.
- Feasibility and reachability of the optimum are checked on all rows by the lake query.

On the FDIC history, the highest return-on-assets floor with at most 10 quarters below it converges in 4 rounds
when forced past the direct limit (`tests/test_prover.py`).

Every result is also recomputed by the independent path. A deliberately injected compiler bug is caught by this
check (`tests/test_prover.py::test_a_compiler_bug_is_caught`).

### Lean re-checks what Z3 decided

Z3 is a solver, and its "unsat" is trusted. When Lean 4 is installed, every run also writes what Z3 decided as
theorems about the exact formulas Z3 was given, and Lean's kernel checks them (`neo/proof/lean.py`):

| What Z3 decided | Lean theorem | How Lean proves it |
|---|---|---|
| value = the lake's aggregates | `values` | `decide +kernel`: the kernel evaluates the exact fractions |
| the witness / optimum satisfies every working row | `witness_*` | `decide +kernel` |
| nothing beats the optimum | `bound` | `grind`, from only the binding rows in Z3's unsat core |
| the constraints clash | `clash` | `grind`, from the core rows |
| a global claim follows | `claim` | `grind`, from the core rows |

A rejected `decide` theorem means Lean disagrees with Z3, so the run goes to human review. If `grind` cannot
prove a non-linear formula, the run is reported as not proved by Lean, not as wrong.

Going from the rows Lean saw to every row of the table uses general theorems. These are proved once in
`lean/Neo` and cover:
- refinement and optima: `Neo.Refinement`
- the extremum verification count: `Neo.Extremum`
- the zero-counterexample check: `Neo.Counterexample`
- the routes that open on an infeasible answer: `Neo.Clash`
- the compiler's exact fractions: `Neo.Exact`
- unit conversion: `Neo.Units`

To use it:
- `neo lean build` builds the library, and checks that every theorem uses only Lean's three standard axioms, so no step is left unproved.
- `neo lean status` shows what is installed.
- `lean.enabled` in `config/neo.yaml` is `auto` (use Lean when it is installed), `off`, or `required`.

Only Lean's core library is used, not Mathlib. The UI shows the theorems and the generated Lean file in step 9.
The Lean file contains row values, so it is never sent to the LLM. See [lean/README.md](lean/README.md).

## Connecting your own systems (production adapters)

| You have | Do this |
|---|---|
| **Your data lake** (Iceberg/Delta behind Trino, Snowflake, Databricks, Spark SQL) | Set `lake.kind: sqlalchemy` with the URL, dialect and time-travel template in [config/neo.yaml](config/neo.yaml). Implement `Catalog` ([neo/lake/catalog.py](neo/lake/catalog.py): `tables`, `snapshot`, `schema`, `row_count`) over your catalog (Iceberg REST, Glue, Unity) and pass it to `Neo(catalog=...)`. Enforce row and column security in the lake under the user's identity as well. |
| **Your ontology store** | Implement `OntologyStore` ([neo/ontology/store.py](neo/ontology/store.py): `current_version`, `load_document`, `publish`, `list_versions`) to return documents in the format of [neo/ontology/model.py](neo/ontology/model.py). Most conceptual ontologies lack what this needs: physical column, unit, time semantics, classification, join keys and cardinality. [scripts/build_deals_ontology.py](scripts/build_deals_ontology.py) shows how an existing ontology (`ontology_v0`) was mapped onto a table. |
| **Identity** | Put the API behind your OIDC/SAML gateway, pass the authenticated user as `X-User`, and map identity-provider groups to the roles in [config/users.yaml](config/users.yaml). |
| **Run store at scale** | `RunStore` uses SQLite and local files. For several API or worker replicas, reimplement it on Postgres and object storage (same methods). |
| **Workflow engine** | `Neo.work()` is a simple claim-and-run loop. Each step is already checkpointed, so it maps directly onto Temporal or Step Functions activities. |
| **Observability** | Spans in `trace.json` have the OpenTelemetry shape. Override `Tracer.emit` to export them. `neo stats` and `GET /stats` give run, latency, token and review metrics. |

## Ontology authoring

Edit [ontology/source/*.json](ontology/source/), then run `neo ontology lint` and
`neo ontology publish --eval golden/core.yaml`.

The fields that matter most:
- **field:** `column` *or* `expression`, plus `unit` (`USD`, `USD_thousands`, `ratio`, `probability`, `bp`, `count`, `text`, `date`, ...), `time_semantics` (`stock` / `flow` / `ytd` with `per_period`), and `classification`
- **join relation:** `on` (the key pairs) and `cardinality`. This is what lets the validator stop double counting.
- **constraint:** `severity: block`, plus `triggers` or a `target`. Methodology and legal rules are triggered by wording; data rules are also triggered by the fields used.
- **scenario_set:** a curated, approved window of history for likelihood and break-even questions

Lint warnings show where the ontology needs work. `neo curation` lists question words that matched nothing,
frequent validator errors, clarifications and negative feedback: that list is the backlog for ontology curators.

## What is local emulation, and what is production-ready

- **Production-ready logic:**
  - validator, exact compiler, interpreter, prover and refinement
  - certificates, governance checks and audit chain
  - evaluation gate and the orchestrator's checkpointing
  These are tested by 107 tests on the real data, every answer against an independent computation.
- **Local stand-ins with production interfaces:**
  - DuckDB over Parquet, standing in for the lake
  - a JSON directory, for the ontology store
  - SQLite, for the run store and queue
  - `X-User`, standing in for real authentication
- **Not exercised here:** the SQLAlchemy engine path (Trino / Snowflake dialects are written but untested), and a live LLM run.
- **Limits:**
  - Aggregates over a per-row varying denominator are computed in floating point and flagged.
  - Refinement supports `all_rows` and `count(...) <= k` shapes, on universes without one-to-many joins.
  - A proof shows the answer follows from the model on the pinned snapshot. Whether the model means what the
    question meant is checked by the validator, review, backtests and golden sets, but not proved. Read the
    certificate's interpretation line.

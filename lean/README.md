# lean/: the theorem library behind neo's proofs

neo uses two provers:
- **Z3** finds answers: it optimises, searches and finds clashes.
- **Lean 4** checks them.

This folder holds the general theorems: facts about neo's *methods*, proved once and true for every run.
`neo/proof/lean.py` generates the per-run theorems about each question's numbers.

| File | Proves | Used by |
|---|---|---|
| `Neo/Exact.lean` | a/b − c/d = (ad − cb)/(bd); comparing sign(num)·sign(den) with 0 compares the fractions; Σ(xᵢ/d) = (Σxᵢ)/d; DECIMAL scaling is exact; "ans = t ∧ ans ≠ v is unsat" means t = v | the compiler, `form: value` |
| `Neo/Units.lean` | scaling by a positive factor keeps comparisons and commutes with sums | unit conversion in the validator |
| `Neo/Refinement.lean` | results on a subset of rows: unsat stays unsat; a bounded and attained optimum is the optimum over every row; a bound from the core rows holds on the working rows | refinement, optimisation, feasibility, global check |
| `Neo/Extremum.lean` | equal "rows strictly better" counts mean every better row is a candidate | `form: extremum` |
| `Neo/Counterexample.lean` | a zero `NOT COALESCE(claim, TRUE)` count means no row is a counterexample (SQL's three-valued logic) | `form: check` over rows |
| `Neo/Clash.lean` | a constraint whose removal makes the rest satisfiable is in every clashing set | infeasible probes ("routes that open") |

## Setup

1. Install Lean with elan, Lean's version manager. Run `curl https://elan.lean-lang.org/elan-init.sh -sSf | sh`, or use `brew install elan-init`.
2. In this folder, run `lake build`, or from the project root run `neo lean build`. Either way, it uses the version pinned in `lean-toolchain`.
3. `neo lean build` also prints every theorem's axioms. Only `propext`, `Classical.choice` and `Quot.sound` (Lean's standard axioms) may appear. Two other axioms would mean something was not really proved:
   - `sorryAx`: a step was skipped
   - `Lean.ofReduceBool`: the proof trusts compiled code

The library uses only Lean's core library, not Mathlib, so there is nothing large to download.

With `lean.enabled: auto` in `config/neo.yaml`, every run is checked once Lean is installed. Each run's generated
file starts with `import Neo` when this library is built, and lists the library theorems it relies on.

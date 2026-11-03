import Neo.Exact
import Neo.Units
import Neo.Refinement
import Neo.Extremum
import Neo.Counterexample
import Neo.Clash

/-!
# neo: the methods behind every proof, checked by Lean

Each file proves, once and for every run, a step that `neo/proof/prover.py` and `neo/compiler/sql.py` rely on.
No data is involved; the per-run checks (`neo/proof/lean.py`) are separate files generated for each question.

* `Neo.Exact`          exact fractions in the compiler, and the value proof of `form: value`
* `Neo.Units`          unit conversion keeps sums and comparisons
* `Neo.Refinement`     results on a subset of rows hold for every row (refinement, optimum, infeasible)
* `Neo.Extremum`       the verification count proves a top / bottom ranking
* `Neo.Counterexample` zero counterexamples means a per-row claim holds
* `Neo.Clash`          a constraint whose removal opens a route is in every clash

Build: `lake build` in this folder (or `neo lean build`). Uses only Lean's core library, no Mathlib.
-/

/-!
# Solving on a subset of rows (counterexample-guided refinement)

`neo/proof/prover.py` (`_Logic`) hands Z3 a working set `W` of rows instead of every row of the table `A`.
Each row contributes a constraint `C r x` on the unknowns `x`; `G x` collects the constraints that do not
depend on a row (variable domains, constraints over parameters only).

These theorems are why a result found on `W` holds for all of `A`:
* infeasible / a global claim proved on `W`  →  also on `A` (`unsat_of_subset`)
* an optimum bounded on `W` and attained by a candidate the lake checked on every row of `A`
  →  the optimum on `A` (`optimal_of_subset`, `minimal_of_subset`)
* a witness the lake checked on every row of `A` satisfies `A` (by definition of `Sat`)
-/
namespace Neo.Refinement

variable {Row X : Type}

/-- `x` satisfies the row constraints of every row in `rows` and the row-free constraints. -/
def Sat (C : Row → X → Prop) (G : X → Prop) (rows : List Row) (x : X) : Prop :=
  G x ∧ ∀ r ∈ rows, C r x

/-- Fewer rows can only relax the constraints. -/
theorem sat_mono {C : Row → X → Prop} {G : X → Prop} {W A : List Row} (hW : ∀ r ∈ W, r ∈ A) {x : X}
    (h : Sat C G A x) : Sat C G W x :=
  ⟨h.1, fun r hr => h.2 r (hW r hr)⟩

/-- No assignment satisfies the working rows, so none satisfies all rows.
Used for `infeasible` and for a global `check` (hypotheses ∧ ¬claim is unsat). -/
theorem unsat_of_subset {C : Row → X → Prop} {G : X → Prop} {W A : List Row} (hW : ∀ r ∈ W, r ∈ A)
    (h : ∀ x, ¬ Sat C G W x) : ∀ x, ¬ Sat C G A x :=
  fun x hA => h x (sat_mono hW hA)

/-- A maximum proved on the working rows, attained by a candidate that satisfies every row,
is the maximum over every row. -/
theorem optimal_of_subset {C : Row → X → Prop} {G : X → Prop} {W A : List Row} (f : X → Rat) (best : Rat)
    (hW : ∀ r ∈ W, r ∈ A) (hbound : ∀ x, Sat C G W x → f x ≤ best)
    (xs : X) (hxs : Sat C G A xs) (hf : f xs = best) :
    Sat C G A xs ∧ ∀ x, Sat C G A x → f x ≤ f xs :=
  ⟨hxs, fun x hx => hf ▸ hbound x (sat_mono hW hx)⟩

/-- The same for a minimum. -/
theorem minimal_of_subset {C : Row → X → Prop} {G : X → Prop} {W A : List Row} (f : X → Rat) (best : Rat)
    (hW : ∀ r ∈ W, r ∈ A) (hbound : ∀ x, Sat C G W x → best ≤ f x)
    (xs : X) (hxs : Sat C G A xs) (hf : f xs = best) :
    Sat C G A xs ∧ ∀ x, Sat C G A x → f xs ≤ f x :=
  ⟨hxs, fun x hx => hf ▸ hbound x (sat_mono hW hx)⟩

/-- Z3's bound only needs the rows in its unsat core: a bound proved from a core `K ⊆ W` holds on `W`.
The per-run Lean check proves the bound from the core rows only. -/
theorem bound_of_core {C : Row → X → Prop} {G : X → Prop} {K W : List Row} (hK : ∀ r ∈ K, r ∈ W)
    {P : X → Prop} (h : ∀ x, Sat C G K x → P x) : ∀ x, Sat C G W x → P x :=
  fun x hx => h x (sat_mono hK hx)

end Neo.Refinement

/-!
# Infeasible answers: the clash and the routes that open

When constraints `S` cannot all hold, `neo/proof/prover.py` reports Z3's unsat core and then drops each
constraint in turn ("probes"). If dropping `c` makes the rest satisfiable, `c` is reported as a route that opens.
This theorem is what that report means: `c` belongs to every set of these constraints that clashes,
so no explanation of the clash can leave it out.
-/
namespace Neo.Clash

variable {Name X : Type}

theorem in_every_clash (holds : Name → X → Prop) (S : List Name) (c : Name)
    (hdrop : ∃ x, ∀ d ∈ S, d ≠ c → holds d x) :
    ∀ T : List Name, (∀ t ∈ T, t ∈ S) → (∀ x, ¬ ∀ t ∈ T, holds t x) → c ∈ T := by
  intro T hT hclash
  apply Classical.byContradiction
  intro hc
  obtain ⟨x, hx⟩ := hdrop
  exact hclash x fun t ht => hx t (hT t ht) fun h => hc (h ▸ ht)

/-- The other direction of a probe: if the constraints without `c` still clash, `c` is not needed
for the clash. -/
theorem clash_without (holds : Name → X → Prop) (S : List Name) (c : Name)
    (h : ∀ x, ¬ ∀ d ∈ S, d ≠ c → holds d x) : ∀ x, ¬ ∀ d ∈ S, holds d x :=
  fun x hall => h x fun d hd _ => hall d hd

end Neo.Clash

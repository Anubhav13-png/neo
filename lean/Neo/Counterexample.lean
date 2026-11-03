/-!
# A per-row check: zero counterexamples means the claim holds

For `form: check` with `scope: rows`, `neo/proof/prover.py` runs
`COUNT(*) WHERE NOT COALESCE(claim, TRUE)` over the eligible rows. SQL's three-valued logic gives each row
`some true`, `some false` or `none` (a missing value). `COALESCE(claim, TRUE)` counts only `some false` as a
failure; the `none` rows are counted separately and reported as undecided.
-/
namespace Neo.Counterexample

variable {Row : Type}

/-- The row is a counterexample: the claim is decided and false (`NOT COALESCE(claim, TRUE)`). -/
def fails (claim : Row → Option Bool) (r : Row) : Bool :=
  !(claim r).getD true

theorem fails_iff (claim : Row → Option Bool) (r : Row) : fails claim r = true ↔ claim r = some false := by
  unfold fails
  cases h : claim r with
  | none => simp
  | some b => cases b <;> simp

/-- The counterexample count is zero, so on every row the claim is true or undecided, never false. -/
theorem holds_of_zero (claim : Row → Option Bool) (rows : List Row)
    (h : (rows.filter (fails claim)).length = 0) : ∀ r ∈ rows, claim r ≠ some false := by
  intro r hr hf
  have hmem : r ∈ rows.filter (fails claim) := List.mem_filter.mpr ⟨hr, (fails_iff claim r).mpr hf⟩
  rw [List.length_eq_zero_iff] at h
  rw [h] at hmem
  simp at hmem

/-- A positive count is a real counterexample on this snapshot. -/
theorem refuted_of_pos (claim : Row → Option Bool) (rows : List Row)
    (h : 0 < (rows.filter (fails claim)).length) : ∃ r ∈ rows, claim r = some false := by
  match hl : rows.filter (fails claim), h with
  | r :: _, _ =>
    have hm : r ∈ rows.filter (fails claim) := by rw [hl]; exact List.mem_cons_self
    obtain ⟨hr, hf⟩ := List.mem_filter.mp hm
    exact ⟨r, hr, (fails_iff claim r).mp hf⟩

end Neo.Counterexample

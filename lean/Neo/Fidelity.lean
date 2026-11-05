/-!
# Fidelity: the model's rows are the rows the question asks about

The fidelity check (`neo/verify/fidelity.py`) compares the model's row filter `m` with each requirement `q` the
question states. Z3 proves `m → q` for every possible row, and the per-run theorem restates it for Lean. The lake
also counts, on the pinned snapshot, the rows where `m` holds and `q` does not (it must be zero). These lemmas say
what those two facts give: every row the answer is about meets the requirement, and a filter equivalent to the
requirements selects exactly the same rows, so every count, sum or average over them is the same.
-/
namespace Neo.Fidelity

variable {Row : Type}

/-- `m → q` for every row: every kept row meets the requirement. -/
theorem kept_meet {m q : Row → Bool} (h : ∀ r, m r = true → q r = true) (rows : List Row) :
    ∀ r ∈ rows.filter m, q r = true := by
  intro r hr
  exact h r (List.mem_filter.mp hr).2

/-- Zero rows with `m` and not `q` in the snapshot: the same conclusion from the lake's count. -/
theorem kept_meet_of_count {m q : Row → Bool} (rows : List Row)
    (h : (rows.filter (fun r => m r && !q r)).length = 0) : ∀ r ∈ rows.filter m, q r = true := by
  intro r hr
  have hm := (List.mem_filter.mp hr)
  cases hq : q r with
  | true => rfl
  | false =>
    have : r ∈ rows.filter (fun r => m r && !q r) := List.mem_filter.mpr ⟨hm.1, by simp [hm.2, hq]⟩
    rw [List.length_eq_zero_iff] at h
    rw [h] at this
    simp at this

/-- A filter equivalent to the requirements selects exactly the same rows. -/
theorem same_rows {m q : Row → Bool} (h : ∀ r, m r = q r) (rows : List Row) : rows.filter m = rows.filter q := by
  have : m = q := funext h
  subst this; rfl

/-- So any aggregate over the selected rows (a count, a sum, an average) is the same. -/
theorem same_aggregate {m q : Row → Bool} {β : Type} (agg : List Row → β) (h : ∀ r, m r = q r) (rows : List Row) :
    agg (rows.filter m) = agg (rows.filter q) := by
  rw [same_rows h rows]

/-- A filter that implies the requirements keeps no more rows than they do. -/
theorem count_le {m q : Row → Bool} (h : ∀ r, m r = true → q r = true) :
    ∀ rows : List Row, (rows.filter m).length ≤ (rows.filter q).length
  | [] => by simp
  | r :: rs => by
    have ih := count_le h rs
    cases hm : m r <;> cases hq : q r <;> simp [hm, hq] <;> first | omega | (have := h r hm; simp_all)

end Neo.Fidelity

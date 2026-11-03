/-!
# Unit conversion

`neo/semantic/units.py` treats a unit as (dimension, scale): `USD_thousands` is USD with scale 3, a value
`v` meaning `v · 10³` dollars. The validator converts both sides to one scale before adding or comparing.
These lemmas are why converting first gives the same answer as working in dollars throughout.
-/
namespace Neo.Units

/-- Converting both sides by the same positive factor keeps every comparison. -/
theorem scale_cmp (a b k : Rat) (hk : 0 < k) : (a * k ≤ b * k ↔ a ≤ b) ∧ (a * k < b * k ↔ a < b) ∧
    (a * k = b * k ↔ a = b) := by
  have h1 : a * k ≤ b * k ↔ a ≤ b := by
    constructor
    · intro h
      apply Classical.byContradiction
      intro hn
      have : b * k < a * k := by
        have := Rat.mul_lt_mul_of_pos_right (Rat.not_le.mp hn) hk
        exact this
      grind
    · intro h; exact Rat.mul_le_mul_of_nonneg_right h (Rat.le_of_lt hk)
  have h2 : a * k < b * k ↔ a < b := by
    constructor
    · intro h
      apply Classical.byContradiction
      intro hn
      have := Rat.mul_le_mul_of_nonneg_right (Rat.not_lt.mp hn) (Rat.le_of_lt hk)
      grind
    · intro h; exact Rat.mul_lt_mul_of_pos_right h hk
  refine ⟨h1, h2, ?_⟩
  constructor
  · intro h; grind
  · intro h; rw [h]

/-- Converting each row then summing equals summing then converting. -/
theorem scale_sum (xs : List Rat) (k : Rat) : (xs.map (· * k)).sum = xs.sum * k := by
  induction xs with
  | nil => simp
  | cons x xs ih =>
    simp only [List.map_cons, List.sum_cons, ih]
    grind

/-- Two conversions in a row (for example thousands → units → billions) compose. -/
theorem rescale (a : Rat) (m n : Nat) : a * 10 ^ m * 10 ^ n = a * 10 ^ (m + n) := by
  induction n with
  | zero => simp
  | succ n ih => rw [Rat.pow_succ, Nat.add_succ, Rat.pow_succ]; grind

end Neo.Units

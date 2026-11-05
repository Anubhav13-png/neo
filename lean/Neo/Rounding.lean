/-!
# Honest rounding: a number shown in an answer is the exact value, rounded

The answer check (`neo/verify/answer.py`) accepts a written number `s` with unit `u` (the last digit shown, for
example 0.01 for "2.97") when the exact proved value `v` lies within half a unit of it. Each accepted number becomes
a per-run theorem `lo ≤ v ∧ v ≤ hi`, proved by Lean's kernel on the exact fractions. These lemmas say what such a
bound means, and that it survives the unit conversions the answer uses (thousands to billions, ratios to percent).
-/
namespace Neo.Rounding

/-- `s` shows `v` honestly at precision `u`: the exact value is within half a unit of what is written. -/
def Shows (v s u : Rat) : Prop := s - u / 2 ≤ v ∧ v ≤ s + u / 2

theorem shows_iff (v s u : Rat) : Shows v s u ↔ s - u / 2 ≤ v ∧ v ≤ s + u / 2 := Iff.rfl

/-- The per-run theorem is exactly `Shows`. -/
theorem shows_of_bounds {v s u : Rat} (lo : s - u / 2 ≤ v) (hi : v ≤ s + u / 2) : Shows v s u := ⟨lo, hi⟩

/-- Moving both by the same amount keeps an honest rounding (e.g. a difference of two shown values). -/
theorem shows_add {v s u : Rat} (h : Shows v s u) (c : Rat) : Shows (v + c) (s + c) u := by
  unfold Shows at *
  constructor <;> grind

/-- Negating keeps an honest rounding ("a loss of 5" for −5). -/
theorem shows_neg {v s u : Rat} (h : Shows v s u) : Shows (-v) (-s) u := by
  unfold Shows at *
  constructor <;> grind

/-- Two honest roundings of the same value at the same precision differ by at most one unit. -/
theorem shows_close {v s t u : Rat} (h₁ : Shows v s u) (h₂ : Shows v t u) : s - t ≤ u ∧ t - s ≤ u := by
  unfold Shows at *
  constructor <;> grind

/-- Equal values are shown the same way: a rounding proved for the proved value holds for the answer. -/
theorem shows_of_eq {v w s u : Rat} (hvw : v = w) (h : Shows w s u) : Shows v s u := by
  subst hvw; exact h

end Neo.Rounding

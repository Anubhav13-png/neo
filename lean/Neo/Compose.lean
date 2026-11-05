/-!
# Combining proved parts

A `form: plan` (`neo/proof/plan.py`) proves each part on its own (`N1 = a`, `N2 = b`) and then a combined value
`f(N1, N2)`. The per-run theorem checks `f(a, b) = c` on the exact fractions. These lemmas carry that to the parts
themselves: whatever is true of the proved values is true of the parts.
-/
namespace Neo.Compose

variable {α β γ : Type}

/-- Proved parts give the combined value. -/
theorem combine_eq (f : α → β → γ) {x a : α} {y b : β} (h₁ : x = a) (h₂ : y = b) : f x y = f a b := by
  subst h₁; subst h₂; rfl

/-- A combination computed from the proved values is the combination of the parts. -/
theorem combine_value (f : α → β → γ) {x a : α} {y b : β} {c : γ} (h₁ : x = a) (h₂ : y = b)
    (hc : f a b = c) : f x y = c := by
  subst h₁; subst h₂; exact hc

/-- A comparison decided on the proved values holds for the parts. -/
theorem compare_of_eq (r : Rat → Rat → Prop) {x a y b : Rat} (h₁ : x = a) (h₂ : y = b) (h : r a b) : r x y := by
  subst h₁; subst h₂; exact h

end Neo.Compose

/-!
# Exact arithmetic in the compiler

`neo/compiler/sql.py` compiles every number to a pair of integers (numerator, denominator) and never divides
inside the lake. These lemmas are the reasons that is exact:

* `frac_sub`        a/b − c/d = (a·d − c·b) / (b·d): how two values are subtracted before comparing
* `cmp_const_den`   with a positive constant denominator, compare the numerator with 0
* `cmp_sign`        with any non-zero denominator, compare sign(num) · sign(den) with 0 (`SIGN(n) * SIGN(d)`)
* `sum_const_den`   Σ (xᵢ / d) = (Σ xᵢ) / d: `SUM(numerator) / denominator` is the exact sum
* `decimal_exact`   a DECIMAL(p, s) value read as (x · 10ˢ) / 10ˢ is x
* `unique_of_unsat` Z3's "ans = t ∧ ans ≠ v is unsat" means t = v (the value proof of `form: value`)
-/
namespace Neo.Exact

theorem frac_sub (a b c d : Int) (hb : b ≠ 0) (hd : d ≠ 0) :
    (a : Rat) / b - (c : Rat) / d = ((a * d - c * b : Int) : Rat) / ((b * d : Int) : Rat) := by
  have hb' : (b : Rat) ≠ 0 := by exact_mod_cast hb
  have hd' : (d : Rat) ≠ 0 := by exact_mod_cast hd
  push_cast
  grind

/-- The two strict cases; the other three follow from them (`cmp_const_den`). -/
theorem strict_const_den (n d : Int) (hd : 0 < d) :
    ((n : Rat) / d < 0 ↔ n < 0) ∧ (0 < (n : Rat) / d ↔ 0 < n) := by
  have hd' : (0 : Rat) < d := by exact_mod_cast hd
  constructor
  · rw [Rat.div_lt_iff hd', Rat.zero_mul]; exact_mod_cast Iff.rfl
  · rw [Rat.lt_div_iff hd', Rat.zero_mul]; exact_mod_cast Iff.rfl

theorem cmp_const_den (n d : Int) (hd : 0 < d) :
    ((n : Rat) / d < 0 ↔ n < 0) ∧ ((n : Rat) / d ≤ 0 ↔ n ≤ 0) ∧ ((n : Rat) / d = 0 ↔ n = 0) ∧
    (0 < (n : Rat) / d ↔ 0 < n) ∧ (0 ≤ (n : Rat) / d ↔ 0 ≤ n) := by
  have ⟨h1, h2⟩ := strict_const_den n d hd
  grind

theorem div_neg_den (n d : Int) : (n : Rat) / d = ((-n : Int) : Rat) / ((-d : Int) : Rat) := by
  push_cast
  grind

theorem cmp_sign (n d : Int) (hd : d ≠ 0) :
    ((n : Rat) / d < 0 ↔ n.sign * d.sign < 0) ∧ ((n : Rat) / d ≤ 0 ↔ n.sign * d.sign ≤ 0) ∧
    ((n : Rat) / d = 0 ↔ n.sign * d.sign = 0) ∧
    (0 < (n : Rat) / d ↔ 0 < n.sign * d.sign) ∧ (0 ≤ (n : Rat) / d ↔ 0 ≤ n.sign * d.sign) := by
  rcases Int.lt_or_gt_of_ne hd with hneg | hpos
  · have h := cmp_const_den (-n) (-d) (by omega)
    rw [div_neg_den n d, Int.sign_eq_neg_one_of_neg hneg]
    rcases Int.lt_trichotomy n 0 with hn | hn | hn
    · rw [Int.sign_eq_neg_one_of_neg hn]; grind
    · subst hn; simp only [Int.sign_zero]; grind
    · rw [Int.sign_eq_one_of_pos hn]; grind
  · have h := cmp_const_den n d hpos
    rw [Int.sign_eq_one_of_pos hpos]
    rcases Int.lt_trichotomy n 0 with hn | hn | hn
    · rw [Int.sign_eq_neg_one_of_neg hn]; grind
    · subst hn; simp only [Int.sign_zero]; grind
    · rw [Int.sign_eq_one_of_pos hn]; grind

theorem sum_const_den (xs : List Int) (d : Int) :
    (xs.map fun (x : Int) => (x : Rat) / d).sum = (Int.cast xs.sum : Rat) / d := by
  induction xs with
  | nil => simp; grind
  | cons x xs ih =>
    simp only [List.map_cons, List.sum_cons, ih]
    push_cast
    grind

theorem decimal_exact (q : Rat) (s : Nat) : q * 10 ^ s / 10 ^ s = q := by
  apply Rat.mul_div_cancel
  have := Rat.pow_pos (a := (10 : Rat)) (n := s) (by decide)
  grind

theorem unique_of_unsat (t v : Rat) (h : ∀ ans : Rat, ¬ (ans = t ∧ ans ≠ v)) : t = v :=
  Classical.byContradiction fun hne => h t ⟨rfl, hne⟩

end Neo.Exact

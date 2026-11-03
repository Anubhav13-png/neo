/-!
# Top / bottom answers: the verification count proves the ranking

For `form: extremum`, `neo/proof/prover.py` fetches candidate rows with `ORDER BY … LIMIT`, then for each
rank runs `COUNT(*)` of the eligible rows strictly better than that candidate's value, and requires it to equal
the number of candidates strictly better.

`ranking_complete`: if the counts are equal, every row of the table that beats the value is already among the
candidates, so the ranking read off the candidates is the true ranking. Only the candidates need to be
distinct (they are: one row per key).
-/
namespace Neo.Extremum

variable {α : Type} [DecidableEq α]

/-- Distinct elements all taken from `l` cannot outnumber `l`. -/
theorem length_le_of_subset : ∀ (s l : List α), s.Nodup → (∀ a ∈ s, a ∈ l) → s.length ≤ l.length
  | [], _, _, _ => Nat.zero_le _
  | a :: s, l, hnd, hsub => by
    have ha : a ∉ s := (List.nodup_cons.mp hnd).1
    have hs : s.Nodup := (List.nodup_cons.mp hnd).2
    have hal : a ∈ l := hsub a List.mem_cons_self
    have hsub' : ∀ b ∈ s, b ∈ l.erase a := by
      intro b hb
      have hne : b ≠ a := fun h => ha (h ▸ hb)
      exact (List.mem_erase_of_ne hne).mpr (hsub b (List.mem_cons_of_mem a hb))
    have ih := length_le_of_subset s (l.erase a) hs hsub'
    have hlen := List.length_erase_of_mem hal
    have hpos : 0 < l.length := List.length_pos_of_mem hal
    simp only [List.length_cons]
    omega

/-- Pigeonhole: distinct `s ⊆ l` with `l` no longer than `s` means `l ⊆ s`. -/
theorem subset_of_length (s l : List α) (hs : s.Nodup) (hsub : ∀ a ∈ s, a ∈ l)
    (hlen : l.length ≤ s.length) : ∀ a ∈ l, a ∈ s := by
  intro a ha
  apply Classical.byContradiction
  intro hna
  have hsub' : ∀ b ∈ s, b ∈ l.erase a := by
    intro b hb
    have hne : b ≠ a := fun h => hna (h ▸ hb)
    exact (List.mem_erase_of_ne hne).mpr (hsub b hb)
  have h1 := length_le_of_subset s (l.erase a) hs hsub'
  have h2 := List.length_erase_of_mem ha
  have h3 : 0 < l.length := List.length_pos_of_mem ha
  omega

/-- The verification count of `form_extremum`. `better r` is "row `r`'s value is strictly better than the
candidate's" (`>` for max, `<` for min); `all` are the eligible rows, `cands` the fetched candidates. -/
theorem ranking_complete (all cands : List α) (better : α → Bool)
    (hcands : cands.Nodup) (hsub : ∀ c ∈ cands, c ∈ all)
    (hcount : (all.filter better).length = (cands.filter better).length) :
    ∀ r ∈ all, better r = true → r ∈ cands := by
  intro r hr hb
  have hs : ∀ a ∈ cands.filter better, a ∈ all.filter better := by
    intro a ha
    obtain ⟨hac, hab⟩ := List.mem_filter.mp ha
    exact List.mem_filter.mpr ⟨hsub a hac, hab⟩
  have key := subset_of_length (cands.filter better) (all.filter better)
    (hcands.filter better) hs (Nat.le_of_eq hcount)
  exact (List.mem_filter.mp (key r (List.mem_filter.mpr ⟨hr, hb⟩))).1

end Neo.Extremum

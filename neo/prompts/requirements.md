List what this question requires, as small claims a program can check against whatever model answers it.
You do not see that model, and you do not answer the question.

Question: {{QUESTION}}

Reply with one JSON object `{"requirements": [...]}`. Each item is one of:
- `{"id": "R1", "kind": "universe", "text": "...", "entity": "<entity id>"}`: whose rows the answer is about
- `{"id": "R2", "kind": "filter", "text": "...", "expr": "<per-row condition>"}`: a condition every row the answer covers must meet, written in the model language over the entity's fields (`year >= 1980 and year <= 1989`, `target_state == 'TX'`)
- `{"id": "R3", "kind": "measure", "text": "...", "aggregate": "count|sum|avg|share|min|max|list|check|possible|compare", "of": "<field, optional>"}`: what is computed
- `{"id": "R4", "kind": "group", "text": "...", "expr": "<per-row expression>"}`: one result per value of it (`target_state`, `year // 10 * 10`)
- `{"id": "R5", "kind": "order", "text": "...", "sense": "max|min", "top": 3}`: a ranking
- `{"id": "R6", "kind": "other", "text": "..."}`: anything else the answer must respect

Rules:
- Only what the question says or plainly implies. Do not add conditions it does not state. Do not drop rows for
  missing values; the model decides that and says so.
- A filter decides which rows the answer is about. A condition that is counted or shared ("what share were
  failed-bank deals") belongs to the measure, not to the filters.
- When the question compares groups, list as filters only the conditions every group shares, and add a measure with
  `"aggregate": "compare"` for the comparison.
- Use field names and text values exactly as the briefing shows them. Write each filter as its own item.
- Reply with the JSON object only.

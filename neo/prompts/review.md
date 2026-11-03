Review your model against the question before the answer is written.

Question: {{QUESTION}}

Your model and what the engine computed:
{{SUMMARY}}

Check:
- Does the model answer this exact question (right form, universe, rows, measure, time window)?
- Are the fields the ones whose terms match the question's concepts? Are joins and grain right (no double counting)?
- Are units converted, and missing values handled? Are the applicable block constraints honoured?
- For scenarios: one-period fields, the action scaled across decades, a baseline next to the result?
- Is the result plausible (an empty set or a zero count may mean a wrong text value)?

If the model is right, reply {"verdict": "ok"}.
If it is wrong, reply {"verdict": "revise", "issues": ["..."], "model": { the complete corrected model }}.
Only revise for a real error. Reply with the JSON object only.

Question: {{QUESTION}}
Asked by: {{USER}} (clearance {{CLEARANCE}}){{POLICIES}}

Write the model for this question.

Work through it before writing:
1. What exactly is asked? Which entity's rows, which measure, which unit, which time window? Write that as `interpretation`.
2. Which form fits?
3. Which entity is the universe, and which joins are needed? Is any join one-to-many (then set `grain`, and never sum the repeated side)?
4. Which fields carry each concept (choose them through their terms), and in which units? Convert before comparing.
5. Which `block` constraints apply (trigger words, fields or entities used)? Which approved rules?
6. For profit / risk / likelihood / "would it still" questions: which scenario set, how the action changes each period, how it scales with size, and which stated numbers need a `sensitivity` sweep?
7. Write every filter, definition, parameter, constraint and assumption as an expression.

Reply with only the JSON object.

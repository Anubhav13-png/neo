"""neo: company-scale, ontology-grounded, proof-carrying question answering over a data lake.

A question becomes a model written in ontology vocabulary (LLM), is validated against the ontology,
governance and units (code), compiled to exact SQL that runs inside the lake (code), and proved:
Z3 over the lake's exact aggregates plus verification queries over every row, cross-checked by an
independent interpreter (code). See README.md for the architecture.
"""

__version__ = "0.1.0"

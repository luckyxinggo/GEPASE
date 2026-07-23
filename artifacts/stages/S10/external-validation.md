# S10 fresh-install validation

Status: passed

A compact sdist and wheel were built from the dirty-but-curated release tree. The wheel was installed with `uv pip --offline` into a new virtual environment, then its version, root help, and config validation were executed. No Agent, external LLM API, candidate search, R3 rerun, or R4 rerun was performed.

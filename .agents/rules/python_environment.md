# Python Environment and Local Tooling Rule

- Always use the repository virtual environment at `.venv/` (`.venv/bin/python`,
  `.venv/bin/pip`, `.venv/bin/<tool>`) for all Python dependency management, local
  development, and local testing.
- Never install Python packages globally or run global `pip install`.
- Ensure scripts and test runners invoke `.venv/bin/python` when running local
  Python tasks outside container boundaries.

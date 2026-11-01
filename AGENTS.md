# Repository checks

Follow the [static-check policy](CONTRIBUTING.md).

- Run `python scripts/check.py` in the installed development environment before
  delivering code changes. Use the locked environment described in CONTRIBUTING.md.
- Run the relevant tests; run the full suite for document geometry, worker lifecycle,
  serialization, or shared editing-state changes.
- Do not introduce new Basedpyright baseline entries or blanket diagnostic exclusions
  to make a change pass. Explain and review any deliberate policy change.
- Keep dynamic callback exceptions explicit in `scripts/vulture_allowlist.py`.
- Place new application modules in the Import Linter layer contract and keep imports
  acyclic. Preserve the independence of document logic from the window and entry point.
- Do not infer Windows/Linux executable qualification from source or headless tests.

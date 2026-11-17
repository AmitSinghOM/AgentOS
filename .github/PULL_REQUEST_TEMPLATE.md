<!-- One sentence: what changes for a user or operator, and why. Link the issue or audit row. -->

## Checklist

- [ ] **Red first.** The new or changed test fails on `main` and passes here; the two one-line
      `pytest` summaries (fix stashed → fails, fix restored → passes) are pasted below.
- [ ] **FAIL_MODES.** If a chokepoint's direction changed or a new one appeared, `docs/FAIL_MODES.md`
      has the row (direction + pinning test) and the preamble's row count is bumped; otherwise
      "no boundary changed" is stated below.
- [ ] **Docs.** Every sentence in `README.md`, `docs/`, a provider README or `.env.example` that this
      change makes true or false is updated; stale claims are deleted rather than re-guessed.
- [ ] **No secrets.** No token, key, DSN password or prompt/model output in code, tests, fixtures,
      cassettes, logs or this description — hashes and prefixes only.
- [ ] Gate run locally: `ruff check .` · `lint-imports` · `pytest -q --ignore=tests/chaos`
      (and `cd ui && npm ci && npm test && npm run build` if `ui/` changed).

## Discrimination proof

```text
# fix stashed:
# fix restored:
```

## Boundary

<!-- "no boundary changed", or the FAIL_MODES row(s) touched -->

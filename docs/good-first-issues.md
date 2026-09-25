# Good first issues

Ready to open as GitHub issues (label `good first issue`) when the repository goes public. Each one is small,
has a clear end, and needs no key to test.

1. **French on the live page.** Copy the English block of `I18N` in `src/siac/web/index.html` and translate it.
2. **German on the live page.** Same as above.
3. **Pause and resume a replay** with the space bar on the live page.
4. **Copy button for each task result** in the "Selected task" panel.
5. **Tokens per task** (in and out) in the "Selected task" panel; the events already carry them.
6. **`siac runs`**: list saved runs with date, status, cost and saving, newest first.
7. **`siac models --json`**: the catalog as JSON, for scripts.
8. **`siac export --csv`**: one row per task (id, title, tier, model, cost) for spreadsheets.
9. **A `--timeout` option for `siac run`**, ending the run as partial when it is reached.
10. **Test for `stitch`** with an outline that names a result twice and one that names a missing result.
11. **Add Mistral models to the catalog** with prices from `siac models --check` and a proposed tier.
12. **Show the provider route** (Vercel, direct, local) next to each model on the live page.

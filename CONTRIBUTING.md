# Contributing to SIAC

Thanks for helping. SIAC is small on purpose: a few Python files, one YAML catalog and one HTML page.

## Set up

```bash
git clone https://github.com/cintocasals/siac && cd siac
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q              # no key needed: tests use a scripted gateway
siac demo              # the whole flow with simulated models
```

## Good ways to help

- **Add or re-tier a model.** Edit `src/siac/models.yaml`, run `siac models --check` for prices, and open a pull
  request that says why the tier fits. Benchmark numbers beat opinions.
- **Run the benchmark** (`bench/public/`) with your keys and share the report in an issue.
- **Translate the live page.** The strings are in `src/siac/web/index.html` (`I18N`); copy the English block.
- **Improve a prompt** (planner, worker, aggregator) with a before and after on the development cases
  (`bench/run_dev.py --dry-run` first, then live with a small `--total-budget`).
- Issues labelled `good first issue` are a gentle start.

## Rules of the house

- Keys never go into code, tests, logs, runs or issues. Tests must pass without any key.
- A change that affects cost or quality comes with numbers: the development cases or the public benchmark.
- Plain words in user-facing text: say what happens, not how it is built.
- Python 3.10 or newer, standard library first, no new dependency without a reason.
- One topic per pull request, with a test when behaviour changes.

## Pull requests

Describe what changes for the person using SIAC, how you tested it, and, if it touches cost or quality, what
the numbers were. CI runs the tests on Python 3.10, 3.12 and 3.13 and builds the Docker image.

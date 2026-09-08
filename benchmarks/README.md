# Coding-agent benchmarks

The dataset in `coding_tasks.json` is a task contract, not executable code. Each task is run against a fresh copy of its fixture and checked by a structured verifier plus trace assertions.

Run the deterministic harness checks with:

```powershell
uv run python scripts/run_benchmark.py --mode fake
```

Run the same contracts against the configured provider with:

```powershell
uv run python scripts/run_benchmark.py --mode real
```

Fake mode is the deterministic regression baseline. Real mode is observational; repeat it to compare success rate, attempts, tool calls, rounds, and duration. Results are stored below `.coding-agent/benchmark-runs/<mode>/`.

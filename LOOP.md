# LOOP.md — How to Run This Overnight

This runs the **TONIGHT** scope in `PROGRESS.md` unattended and stops at the
`⛔ STOP` boundary. It builds the deterministic core (schema, validation,
routing, backend interface + offline stub, stub-backed core pipeline). It needs
**no Gemini key and no Ollama** — those are tomorrow.

## Before you start

- Be on a throwaway branch or an isolated worktree so the night's work is
  contained and easy to review:
  `git switch -c overnight-build` (or `git worktree add ../overnight overnight-build`).
- Have these in the repo root: `CLAUDE.md`, `PROGRESS.md`, `LOOP.md`,
  `.claude/agents/verifier.md`, and the specs in `docs/`.
- Be logged into Claude Code as usual. The loop uses your existing auth; on a
  subscription it counts against your normal weekly rate limit, on an API key it
  bills pay-as-you-go (the `--max-budget-usd` guard below applies in that case).

## Recommended: the driver loop (one task per iteration, commit each)

A fresh context per iteration means no context-window drift over a long night,
each task is its own commit, and a single crashed iteration just resumes from
the ledger next loop.

```bash
#!/usr/bin/env bash
# run-overnight.sh — stops itself at the PROGRESS.md ⛔ STOP boundary
set -u
for i in $(seq 1 20); do
  echo "=== iteration $i ==="
  claude -p "Read PROGRESS.md, CLAUDE.md, and the specs in docs/. Do the NEXT
    unchecked task in the TONIGHT section only — exactly one. Implement it per
    docs/05_build_plan.md. Run that task's Check command and paste the output.
    If it passes: commit just that task's changes with its Commit message, then
    tick its box in PROGRESS.md and commit that. If it fails after reasonable
    attempts: add a one-line note under BLOCKED in PROGRESS.md, commit, and move
    on. NEVER start a TOMORROW task. NEVER add a dependency outside task N1's
    night set. NEVER edit files in docs/. When every TONIGHT box is checked, run
    'uv run pytest -q' and 'uv run ruff check .'; if both are clean, print
    exactly DONE_ALL." \
    --allowedTools "Read,Edit,Write,Bash" \
    --permission-mode acceptEdits \
    --max-turns 25 \
    --max-budget-usd 0.75 \
    --output-format json | tee "run_$i.json"
  if grep -q "DONE_ALL" "run_$i.json"; then
    echo "All TONIGHT tasks complete."; break
  fi
done
```

Launch it and walk away:

```bash
chmod +x run-overnight.sh
nohup ./run-overnight.sh > overnight.log 2>&1 &
```

(`nohup ... &` keeps it running if the terminal closes. For a laptop that
sleeps, run it in `tmux` on a machine that stays awake.)

## Alternative: a single `/goal` session

Simpler, one process. `/goal` keeps the session going until a small evaluator
model confirms the condition from the transcript, so the condition ends by
running the proof commands. It has no built-in budget, hence the turn cap.

```bash
claude -p "/goal Every task in the TONIGHT section of PROGRESS.md is checked
  off, and 'uv run pytest -q' exits 0, and 'uv run ruff check .' reports no
  errors. Work one TONIGHT task at a time, top to bottom; after each task's
  Check passes, commit it with its PROGRESS.md message and tick its box. Never
  start a TOMORROW task. Never add a dependency outside task N1's night set.
  Never edit docs/. Prove completion by running pytest and ruff and printing
  'cat PROGRESS.md' at the end. Stop after 60 turns regardless." \
  --allowedTools "Read,Edit,Write,Bash" \
  --permission-mode acceptEdits
```

The driver loop is the safer choice for a long unattended night; `/goal` is
fine if you prefer one process and will glance at it.

## Guardrails (why this is safe to leave)

- **Hard scope:** both the ledger and the prompt forbid starting TOMORROW work
  or adding model dependencies, so it cannot wander into Gemini/Ollama.
- **Isolation:** running on a branch/worktree means the morning review is a
  clean diff and nothing touched `main`.
- **Caps:** `--max-turns` (and `--max-budget-usd` on API billing) stop a runaway
  iteration; the loop caps total iterations.
- **Broad Bash is granted** so it can run `uv`, `pytest`, and `git` — which is
  exactly why you keep it on an isolated branch.

## In the morning

1. `git log --oneline` — you should see roughly one commit per N-task.
2. **Read the diffs.** Agents are good at *looking* done; the test suite is the
   real gate, but skim the code. `uv run pytest -q` yourself.
3. Check `PROGRESS.md`: which TONIGHT boxes are ticked, and read any **BLOCKED**
   entries — those are your first fixes.
4. Then continue with TOMORROW: set up the Gemini key (and/or Ollama), implement
   the Docling/OCR parsing and the two backends, wire the real `acquire` into
   `core.py`, then persistence, the watcher, the web demo, and the eval harness.

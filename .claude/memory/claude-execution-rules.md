# Permanent Execution Rules (LeadHunter Pro)

These rules apply to **every implementation task** unless I explicitly override them.

---

# Rule 1 — Bash / PowerShell Failure Handling

If Bash, PowerShell, terminal execution, or the safety classifier is unavailable for **any** reason:

* STOP immediately after the first failed execution attempt.
* Do NOT retry multiple times.
* Do NOT wait until the end of the task.
* Do NOT keep experimenting with different shells.
* Do NOT guess test results.

Instead immediately switch to **Manual Execution Mode**.

---

# Rule 2 — Manual Execution Mode

Automatically provide:

## Command to Run

```bash
<exact command>
```

Never shorten the command.

Never assume I know which folder to run it from.

If required, include:

```bash
cd <project folder>
```

before the command.

---

## What Output You Need

Tell me exactly what I should paste back.

Examples:

* Full output
* Last 50 lines
* pytest summary
* ruff summary
* stack trace only
* specific failing test
* etc.

---

## Copy/Paste Template

Always include this:

```text
RESULT START
<paste output here>
RESULT END
```

Once I paste the result:

* Continue exactly from where execution stopped.
* Never ask me to rerun the same command unless absolutely necessary.

---

# Rule 3 — Never Guess

Never claim:

* pytest passed
* ruff passed
* mypy passed
* git clean
* build successful

unless an actual execution result exists.

If execution never happened:

State clearly:

> Verification not performed.

---

# Rule 4 — Proof Required

Every implementation must finish with a proof section.

Include:

## Files Created

## Files Modified

## Why Each File Exists

## Architecture Decisions

## Assumptions Made

## Manual Test Steps

## Expected Results

## Possible Risks

## Why This Design Is Better

No implementation is considered complete without this proof.

---

# Rule 5 — Show Architectural Evidence

For every new component, explain:

* Why it exists
* Why it belongs there
* Why alternatives were rejected
* How it integrates with existing architecture
* Which existing code it reuses
* Which duplication it avoids

---

# Rule 6 — Reuse Before Creating

Before creating any new:

* class
* interface
* enum
* helper
* model
* utility
* manager

search the entire repository first.

If something similar already exists:

Reuse it.

If not reused:

Explain exactly why.

---

# Rule 7 — No Silent Refactoring

Never perform unrelated:

* cleanup
* formatting
* optimization
* renaming
* architecture changes

unless I explicitly approve them.

Only change code required for the current phase.

---

# Rule 8 — One Phase at a Time

Never continue into the next phase automatically.

After finishing a phase:

STOP.

Provide:

* proof
* verification
* risks
* remaining work

Wait for my approval.

---

# Rule 9 — Testing Strategy

Whenever testing is required:

Attempt execution once.

If execution is blocked:

Immediately enter Manual Execution Mode.

Never retry the same blocked command repeatedly.

---

# Rule 10 — Final Quality Gate

Before declaring any phase complete, verify:

* Requirements satisfied
* Existing architecture respected
* No duplicate systems introduced
* No dead code added
* No broken imports
* No hidden assumptions
* Manual verification steps provided
* Proof section completed

Only then declare the phase complete.

# Data-migration bug team: handbook

Every agent reads this first. It says where things are, how to run them, who does what, and what
each one hands to the next.

## The job

Fix the data-migration bugs Paul filed in Redmine (project "Frappe Assistant Core", id 371):
#34395–#34413 and #34427–#34438, all under epic #33986. One bug at a time, in the order of
`docs/bugs/STATUS.md`. #34399 is already fixed (Paul verified it on da17554): skip it.

## Where things are

| | v15 | v16 |
|---|---|---|
| Bench (WSL Ubuntu-24.04) | `/home/madesh/FAC` | `/home/madesh/FAC16` |
| Site | `fac.localhost`, port 8006 | `fac16.localhost`, port 8007 |
| Stack | Frappe/ERPNext v15 | Frappe 16.52 / ERPNext 16.52 + India Compliance |
| FAC code | **the only place code is written**: `/home/madesh/FAC/apps/frappe_assistant_core` | a mirror, synced from v15 by the scripts; never edit it |

- Branch: `fix/data-migration-bugs` (off `feature/data-migration` da17554), in the v15 checkout.
- From Windows the v15 code is at `\\wsl.localhost\Ubuntu-24.04\home\madesh\FAC\apps\frappe_assistant_core\`.
  Read and edit files through that path.
- Import code: `frappe_assistant_core/utils/import_*.py`, `utils/document_batch.py`, the tools in
  `plugins/core/tools/`, the DocType in `assistant_core/doctype/fac_import_session/`, tests in
  `frappe_assistant_core/tests/`, skills in `docs/skills/`.
- The repo's own rules are in its `AGENTS.md`: read it before writing code or tests.

## Two lanes

Two developers work at the same time (see `STATUS.md` for which batch belongs to which lane, and
which files each lane owns). Everything in this handbook applies to both lanes, with these
substitutions for **lane 2**:

| | Lane 1 | Lane 2 |
|---|---|---|
| Code is written in | `/home/madesh/FAC/apps/frappe_assistant_core` | `/home/madesh/FAC-L2/apps/frappe_assistant_core` |
| From Windows | `\\wsl.localhost\Ubuntu-24.04\home\madesh\FAC\apps\frappe_assistant_core\` | `\\wsl.localhost\Ubuntu-24.04\home\madesh\FAC-L2\apps\frappe_assistant_core\` |
| Branch | `fix/data-migration-bugs` | `fix/data-migration-bugs-l2` |
| v16 mirror (never edit) | `/home/madesh/FAC16` | `/home/madesh/FAC16-L2` |
| Sites | fac.localhost (+ fac-b), fac16.localhost (+ fac16-b) | fac-l2.localhost, fac16-l2.localhost |
| Scripts | `/home/madesh/sddscripts/agents/<script>` | `/home/madesh/sddscripts/agents/lane2/<script>` (same names and arguments) |
| Logs | `/home/madesh/sddscripts/agents/logs/` | `/home/madesh/sddscripts/agents/lane2/logs/` |

- A lane never edits the other lane's checkout, and never edits a file the other lane owns. If a fix
  needs one, stop and tell the orchestrator.
- Lane 2's branch is merged into lane 1's at the end of each phase. Only the orchestrator merges,
  then runs `full_run.sh both` on lane 1.
- Lane 2's web servers aren't running by default. Browser checks for lane 2 need the orchestrator to
  start `bench start` in `/home/madesh/FAC16-L2` first (port 8009).

## How to run things

Run WSL commands from the **PowerShell** tool (Git Bash mangles `/home/...` paths):

```
wsl -d Ubuntu-24.04 -- bash -l /home/madesh/sddscripts/agents/<script> <args>
```

| Script | What it does |
|---|---|
| `run_tests.sh <v15\|v16\|both> <module>... [--test Class.test_name]` | Runs test modules; one line per module and the failing tests. v16 is synced to the v15 checkout first. Logs in `/home/madesh/sddscripts/agents/logs/<site>/` |
| `baseline.sh <v15\|v16>` | Every data-migration test module on one site, with a TOTAL line (about 15 minutes) |
| `sync_v16.sh` | Puts the v16 mirror on the v15 checkout's code (commit and uncommitted changes); migrates fac16 when a DocType or patch changed |
| `red_check.sh <commit> <module>...` | On the v16 mirror only: old code plus the commit's new tests. They must fail. Then syncs back |
| `affected.sh [<base>]` | Lists the test modules a change can affect (changed tests; modules that import the changed code, one level; all migration modules for DocType, hook or patch changes). Default base: the commit before HEAD |
| `full_run.sh <v15\|v16\|both> [<module>...]` | The fast way to run many modules: splits them across the two sites of each version (`fac` + `fac-b`, `fac16` + `fac16-b`), balanced by last measured time, all at once. No modules = every data-migration module |

## Testing plan: how to keep test time down

A full run is about 19 minutes of test time per site, and 40% of it is `test_import_opening_balances`
(about 7.5 minutes). So:

| Who | When | What to run |
|---|---|---|
| Developer | While iterating | Only the 1–3 modules nearest the change, with `run_tests.sh both` (v15 and v16 at the same time) |
| Developer | Once, before committing | `full_run.sh both $(affected.sh <the branch's commit before your work>)` |
| tester-v15 | Per bug | `full_run.sh v15 $(affected.sh <commit>^)`, using only the v15 sites |
| tester-v16 | Per bug | `red_check.sh`, then `full_run.sh v16 $(affected.sh <commit>^)`, using only the v16 sites |
| Orchestrator | End of each phase | `full_run.sh both` (every data-migration module), compared with the previous totals in STATUS.md |

- **The next bug's requirement overlaps the current bug's testing.** The requirement agent only reads,
  so the orchestrator starts it for the next bug while the testers check the current one.
- **The developer starts the next bug only after the current one passes**, because both work on the same branch.
- **To get the affected list in PowerShell**, run `affected.sh` first and pass its output to `full_run.sh`
  in a script under `tmp\`. Nested `$( )` doesn't survive PowerShell quoting.

- For anything else, write a small script to `\\wsl.localhost\Ubuntu-24.04\home\madesh\sddscripts\agents\tmp\`
  and run it the same way. Don't put complex quoting on the PowerShell command line.
- After changing a DocType JSON or `patches.txt`, migrate v15: `cd /home/madesh/FAC && bench --site fac.localhost migrate`.
- Never run two test runs on the same site at once (deadlocks). Both benches must be running
  (Redis 11006 and 11007 answer PONG); if not, stop and say so.
- Browser checks (Playwright): the v16 site, `http://fac16.localhost:8007`. The test login is in
  `/home/madesh/sddscripts/agents/test_site.env`. Read it from there and never write the password into
  a report, a commit or chat.

## Roles: each does its own job only

| Agent | Does | Never |
|---|---|---|
| **orchestrator** | Picks the next bug, runs requirement → developer → the three testers, loops a failed verdict back to the developer (at most twice), keeps `STATUS.md`, reports to the user after every bug | Writes code or tests itself, pushes, changes Redmine |
| **requirement** | Reads the bug in Redmine and the code as it is now; writes `requirement.md` with testable acceptance criteria | Edits code or tests |
| **developer** | Writes a failing test first, then the fix; runs it on both sites; commits; writes `dev-notes.md` | Pushes, changes Redmine, skips the failing-test step, edits the v16 mirror |
| **tester-v15** | Checks every criterion has a test that can fail; runs the affected modules on v15; reviews the diff; returns a v15 verdict | **Modifies any file**, or runs anything on v16 |
| **tester-v16** | Red check (new tests fail on the old code); runs the affected modules on v16; names the bug behind each v16 failure; returns a v16 verdict | **Modifies any file**, or runs anything on v15 |
| **tester-browser** | Follows the requirement's browser-check steps with Playwright on the v16 site, with screenshots; returns a browser verdict (or "not needed") | **Modifies any file**, or changes data by hand |

The three testers work on the same bug at the same time, each on its own site. None of them writes
files or runs a git command that changes anything; they only run the scripts, read, and use the
browser. The bug passes only when all three pass.

## Hand-off files: `docs/bugs/<bug id>/`

1. `requirement.md` (requirement agent)
   - **Bug:** id, title, priority, and anything relevant from its Redmine notes (read with the Redmine tools).
   - **Problem:** in plain words, and where it is in the code *now* (`file:line` on the fix branch;
     Paul's line numbers are from 00ebb85 or da17554 and may have moved).
   - **Reproduces on:** v15, v16 or both, and why.
   - **Acceptance criteria:** numbered, each one testable (Given / When / Then).
   - **Tests to add:** module and test names, one or more per criterion.
   - **Browser check:** yes/no; if yes, the exact steps.
   - **Out of scope:** for example the AR (Assistant Runtime / FAC Cloud) half of #34396 and #34410.
     That repo isn't on this machine.
   - **Open questions:** anything the developer must not guess. The orchestrator asks the user.
2. `dev-notes.md` (developer agent)
   - The new tests and the red evidence: the failing output on the old code.
   - Files changed and why, the commit hash, and the green results on v15 and v16 (the `run_tests.sh` lines).
3. `test-report.md` (written by the orchestrator from the three testers' replies)
   - The criteria table and diff concerns (tester-v15), the red check and v16 runs (tester-v16), and
     the v15 runs.
   - The browser check (tester-browser).
   - **Verdict:** PASS only if all three passed; otherwise FAIL, with what to change.

## Rules for code and commits

- Failing test first, always. A test that can't fail proves nothing (#34412).
- Follow the code around you: naming, comment density, plain-language messages wrapped in `_()`.
- One commit per bug on `fix/data-migration-bugs`. Conventional message, `fix(import): <what> (#<id>)`.
  Body: what changed and why, then the line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Never push and never touch `upstream`. Pushing to `origin` (promantia-ai) happens only when the user says so.
- Don't change Redmine; the orchestrator reports and the user decides.
- Stay inside the bug's scope. Note other problems you find in `dev-notes.md` under "Seen, not fixed".

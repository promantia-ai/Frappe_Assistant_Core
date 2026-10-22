# Handoff: continue the data-migration bug fixes on another system

Stopped 2026-10-22 (out of usage on the first machine). Everything needed is in the repo.

## Branches on origin (promantia-ai/Frappe_Assistant_Core)

| Branch | What | State |
|---|---|---|
| `fix/data-migration-bugs` | Lane 1, verified work | #34430 done (de2df60), passed v15 + v16 testers |
| `wip/34395-dry-run-v16` | Lane 1, #34395 in progress (on top of the above) | **Unverified WIP**: the developer was mid-fix. Also holds this team folder |
| `wip/L2-B-34396` | Lane 2, L2-B (#34396 batch create/update) in progress | **Unverified WIP**: coded, its test run hadn't finished |

## The team (in `docs/bug-team/` on `wip/34395-dry-run-v16`)

- `agents/`: the agent definitions. Copy them to `<your project>/.claude/agents/`; they load when a session starts:
  orchestrator (run with `claude --agent orchestrator`), requirement, developer, tester-v15, tester-v16, tester-browser.
- `TEAM.md`: the handbook: benches, sites, scripts, roles, hand-off files, the testing plan, the two lanes.
- `STATUS.md`: the queue in batches and lanes, every user decision, and the baselines.
- `<bug or batch>/`: requirement.md, dev-notes.md, test-report.md per bug. Done: 34430. Requirements ready: 34395, L2-A, L2-B, L2-C.
- `scripts/`: the test scripts (`run_tests.sh`, `full_run.sh`, `affected.sh`, `affected_imports.py`, `sync_v16.sh`,
  `red_check.sh`, `baseline.sh`, and the bench builders `make_shard_sites.sh`, `make_lane2.sh`, `make_lane2_scripts.sh`).
  They assume this machine's paths (`/home/madesh/FAC`, `/home/madesh/FAC16`, ports 8006/8007, Redis 11006/11007);
  edit the paths at the top for the new system.

## What the new system needs

1. A v15 bench (Frappe/ERPNext v15) and a v16 bench (Frappe/ERPNext 16.52 + India Compliance 16.x, Python 3.14,
   Node 24), each with FAC on the branch you continue; sites with `allow_tests` and ERPNext's setup done
   (the v16 site used an Indian company, INR).
2. Run `bench migrate` on both sites after checking out the branch.
3. Baselines to compare against (every data-migration module): v15 1,054 ran, 0 failures; v16 1,054 ran,
   277 failures + 43 errors after #34430, of which 312 are #34395.

## Where to pick up

1. **#34395** (lane 1): read `docs/bug-team/34395/requirement.md`, review the WIP diff on `wip/34395-dry-run-v16`,
   finish the tests and the fix, then run the testers. Target on v16: only the 7 known failures left
   (5 for #34428, 1 for #34412, 1 pre-existing KeyError in undo).
2. **L2-B / #34396** (lane 2): review `wip/L2-B-34396`, run its tests, then the testers. Requirement in `L2-B/`.
3. Then L2-A (moved to lane 1), L2-C (requirement ready), and the rest of `STATUS.md`.
4. Nothing is merged into `feature/data-migration` and nothing is updated in Redmine yet.

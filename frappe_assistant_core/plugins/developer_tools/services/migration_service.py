# Copyright (C) 2025 Promantia
# Developer Tools Plugin — MigrationService

import os
import shutil

import frappe
import psutil

from frappe_assistant_core.plugins.developer_tools.guards import build_failure

MAX_MIGRATE_SECONDS = 60 * 60


def _read_pid_file(pid_file):
    """
    Returns (pid, recorded_start_time) or None if the file is missing/unparseable.
    recorded_start_time is None for pid files written before create_time tracking
    was added (bare-int format) — reuse/timeout detection is then skipped for that
    one in-flight run, falling back to liveness-only behavior.
    """
    try:
        with open(pid_file) as f:  # fmt: skip  # nosemgrep: frappe-security-file-traversal — reading PID from well-known bench logs directory
            raw = f.read().strip()
        if ":" in raw:
            pid_str, start_str = raw.split(":", 1)
            return int(pid_str), float(start_str)
        return int(raw), None
    except (FileNotFoundError, ValueError):
        return None


class MigrationService:
    """
    Business logic for bench_execute's migration-related actions.

    Each @staticmethod implements one action and takes the raw arguments
    dict passed to BenchExecute.execute(). bench_execute.py's execute()
    calls straight into these and returns their result directly — no
    try/except here or at the call site, so ValidationError/PermissionError
    propagate uncaught to base_tool.py's error handling.
    """

    @staticmethod
    def migrate_status(arguments):
        import subprocess
        import time

        bench_path = frappe.utils.get_bench_path()
        log_file = os.path.join(bench_path, "logs", "fac_migrate.log")
        pid_file = os.path.join(bench_path, "logs", "fac_migrate.pid")
        exitcode_file = os.path.join(bench_path, "logs", "fac_migrate.exitcode")
        restarted_file = os.path.join(bench_path, "logs", "fac_migrate.restarted")
        restart_flag_file = os.path.join(bench_path, "logs", "fac_migrate.restart_flag")

        pid_file_exists = os.path.exists(pid_file)

        parsed = _read_pid_file(pid_file)
        still_running = MigrationService._is_pid_running(pid_file)

        stale_timeout = False
        if still_running and parsed is not None and parsed[1] is not None:
            elapsed = time.time() - parsed[1]
            if elapsed > MAX_MIGRATE_SECONDS:
                still_running = False
                stale_timeout = True

        output = ""
        try:
            with open(log_file) as f:  # fmt: skip  # nosemgrep: frappe-security-file-traversal — reading well-known bench logs directory
                output = f.read()[-800:]
        except Exception:
            output = "Log file not found"

        # Not running: figure out *why* before reporting anything — never started,
        # killed before it could record an exit code, or genuinely finished (success
        # or failure) — and only fire `bench restart` the first time we observe a
        # finished run for this migrate() invocation.
        exit_code = None
        elapsed_seconds = None
        restart_warning = None
        if still_running and parsed is not None and parsed[1] is not None:
            # Round once, up front, so the message text and the elapsed_seconds
            # field below always agree — no separate int()-truncation vs round()
            # producing off-by-one-second mismatches between the two.
            elapsed_seconds = round(time.time() - parsed[1])

        if still_running:
            status = "running"
            success = True
            if elapsed_seconds is not None:
                mins, secs = elapsed_seconds // 60, elapsed_seconds % 60
                message = (
                    f"Migrate is still running normally (elapsed {mins}m {secs}s). "
                    f"This is expected for larger migrations — keep polling; it is not stuck."
                )
            else:
                message = "Migrate is still running normally. Keep polling; it is not stuck."
        elif stale_timeout:
            status = "stale_timeout"
            success = False
            pid = parsed[0] if parsed is not None else "unknown"
            message = (
                f"Migrate has been running for over {MAX_MIGRATE_SECONDS // 60} minutes "
                f"with no exit code recorded. That exceeds the normal migration window and "
                f"likely means it is genuinely hung (not just slow) — check `ps aux` for PID "
                f"{pid} and the log file directly; you may need to kill it and re-run."
            )
        elif not pid_file_exists:
            # migrate() has never been called on this bench — nothing to report and
            # nothing to restart.
            status = "not_started"
            success = True
            message = "No migration has been run yet."
        else:
            try:
                with open(exitcode_file) as f:  # fmt: skip  # nosemgrep: frappe-security-file-traversal — reading well-known bench logs directory
                    exit_code = int(f.read().strip())
            except (FileNotFoundError, ValueError):
                exit_code = None

            if exit_code is None:
                # PID is gone but no exit code was ever recorded — the wrapper
                # process was almost certainly killed outright (SIGKILL, SIGTERM,
                # OOM) before it could write fac_migrate.exitcode. We genuinely
                # don't know whether `bench migrate` itself succeeded, so don't
                # report success and don't restart.
                status = "unknown"
                success = False
                message = (
                    "Migrate process ended without recording an exit code "
                    "(it may have been killed). Check the output/log and re-run migrate if needed."
                )
            else:
                if not os.path.exists(restarted_file):
                    # Honor the restart=False the caller passed to migrate() — read the
                    # flag it recorded at launch instead of always restarting
                    # unconditionally. Missing flag file (e.g. a run started before this
                    # fix) defaults to True, preserving the old always-restart behavior.
                    try:
                        with open(restart_flag_file) as rff:  # fmt: skip  # nosemgrep: frappe-security-file-traversal — reading restart flag from well-known bench logs directory
                            should_restart = rff.read().strip() != "0"
                    except FileNotFoundError:
                        should_restart = True

                    # `bench restart` only does anything if a process manager it knows how
                    # to drive is actually present (overmind, or supervisorctl on PATH) —
                    # otherwise it's a silent no-op that exits 0. On a `bench start`/honcho
                    # dev setup (no supervisor, no overmind) there's nothing to restart; the
                    # dev server relies on its own autoreload instead. Skip the pointless
                    # subprocess call and say so, rather than implying a restart happened.
                    if should_restart and not (shutil.which("supervisorctl") or shutil.which("overmind")):
                        restart_warning = (
                            "No process manager (supervisorctl/overmind) found on this bench, "
                            "so 'bench restart' would be a no-op — skipped it. If this is a "
                            "`bench start` dev setup, the web server's own autoreload handles "
                            "picking up code changes instead."
                        )
                        should_restart = False

                    if should_restart:
                        restart_result = subprocess.run(
                            ["bench", "restart"], capture_output=True, text=True, cwd=bench_path
                        )
                        if restart_result.returncode != 0:
                            restart_warning = (
                                f"'bench restart' exited with status {restart_result.returncode}: "
                                f"{restart_result.stderr.strip()}. Workers may still be serving "
                                f"pre-migrate code until they're restarted manually."
                            )
                    with open(restarted_file, "w") as rf:  # fmt: skip  # nosemgrep: frappe-security-file-traversal — writing restart marker to well-known bench logs directory
                        rf.write(str(exit_code))

                if exit_code == 0:
                    status = "completed"
                    success = True
                    message = "Migrate completed!"
                else:
                    status = "failed"
                    success = False
                    message = f"Migrate failed with exit code {exit_code}. Check the output/log for details."

        if still_running:
            time.sleep(15)  # enforce 15s between polls so Claude doesn't fire back-to-back

        if success:
            result = {
                "success": success,
                "still_running": still_running,
                "status": status,
                "message": message,
                "output": output,
            }
        else:
            result = {
                "still_running": still_running,
                "status": status,
                "output": output,
                **build_failure(message),
            }
        if elapsed_seconds is not None:
            result["elapsed_seconds"] = elapsed_seconds
        if exit_code is not None and exit_code != 0:
            result["exit_code"] = exit_code
        if restart_warning is not None:
            result["restart_warning"] = restart_warning
        return result

    @staticmethod
    def _is_pid_running(pid_file):
        """
        True if the PID recorded in pid_file is still the *same* live, non-zombie
        process that migrate() launched — not a reused PID assigned to something
        unrelated after the real process exited.

        Used by migrate()'s "already in progress" guard and by migrate_status().
        pgrep -f string-matching against the command line is fragile here (the real
        argv, joined, is "bench --site <site> migrate --skip-failing" — "bench" and
        "migrate" are never contiguous, so a pattern like "bench migrate" never
        matches), so this checks the same fac_migrate.pid file migrate() already
        writes instead, comparing the process's create_time() against the one
        recorded at launch to rule out PID reuse.
        """
        parsed = _read_pid_file(pid_file)
        if parsed is None:
            return False
        pid, recorded_start_time = parsed

        try:
            proc = psutil.Process(pid)
            if proc.status() == psutil.STATUS_ZOMBIE:
                return False
            if recorded_start_time is not None and proc.create_time() != recorded_start_time:
                return False  # PID was reused by an unrelated process
            return True
        except psutil.NoSuchProcess:
            return False

    @staticmethod
    def _is_out_of_band_migrate_running(site_name):
        """
        Scans live processes for another 'bench ... migrate' invocation for this site
        that our own fac_migrate.pid tracking doesn't know about (e.g. a manual SSH
        session, cron, or a different tool). Used before deleting Frappe's own
        bench_migrate.lock file, so a real in-flight migrate's lock isn't ripped out
        from under it just because we don't have a pid file for it.
        """
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if "bench" in cmdline and "migrate" in cmdline and site_name in cmdline:
                return True
        return False

    @staticmethod
    def migrate(arguments):
        import subprocess
        import sys

        bench_path = frappe.utils.get_bench_path()
        site_name = frappe.local.site
        restart = arguments.get("restart", True)
        build_assets = arguments.get("build_assets", False)
        app_name = arguments.get("app_name")

        pid_file = os.path.join(bench_path, "logs", "fac_migrate.pid")
        already_in_progress = build_failure(
            "Migrate already in progress. Please wait for it to complete. "
            "Check running processes with: ps aux | grep migrate"
        )
        if MigrationService._is_pid_running(pid_file):
            return already_in_progress

        # Atomically claim pid_file to close the race between the liveness check
        # above and actually launching the subprocess below: without this, two
        # near-simultaneous migrate() calls could both pass the check before either
        # writes a new pid, launching two concurrent `bench migrate` processes
        # against the same site (real DB-migration corruption risk).
        #
        # The claim placeholder must be a valid, currently-alive "pid:starttime" —
        # not an unparseable literal like "starting". _read_pid_file() can't parse
        # that, so _is_pid_running() falsely reports "not running" for the whole
        # window between claiming the file and overwriting it with the real
        # subprocess's PID below, letting a concurrent call see a "stale" claim and
        # steal it (confirmed live in remove_app's copy of this exact pattern: two
        # near-simultaneous calls both proceeded and ran in parallel against the
        # same log/result files). Writing our own (parent request's) pid:starttime
        # instead keeps the file parseable and correctly "alive" for the entire
        # claim window.
        own_claim = f"{os.getpid()}:{psutil.Process(os.getpid()).create_time()}".encode()
        try:
            claim_fd = os.open(pid_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(claim_fd, own_claim)
            os.close(claim_fd)
        except FileExistsError:
            # Lost the race to a concurrent call, or a stale file from a run that
            # crashed with no live process (already ruled out by the liveness check
            # above). Safe to reclaim a stale file, but only once — if someone else
            # claims it in between, back off instead of clobbering their claim.
            if MigrationService._is_pid_running(pid_file):
                return already_in_progress
            os.remove(pid_file)
            try:
                claim_fd = os.open(pid_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(claim_fd, own_claim)
                os.close(claim_fd)
            except FileExistsError:
                return build_failure(
                    "Migrate already in progress (lost the race to start it). " "Please wait and try again."
                )

        lock_path = os.path.join(bench_path, "sites", site_name, "locks", "bench_migrate.lock")
        if os.path.exists(lock_path):
            if MigrationService._is_out_of_band_migrate_running(site_name):
                os.remove(pid_file)  # release our claim; we're not actually starting a migrate
                return build_failure(
                    f"A 'bench migrate' process for site '{site_name}' appears to already be "
                    f"running outside of this tool's tracking. Not deleting the lock file at "
                    f"{lock_path} or starting a new migrate — wait for it to finish."
                )
            os.remove(lock_path)

        import time

        purge_result = subprocess.run(
            ["bench", "--site", site_name, "purge-jobs"],
            capture_output=True,
            text=True,
            cwd=bench_path,
        )
        purge_jobs_warning = None
        if purge_result.returncode != 0:
            purge_jobs_warning = (
                f"'bench purge-jobs' exited with status {purge_result.returncode}: "
                f"{purge_result.stderr.strip()}. Proceeding with migrate anyway — stale/duplicate "
                f"queued jobs may still be present."
            )

        log_file = os.path.join(bench_path, "logs", "fac_migrate.log")
        exitcode_file = os.path.join(bench_path, "logs", "fac_migrate.exitcode")
        restarted_file = os.path.join(bench_path, "logs", "fac_migrate.restarted")
        restart_flag_file = os.path.join(bench_path, "logs", "fac_migrate.restart_flag")

        # Clear markers from any previous run so migrate_status can't mistake a
        # stale exit code / restart record for this run's outcome.
        for stale_file in (exitcode_file, restarted_file):
            try:
                os.remove(stale_file)
            except FileNotFoundError:
                pass

        # Persist the caller's restart choice for migrate_status() to honor later —
        # it runs in a separate HTTP request with no access to this function's locals.
        with open(restart_flag_file, "w") as rff:  # fmt: skip  # nosemgrep: frappe-security-file-traversal — writing restart flag to well-known bench logs directory
            rff.write("1" if restart else "0")

        # migrate_status() runs in a separate HTTP request with no live Popen handle,
        # so a file is the only way to hand it the real exit code. Wrap the migrate
        # command in a tiny Python subprocess (pure argv, no shell) that blocks on it
        # and writes the exit code to disk once it finishes. The wrapper's own PID
        # (written to fac_migrate.pid below) stays alive for its entire lifetime,
        # which includes writing the exit code file — so by the time migrate_status()
        # sees that PID as gone, the exit code file is guaranteed to already exist.
        wrapper_code = (
            "import subprocess, sys\n"
            "try:\n"
            "    code = subprocess.call(sys.argv[2:])\n"
            "except OSError:\n"
            "    code = 127\n"
            "with open(sys.argv[1], 'w') as ef:\n"
            "    ef.write(str(code))\n"
        )
        migrate_cmd = ["bench", "--site", site_name, "migrate", "--skip-failing"]

        with open(log_file, "w") as f:  # fmt: skip  # nosemgrep: frappe-security-file-traversal — writing to well-known bench logs directory
            process = subprocess.Popen(
                [sys.executable, "-c", wrapper_code, exitcode_file, *migrate_cmd],
                stdout=f,
                stderr=subprocess.STDOUT,
                cwd=bench_path,
                start_new_session=True,
            )

        # Record create_time alongside the PID so migrate_status()/_is_pid_running()
        # can detect PID reuse (the OS assigning this same PID to an unrelated
        # process after the real wrapper has already exited).
        try:
            start_time = psutil.Process(process.pid).create_time()
        except psutil.NoSuchProcess:
            start_time = 0.0  # process already gone; reuse-check degrades gracefully

        with open(pid_file, "w") as pf:  # fmt: skip  # nosemgrep: frappe-security-file-traversal — writing PID to well-known bench logs directory
            pf.write(f"{process.pid}:{start_time}")

        result = {
            "success": True,
            "background": True,
            "status": "running",
            "message": "Migrate started. Checking progress...",
        }
        if purge_jobs_warning is not None:
            result["purge_jobs_warning"] = purge_jobs_warning
        if build_assets:
            # There is no real asset-rebuild step wired up for this action — say so
            # instead of silently no-oping while implying it happened.
            result["build_assets_note"] = (
                "build_assets=True was requested, but this action does not rebuild "
                f"assets. Run 'bench build{f' --app {app_name}' if app_name else ''}' "
                "manually if you need JS/CSS rebuilt."
            )
        return result

# Copyright (C) 2025 Promantia
# Developer Tools Plugin — MigrationService

import os

import frappe

from frappe_assistant_core.plugins.developer_tools.guards import build_failure


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

        bench_path = frappe.utils.get_bench_path()
        log_file = os.path.join(bench_path, "logs", "fac_migrate.log")
        pid_file = os.path.join(bench_path, "logs", "fac_migrate.pid")
        exitcode_file = os.path.join(bench_path, "logs", "fac_migrate.exitcode")
        restarted_file = os.path.join(bench_path, "logs", "fac_migrate.restarted")

        pid_file_exists = os.path.exists(pid_file)

        still_running = False
        try:
            with open(pid_file) as f:  # fmt: skip  # nosemgrep: frappe-security-file-traversal — reading PID from well-known bench logs directory
                pid = int(f.read().strip())
            os.kill(pid, 0)  # signal 0 = existence check only, no actual signal sent
            # os.kill succeeds for zombie processes too — check /proc to rule out zombies
            try:
                with open(f"/proc/{pid}/status") as sf:  # fmt: skip  # nosemgrep: frappe-security-file-traversal — reading /proc pseudo-filesystem, not user files
                    for line in sf:
                        if line.startswith("State:"):
                            still_running = "Z" not in line  # Z = zombie = finished
                            break
                    else:
                        still_running = True  # State line not found — assume running
            except (FileNotFoundError, PermissionError):
                still_running = False  # /proc entry gone = process exited
        except (FileNotFoundError, ProcessLookupError, ValueError):
            still_running = False

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
        if still_running:
            status = "running"
            success = True
            message = "Migrate still in progress..."
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
                    subprocess.run(["bench", "restart"], capture_output=True, cwd=bench_path)
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
            import time

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
        if exit_code is not None and exit_code != 0:
            result["exit_code"] = exit_code
        return result

    @staticmethod
    def _is_pid_running(pid_file):
        """
        True if the PID recorded in pid_file is a live, non-zombie process.

        Used by migrate()'s "already in progress" guard. pgrep -f string-matching
        against the command line is fragile here (the real argv, joined, is
        "bench --site <site> migrate --skip-failing" — "bench" and "migrate" are
        never contiguous, so a pattern like "bench migrate" never matches), so this
        checks the same fac_migrate.pid file migrate() already writes instead.
        """
        try:
            with open(pid_file) as f:  # fmt: skip  # nosemgrep: frappe-security-file-traversal — reading PID from well-known bench logs directory
                pid = int(f.read().strip())
            os.kill(pid, 0)  # signal 0 = existence check only, no actual signal sent
            # os.kill succeeds for zombie processes too — check /proc to rule out zombies
            try:
                with open(f"/proc/{pid}/status") as sf:  # fmt: skip  # nosemgrep: frappe-security-file-traversal — reading /proc pseudo-filesystem, not user files
                    for line in sf:
                        if line.startswith("State:"):
                            return "Z" not in line  # Z = zombie = finished
                    return True  # State line not found — assume running
            except (FileNotFoundError, PermissionError):
                return False  # /proc entry gone = process exited
        except (FileNotFoundError, ProcessLookupError, ValueError):
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
        if MigrationService._is_pid_running(pid_file):
            return build_failure(
                "Migrate already in progress. Please wait for it to complete. "
                "Check running processes with: ps aux | grep migrate"
            )

        lock_path = os.path.join(bench_path, "sites", site_name, "locks", "bench_migrate.lock")
        if os.path.exists(lock_path):
            os.remove(lock_path)

        import time

        subprocess.run(
            ["bench", "--site", site_name, "purge-jobs"],
            capture_output=True,
            text=True,
            cwd=bench_path,
        )

        log_file = os.path.join(bench_path, "logs", "fac_migrate.log")
        exitcode_file = os.path.join(bench_path, "logs", "fac_migrate.exitcode")
        restarted_file = os.path.join(bench_path, "logs", "fac_migrate.restarted")

        # Clear markers from any previous run so migrate_status can't mistake a
        # stale exit code / restart record for this run's outcome.
        for stale_file in (exitcode_file, restarted_file):
            try:
                os.remove(stale_file)
            except FileNotFoundError:
                pass

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

        with open(pid_file, "w") as pf:  # fmt: skip  # nosemgrep: frappe-security-file-traversal — writing PID to well-known bench logs directory
            pf.write(str(process.pid))

        return {
            "success": True,
            "background": True,
            "status": "running",
            "message": "Migrate started. Checking progress...",
        }

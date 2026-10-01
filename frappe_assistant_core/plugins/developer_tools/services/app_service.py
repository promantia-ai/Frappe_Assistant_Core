# Copyright (C) 2025 Promantia
# Developer Tools Plugin — AppService

import os
import shutil
import sys
import threading
import time

import frappe
import frappe.installer
from frappe import _

from frappe_assistant_core.plugins.developer_tools.guards import (
    assert_app_dir_exists,
    assert_not_protected_app,
    assert_required,
    assert_valid_app_name,
    build_failure,
    get_app_path,
    get_apps_path,
)
from frappe_assistant_core.plugins.developer_tools.services.app_registry import AppRegistry


def _pending_removal_marker_path(app_name):
    """
    Path to the marker signaling app_name's background directory cleanup
    (scheduled by _execute_app_removal) hasn't finished yet. Lives under
    logs/, never under apps/, so creating/removing it never touches this
    bench's watched-path reloader.
    """
    bench_path = frappe.utils.get_bench_path()
    marker_dir = os.path.join(bench_path, "logs", "fac_remove_pending")
    return os.path.join(marker_dir, f"{app_name}.marker")


def _is_removal_pending(app_name):
    return os.path.exists(_pending_removal_marker_path(app_name))


def _execute_app_removal(site_name, app_name):
    """
    Performs the actual app removal: uninstall from the site, deregister from
    apps.txt/installed_apps, clear the Redis module-map cache, pip-uninstall the
    package, then schedule the app's directory to be deleted in the background.

    Runs inline within AppService.remove_app()'s own request, but the final
    directory delete is deliberately NOT awaited here: this bench's dev server
    auto-restarts on any file change under apps/ (watchdog-based reloader), and
    deleting the directory synchronously would risk dropping this request's own
    HTTP response mid-flight even though the removal already succeeded. Uninstall,
    deregister, cache clear, and pip-uninstall all happen before that point and
    are fully confirmed before we return — only the watched-path step is deferred.
    """
    app_path = get_app_path(app_name)

    # Uninstall from site first if installed. Fail safe: if this raises, the app
    # is left exactly as it was (still installed, still on disk) — never fall
    # through to deregistering or deleting the directory on a failed/uncertain
    # uninstall.
    installed_apps = frappe.get_installed_apps()
    if app_name in installed_apps:
        try:
            frappe.installer.remove_app(app_name, yes=True, no_backup=True)
        except Exception as e:
            return build_failure(
                f"App '{app_name}' was NOT removed: uninstalling it from the site failed "
                f"with: {e}. No files were deleted."
            )
    else:
        # Still clean from DB just in case
        new_list = []
        for app in installed_apps:
            if app != app_name:
                new_list.append(app)
        frappe.db.set_global("installed_apps", frappe.as_json(new_list))
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — required to persist app state before next DB read

    # Deregister from installed_apps/apps.txt BEFORE deleting the directory. If this
    # ran after the delete instead, any concurrent background job (e.g. the
    # scheduler) that still saw app_name as installed would try to import it in that
    # window and crash with ModuleNotFoundError, since the directory would already be gone.
    try:
        registry_changes = AppRegistry().deregister_app(app_name)
    except Exception as e:
        return build_failure(
            f"App '{app_name}' was NOT removed: deregistering it from apps.txt/installed_apps "
            f"failed with: {e}. No files were deleted."
        )

    # frappe.installer.remove_app() above already calls frappe.clear_cache(), but it does
    # so BEFORE apps.txt is updated (the deregister_app() call just above). Anything that
    # recomputes Frappe's Redis-cached module map (app_modules/installed_app_modules/
    # all_apps/app_hooks) in that gap — e.g. a scheduler tick — repopulates it from the
    # still-stale apps.txt, and nothing would clear it again afterwards. Clearing here,
    # now that apps.txt/installed_apps truly reflect app_name being gone, closes that gap.
    frappe.clear_cache()

    # Pip uninstall the package before deleting the directory. Ordering matters for two
    # reasons: (1) pip only touches env/site-packages, not apps/, so it can't trigger
    # the dev-server reload that shutil.rmtree() below can — doing it first keeps that
    # one risky step last, after everything else has already committed; (2) a failure
    # here can't be blamed on the directory already being gone. Best-effort: some pip
    # versions call sys.exit() internally on failure, which raises SystemExit (not an
    # Exception subclass), so it must be caught explicitly alongside Exception. Non-fatal
    # since the site/DB removal above already succeeded and shouldn't be undone, but the
    # failure reason is kept and surfaced rather than silently discarded.
    from pip._internal.cli.main import main as pip_main

    pip_uninstall_warning = None
    try:
        exit_code = pip_main(["uninstall", "-y", app_name])
        if exit_code != 0:
            pip_uninstall_warning = f"pip uninstall exited with status {exit_code}."
    except (Exception, SystemExit) as e:
        pip_uninstall_warning = str(e)

    marker_path = _pending_removal_marker_path(app_name)
    os.makedirs(os.path.dirname(marker_path), exist_ok=True)
    open(  # nosemgrep: frappe-security-file-traversal — path derived from validated app_name, not user input
        marker_path, "w"
    ).close()

    def _cleanup():
        # Runs on a background Timer thread with no initialized frappe.local —
        # must stick to plain filesystem calls only, no frappe.* here.
        staged_path = f"{app_path}.removing-{os.getpid()}-{int(time.time())}"
        try:
            os.rename(app_path, staged_path)
        except OSError:
            staged_path = app_path
        shutil.rmtree(staged_path, ignore_errors=True)
        try:
            os.remove(marker_path)
        except OSError:
            pass

    # Delayed so this request's response has a chance to flush before the
    # directory delete fires the reloader's watched-path restart. Reduces,
    # but can't fully guarantee against, that race — see module note above.
    threading.Timer(1.5, _cleanup).start()

    result = {
        "success": True,
        "app_name": app_name,
        "apps_txt_updated": registry_changes["apps_txt_updated"],
        "directory_cleanup": "in_progress",
        "message": (
            f"App '{app_name}' uninstalled, deregistered, and pip-uninstalled. "
            f"Its directory is being deleted in the background; this may cause a "
            f"brief dev-server restart in the next couple of seconds, which is "
            f"expected and not a failure."
        ),
    }
    if pip_uninstall_warning is not None:
        result["pip_uninstall_warning"] = (
            f"App directory removal was scheduled, but 'pip uninstall {app_name}' "
            f"failed with: {pip_uninstall_warning}. You may need to run "
            f"'pip uninstall {app_name}' manually to clean up its package metadata."
        )
    return result


class AppService:
    """
    Business logic for bench_execute's app-lifecycle actions.

    Each @staticmethod implements one action and takes the raw arguments
    dict passed to BenchExecute.execute(). bench_execute.py's execute()
    calls straight into these and returns their result directly — no
    try/except here or at the call site, so ValidationError/PermissionError
    propagate uncaught to base_tool.py's error handling.
    """

    @staticmethod
    def list_apps(arguments):
        apps_path = get_apps_path()

        try:
            all_entries = os.listdir(apps_path)
        except OSError as e:
            frappe.throw(
                _("Cannot list apps directory '{0}': {1}").format(apps_path, str(e)),
                frappe.ValidationError,
            )
        bench_apps = [
            entry
            for entry in all_entries
            if not entry.startswith(".") and os.path.isdir(os.path.join(apps_path, entry))
        ]

        all_installed = frappe.get_installed_apps()
        site_apps = list(all_installed)

        # Only auto-clean ghost entries for non-protected apps; never silently
        # remove protected app entries even if their directory appears missing.
        ghosts = AppRegistry().remove_ghost_apps(apps_path)
        if ghosts:
            site_apps = [app for app in site_apps if app not in ghosts]
            # remove_ghost_apps() just mutated the installed_apps DB global. Without
            # this, Frappe's Redis-cached module map (app_modules/all_apps/etc, shared
            # across every process) keeps listing the ghost app until something else
            # happens to clear it — the same stale-cache bug fixed in remove_app.
            frappe.clear_cache()

        return {
            "success": True,
            "bench_apps": bench_apps,
            "bench_apps_count": len(bench_apps),
            "site_apps": site_apps,
            "site_apps_count": len(site_apps),
            "message": "bench_apps are all apps on disk. site_apps are apps installed on current site.",
        }

    @staticmethod
    def install_app(arguments):
        import subprocess

        app_name = arguments.get("app_name")
        assert_required(app_name, _("app_name is required for install_app."))

        site_name = frappe.local.site
        bench_path = frappe.utils.get_bench_path()
        frappe.installer.install_app(app_name)

        pip_install_result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", f"apps/{app_name}", "--no-deps"],
            capture_output=True,
            text=True,
            cwd=bench_path,
        )

        verify_result = subprocess.run(
            [sys.executable, "-c", f"import {app_name}; print('ok')"],
            capture_output=True,
            text=True,
            cwd=bench_path,
        )
        if verify_result.returncode != 0:
            return build_failure(
                "App installed but import failed. hooks.py may be missing.",
                app_name=app_name,
                site_name=site_name,
            )

        result = {
            "success": True,
            "app_name": app_name,
            "site_name": site_name,
            "message": f"App '{app_name}' installed on site '{site_name}' successfully.",
        }
        # The import above can still succeed against a stale-but-working prior install
        # even when this pip step just failed (e.g. a new dependency/build-backend
        # issue), so a passing import check alone doesn't prove pip succeeded — surface
        # it instead of letting it go unnoticed.
        if pip_install_result.returncode != 0:
            result["pip_install_warning"] = (
                f"App imports successfully, but 'pip install -e apps/{app_name}' failed with: "
                f"{pip_install_result.stderr.strip()}. Its editable package metadata may be stale."
            )
        return result

    @staticmethod
    def uninstall_app(arguments):
        app_name = arguments.get("app_name")
        assert_required(app_name, _("app_name is required for uninstall_app."))

        installed = frappe.get_installed_apps()
        if app_name not in installed:
            return {
                "success": True,
                "already_uninstalled": True,
                "app_name": app_name,
                "message": f"App '{app_name}' is already not installed on the current site.",
            }

        try:
            frappe.installer.remove_app(app_name, yes=True, no_backup=True)
        except Exception as e:
            return build_failure(f"App '{app_name}' was NOT uninstalled: {e}.")

        # frappe.installer.remove_app() silently no-ops (no exception, no DB change) if
        # app_name is a required_apps dependency of another installed app. Verify it
        # actually left installed_apps instead of trusting the call succeeded just
        # because it didn't raise.
        if app_name in frappe.get_installed_apps():
            return build_failure(
                f"App '{app_name}' was NOT uninstalled: it is likely still required by another "
                f"installed app (Frappe blocks uninstalling a dependency without raising an error)."
            )

        AppRegistry().remove_from_installed_apps(app_name)

        return {
            "success": True,
            "app_name": app_name,
            "message": f"App '{app_name}' uninstalled from current site successfully.",
        }

    @staticmethod
    def create_app(arguments):
        app_name = arguments.get("app_name")
        assert_required(app_name, _("app_name is required for create_app."))

        assert_valid_app_name(app_name)

        assert_not_protected_app(
            app_name,
            _("Cannot create or overwrite protected app '{0}'.").format(app_name),
        )

        if _is_removal_pending(app_name):
            # A prior remove_app for this exact name may still be mid-rename/rmtree
            # in the background. Wait briefly for the common fast case rather than
            # immediately erroring or racing the isdir check below against it.
            for _attempt in range(15):  # ~3s total at 0.2s intervals
                if not _is_removal_pending(app_name):
                    break
                time.sleep(0.2)
            else:
                return build_failure(
                    f"App '{app_name}' removal is still finishing in the background. "
                    f"Retry create_app in a moment.",
                    removal_in_progress=True,
                )

        apps_path = get_apps_path()
        app_path = get_app_path(app_name)

        if os.path.isdir(app_path):
            return {
                "success": True,
                "already_existed": True,
                "installed": False,
                "apps_txt_updated": False,
                "app_name": app_name,
                "message": f"App '{app_name}' already exists at {app_path}.",
            }

        app_title = arguments.get("app_title") or app_name.replace("_", " ").title()
        app_description = arguments.get("app_description") or ""

        hooks = frappe._dict(
            app_name=app_name,
            app_title=app_title,
            app_description=app_description,
            app_publisher="Promantia",
            app_email="dev@promantia.com",
            app_license="mit",
            create_github_workflow=False,
        )

        from frappe.utils.boilerplate import _create_app_boilerplate

        _create_app_boilerplate(apps_path, hooks, no_git=True)

        outer_init = os.path.join(apps_path, app_name, "__init__.py")
        if not os.path.exists(outer_init):
            open(  # nosemgrep: frappe-security-file-traversal — path validated by resolve_and_validate_path()
                outer_init, "w"
            ).close()

        pyproject = os.path.join(apps_path, app_name, "pyproject.toml")
        if not os.path.exists(pyproject):
            with open(  # nosemgrep: frappe-security-file-traversal — path validated by resolve_and_validate_path()
                pyproject, "w"
            ) as f:
                f.write(f"""[project]
name = "{app_name}"
version = "0.0.1"

[build-system]
requires = ["flit_core >=3.2,<4"]
build-backend = "flit_core.buildapi"
""")

        from pip._internal.cli.main import main as pip_main

        # Best-effort: some pip versions call sys.exit() internally on failure, which
        # raises SystemExit (not an Exception subclass), so it must be caught explicitly
        # alongside Exception, and a non-zero return code is a failure with no exception
        # at all. Non-fatal since the directory/apps.txt changes below shouldn't be
        # undone, but the failure reason is kept and surfaced rather than discarded.
        pip_install_warning = None
        try:
            exit_code = pip_main(["install", "--quiet", "-e", os.path.join(apps_path, app_name)])
            if exit_code != 0:
                pip_install_warning = f"pip install exited with status {exit_code}."
        except (Exception, SystemExit) as e:
            pip_install_warning = str(e)

        if app_path not in sys.path:
            sys.path.insert(0, app_path)

        apps_txt_updated = AppRegistry().add_to_apps_txt(app_name)

        result = {
            "success": True,
            "already_existed": False,
            "installed": False,
            "apps_txt_updated": apps_txt_updated,
            "app_name": app_name,
            "app_title": app_title,
            "message": f"App '{app_name}' created successfully. Use install_app to install it on the site.",
        }
        if pip_install_warning is not None:
            result["pip_install_warning"] = (
                f"App files were created and added to apps.txt, but 'pip install -e' failed with: "
                f"{pip_install_warning}. It may only be importable in this process until "
                f"'pip install -e apps/{app_name}' is run manually."
            )
        return result

    @staticmethod
    def remove_app(arguments):
        app_name = arguments.get("app_name")
        assert_required(app_name, _("app_name is required for remove_app."))

        assert_valid_app_name(app_name)

        assert_not_protected_app(
            app_name,
            _("Cannot remove protected app '{0}'.").format(app_name),
        )

        if _is_removal_pending(app_name):
            return build_failure(
                f"Removal of '{app_name}' is still finishing in the background. "
                f"Wait a few seconds and retry.",
                removal_in_progress=True,
            )

        app_path = get_app_path(app_name)

        assert_app_dir_exists(
            app_path,
            _("App '{0}' directory does not exist at {1}.").format(app_name, app_path),
        )

        if not arguments.get("confirm"):
            return build_failure(
                f"This will permanently delete '{app_name}' and all its files. "
                f"Call remove_app again with confirm=true to proceed.",
                requires_confirmation=True,
            )

        # Confirms uninstall/deregister/pip-uninstall synchronously and returns;
        # the directory delete itself is scheduled in the background — see
        # _execute_app_removal()'s docstring.
        return _execute_app_removal(frappe.local.site, app_name)

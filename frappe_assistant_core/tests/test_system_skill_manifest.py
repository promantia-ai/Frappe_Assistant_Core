# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Tests for the system skill and prompt template manifests.

`_install_system_skills` skips a manifest entry whose content file is missing
and only logs it, so a typo in `content_file` ships a skill that never
installs. These tests fail on that instead, and pin that retired system
entries are removed from sites that already have them.
"""

import json
import os
import re

import frappe

from frappe_assistant_core.tests.base_test import BaseAssistantTest
from frappe_assistant_core.utils.migration_hooks import (
    _install_system_prompt_templates,
    _install_system_skills,
)

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(APP_DIR)

SETUP_SKILLS = (
    "site-health-check",
    "setup-change-protocol",
    "guided-setup",
    "add-custom-field",
    "set-up-approval-workflow",
    "configure-naming-series",
)
RETIRED_TEMPLATE = "export_fac_skills"

MIGRATE_DATA = "migrate-data"
# The words users start a migration with (#34004): the description is what makes FACO load it.
MIGRATE_DATA_TRIGGERS = ("migrate", "import my", "Tally")
MIGRATE_DATA_STEPS = (
    "Session",
    "Mapping",
    "Cleaning",
    "Dry run",
    "Fixes",
    "Import",
    "Failed rows",
    "Report",
)
# Code names the page uses that are arguments or result keys, not tools. ask_user is a
# FAC Cloud tool, so it has no file on the site.
NOT_TOOLS = {
    "ask_user",
    "closest",
    "documents",
    "failed_rows_file",
    "fixes",
    "google_sheets_url",
    "import_session",
    "session_id",
    "status",
}


def _load_manifest(name: str) -> list:
    with open(os.path.join(APP_DIR, "data", name), encoding="utf-8") as f:
        return json.load(f)


def _skill_content(content_file: str) -> str:
    with open(os.path.join(REPO_DIR, "docs", "skills", content_file), encoding="utf-8") as f:
        return f.read()


def _tool_files() -> set:
    return {
        os.path.splitext(name)[0]
        for plugin in os.listdir(os.path.join(APP_DIR, "plugins"))
        if os.path.isdir(os.path.join(APP_DIR, "plugins", plugin, "tools"))
        for name in os.listdir(os.path.join(APP_DIR, "plugins", plugin, "tools"))
        if name.endswith(".py")
    }


class TestSystemSkillManifest(BaseAssistantTest):
    def test_every_entry_has_its_content_file(self):
        for entry in _load_manifest("system_skills.json"):
            path = os.path.join(REPO_DIR, "docs", "skills", entry["content_file"])
            self.assertTrue(os.path.isfile(path), f"{entry['skill_id']}: {path} missing")
            with open(path, encoding="utf-8") as f:
                self.assertTrue(f.read().strip(), f"{entry['skill_id']}: content is empty")

    def test_skill_ids_are_unique_and_well_formed(self):
        ids = [entry["skill_id"] for entry in _load_manifest("system_skills.json")]
        self.assertEqual(len(ids), len(set(ids)))
        for skill_id in ids:
            self.assertRegex(skill_id, re.compile(r"^[a-z0-9_-]+$"))

    def test_setup_skills_install_as_published_system_skills(self):
        _install_system_skills()

        for skill_id in SETUP_SKILLS:
            skill = frappe.db.get_value(
                "FAC Skill",
                {"skill_id": skill_id},
                ["is_system", "status", "skill_type", "content"],
                as_dict=True,
            )
            self.assertIsNotNone(skill, f"{skill_id} was not installed")
            self.assertEqual(skill.is_system, 1)
            self.assertEqual(skill.status, "Published")
            self.assertEqual(skill.skill_type, "Workflow")
            self.assertTrue(skill.content.startswith("# "))

    def test_migrate_data_installs_as_a_published_workflow(self):
        _install_system_skills()

        skill = frappe.db.get_value(
            "FAC Skill",
            {"skill_id": MIGRATE_DATA},
            ["is_system", "status", "skill_type", "content"],
            as_dict=True,
        )
        self.assertIsNotNone(skill, f"{MIGRATE_DATA} was not installed")
        self.assertEqual(skill.is_system, 1)
        self.assertEqual(skill.status, "Published")
        self.assertEqual(skill.skill_type, "Workflow")
        self.assertTrue(skill.content.startswith("# Migrate Data"))

    def test_migrate_data_description_has_the_users_words(self):
        entry = next(e for e in _load_manifest("system_skills.json") if e["skill_id"] == MIGRATE_DATA)
        for words in MIGRATE_DATA_TRIGGERS:
            self.assertIn(words.lower(), entry["description"].lower())

    def test_migrate_data_lists_the_steps_in_order(self):
        content = _skill_content("migrate_data.md")
        positions = [content.find(f"**{step}**") for step in MIGRATE_DATA_STEPS]
        self.assertNotIn(-1, positions, dict(zip(MIGRATE_DATA_STEPS, positions)))
        self.assertEqual(positions, sorted(positions))

    def test_migrate_data_cleaning_checks_the_file_before_its_fixes(self):
        # A fix names a group of the last check, so a first call with fixes finds no group:
        # the Cleaning step has to say to check the file first.
        row = next(line for line in _skill_content("migrate_data.md").splitlines() if "**Cleaning**" in line)
        tool = row.split("|")[3]
        self.assertIn("`dry_run_import`", tool)
        self.assertIn("`fixes`", tool)
        self.assertLess(tool.find("`dry_run_import`"), tool.find("then"), row)
        self.assertLess(tool.find("then"), tool.find("`fixes`"), row)

    def test_migrate_data_names_only_tools_and_skills_that_exist(self):
        # The page is the model's guide to the tools: a renamed or removed tool left in it
        # sends FACO to a tool that isn't there.
        content = _skill_content("migrate_data.md")
        tools = _tool_files()
        for name in set(re.findall(r"`([a-z][a-z0-9_]*)`", content)) - NOT_TOOLS:
            self.assertIn(name, tools, f"migrate_data.md names `{name}`, which is not a tool")

        skill_ids = {e["skill_id"] for e in _load_manifest("system_skills.json")}
        for skill_id in re.findall(r'get_skill\("([a-z0-9_-]+)"\)', content):
            self.assertIn(skill_id, skill_ids)

    def test_retired_export_template_is_removed(self):
        self.assertNotIn(
            RETIRED_TEMPLATE,
            {t.get("prompt_id") for t in _load_manifest("system_prompt_templates.json")},
        )
        if not frappe.db.exists("Prompt Template", {"prompt_id": RETIRED_TEMPLATE}):
            frappe.get_doc(
                {
                    "doctype": "Prompt Template",
                    "prompt_id": RETIRED_TEMPLATE,
                    "title": "Export FAC Skills as Claude Skills",
                    "status": "Published",
                    "description": "Retired system template.",
                    "template_content": "Export the skills.",
                    "is_system": 1,
                }
            ).insert(ignore_permissions=True)

        _install_system_prompt_templates()

        self.assertFalse(frappe.db.exists("Prompt Template", {"prompt_id": RETIRED_TEMPLATE}))

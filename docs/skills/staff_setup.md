# Staff Setup

## Overview

Turn the client's staff list into users with the right roles and restrictions, so access is right on go-live day. You read the sheet, propose **groups** (one role profile per kind of work, with its roles and the user permissions its work implies), the consultant adjusts them in chat, and they become the **Users and roles** area of the setup plan: `run_setup_plan` creates everything with one approval card. Afterwards, `test_as_user` answers "what can this person see and do?".

## When to use

- The consultant attaches or describes a staff list: "set up these users", "create users from this sheet".
- "What can Ravi see and do?", "what access does the storekeeper have?": `test_as_user` with only `user` (see that skill).

## Workflow

1. **Read the sheet.** `extract_file_content` with `operation: "parse_data"` on the attached file. If it
   came with files to import and was opened as an import session, that tool can't read it: use
   `setup_plan(action="discover", staff_session=...)`, which gives every row as `staff_list` and closes
   the session. You need each person's name, email, department and what they do. Rows without an email can't become users: list them and ask.
2. **Propose the groups.** Group people by what they do, not by title. For each group: a role profile name ("Sales Executive"), its roles, and its user permissions. Check what exists first: `list_documents` on `Role` (`disabled = 0`, `desk_access = 1`) and on `Role Profile`. Use only roles that exist. Show one table:

   | Group (role profile) | Roles | Limited to | People |
   |---|---|---|---|
   | Sales Executive | Sales User, Stock User | Company: ABC Ltd; Territory: South | Ravi, Meena (2) |

   - **Roles from the work:** sells → Sales User; approves sales → Sales Manager; buys → Purchase User; stock and dispatch → Stock User; accounts → Accounts User; a manager of an area → that area's Manager role.
   - **Limits from the work:** a person who works for one company, warehouse, territory or branch gets a User Permission for it. Name records that exist (`list_documents` on Company, Warehouse, Territory, Branch); ask when the sheet doesn't say.
3. **Adjust in chat.** When the consultant changes a group ("sales also needs Stock User"), change the group: it applies to everyone in it. Show the table again.
4. **Create.** When the consultant approves, put the groups in the setup plan: `setup_plan(action="save", company, answers={"staff": [groups], "send_welcome_email": true only when the consultant asked})`, or with other areas of the setup. A changed group later: `setup_plan(action="update", items={n: {values: {group}}})`. Then `run_setup_plan` with the plan's `approval`: one approval card for the whole plan.
5. **Report** in plain words: who was created, which role profiles were made, how many restrictions, who was skipped because they are users already. Say that FAC Chat access was not given: seats are billed and added in FAC's user settings.
6. **Check** one person per group with `test_as_user` (only `user`), and tell the consultant in plain words what they can do.

## What the plan refuses

- **System Manager, Administrator and the Assistant roles**: admin rights and FAC access are given by hand. Leave them out of the plan and tell the consultant.
- An existing role profile with different roles: use its roles, or another name.
- A role, record or email that doesn't exist or isn't valid. Saving the plan lists every problem of a group in `problems` and leaves that group out: fix it and add it again (`setup_plan(action="update", add=[{area: "staff", group}])`). When the plan runs, each group is created all or nothing.

## Gotchas

- **Users are created only by `run_setup_plan`**, which always asks for approval on its own card (never "don't ask again"): creating users is a security-relevant write.
- Users who already exist are not changed. To change one, do it in Desk or with `update_document` after asking.
- A User Permission on Company limits every DocType with a Company field. Don't add one for a person who works across companies.

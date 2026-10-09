# Access sync

One Google Group on sampura.org is the team roster. Adding someone to the
group is onboarding; removing them is offboarding. `Code.gs` reads the group
every few minutes and pushes the member list to the platform, which stores it
in its `access_roster` table and checks it on every admin request.

Group role maps to access level: Owner or Manager is admin, Member is member.
Nested groups, used for volunteer cohorts such as `spar-2026@`, are walked;
their people are always members.

`Code.gs` is the reference copy. The live copy is an Apps Script project owned
by a group Owner (currently sander@sampura.org). Edit here, then paste into
the project, so the repo stays the source for review and recovery.

## Setup, once

1. Generate a secret, for example `openssl rand -hex 32`. Set it as
   `ACCESS_SYNC_SECRET` on the backend (Render env for production,
   `backend/.env` locally).
2. Sign in as the group Owner and open https://script.google.com. New
   project, paste `Code.gs`, save.
3. Project Settings, Script properties:
   - `GROUP_EMAIL`: the group's address.
   - `PLATFORM_API_URL`: the API origin, e.g.
     `https://api.platform.complementarities.org`.
   - `ACCESS_SYNC_SECRET`: the same secret as step 1.
4. Run `syncPlatform` from the editor once. Approve the permission prompt.
   The log shows how many people were pushed; the platform's Team page
   shows them.
5. Triggers, add trigger: `syncPlatform`, time-driven, every 5 minutes.

## Onboarding and offboarding

- **Staff:** add their sampura.org address to the group, as Member, or as
  Manager if they should administer the platform.
- **Volunteer cohort:** create a cohort group with external members allowed,
  add it to the main group as a Member, add the volunteers to the cohort
  group. Remove the cohort group at the end of the programme.
- **Removal:** remove the person or cohort from the group. The next sync
  drops them from the roster and their next platform request is refused.

Nobody edits the platform's roster directly; it is overwritten every sync.

## Failure handling

Apps Script emails the project owner when a triggered run throws. The script
refuses to push an empty group, and the backend refuses an empty push, so a
misconfigured group cannot remove everyone. A wrong secret or an unreachable
API leaves the previous roster in place; access goes stale, never open.
`ADMIN_ALLOWLIST` on the backend is a break-glass list for exactly that
case and is otherwise empty.

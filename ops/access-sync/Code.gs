// Access sync: one Google Group is the team roster. Every few minutes this
// script reads the group and pushes the member list to the platform, which
// keeps it in its access_roster table and checks it on every admin request.
//
// Group role -> access level:  Owner or Manager = admin, Member = member.
// Nested groups (volunteer cohorts) are walked; their people are always
// members, never admins, whatever their role inside the cohort group.
//
// This file is the reference copy. The live copy is an Apps Script project
// owned by a group Owner (sander@sampura.org) with a time-driven trigger on
// syncPlatform. See README.md in this folder for setup.
//
// Script properties:
//   GROUP_EMAIL         the roster group, e.g. team@sampura.org
//   PLATFORM_API_URL    e.g. https://api.platform.complementarities.org
//   ACCESS_SYNC_SECRET  same value as the backend's ACCESS_SYNC_SECRET

const props = PropertiesService.getScriptProperties();

function syncPlatform() {
  const roster = readGroup();
  const members = Object.entries(roster).map(([email, role]) => ({ email, role }));
  const res = UrlFetchApp.fetch(`${props.getProperty('PLATFORM_API_URL')}/api/admin/access-roster`, {
    method: 'put',
    contentType: 'application/json',
    headers: { Authorization: `Bearer ${props.getProperty('ACCESS_SYNC_SECRET')}` },
    payload: JSON.stringify({ members }),
    muteHttpExceptions: true,
  });
  if (res.getResponseCode() >= 400) {
    throw new Error(`Platform refused the roster: ${res.getResponseCode()} ${res.getContentText()}`);
  }
  Logger.log(`Pushed ${members.length} people: ${res.getContentText()}`);
}

/** @returns {Object<string, 'admin'|'member'>} lower-cased email -> level */
function readGroup() {
  const roster = {};
  collect(GroupsApp.getGroupByEmail(props.getProperty('GROUP_EMAIL')), roster, true);
  if (Object.keys(roster).length === 0) {
    throw new Error('Group is empty; refusing to push an empty roster');
  }
  return roster;
}

function collect(group, roster, direct) {
  group.getUsers().forEach((user) => {
    const email = user.getEmail().toLowerCase();
    let level = 'member';
    if (direct) {
      const role = group.getRole(user);
      if (role === GroupsApp.Role.OWNER || role === GroupsApp.Role.MANAGER) level = 'admin';
    }
    if (roster[email] !== 'admin') roster[email] = level;
  });
  group.getGroups().forEach((nested) => collect(nested, roster, false));
}

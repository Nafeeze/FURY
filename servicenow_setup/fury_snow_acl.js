/**
 * FURY ServiceNow access rule: let the FURY integration role read INCIDENT WORK NOTES.
 *
 * WHERE:  All > System Definition > Scripts - Background   (scope: Global)  -> paste this file -> Run script.  (admin only)
 *
 * WHY:    FURY writes every investigation step as a work note, but the "itil" role cannot read them back through the
 *         API (sys_journal_field returns no rows), so the FURY UI and chat cannot show an incident's notes.
 *
 * WHAT IT DOES (idempotent): creates ONE read ACL
 *            table      sys_journal_field
 *            operation  read
 *            condition  name = incident  AND  element = work_notes
 *            role       itil
 *         It does not widen anything else: no write access, no other tables, no other journal fields (comments and
 *         other tables stay as they are), no admin role.
 *
 * SAFETY: DRY_RUN = true only reports. Set it to false to apply. To undo: delete the ACL whose description starts with
 *         "FURY: read incident work notes" (System Security > Access Control (ACL)).
 * After applying, re-run:  python check_all.py --only snow   and open an incident in the FURY UI: its notes should appear.
 */
(function () {
    var DRY_RUN = false;
    var TABLE = 'sys_journal_field';
    var MARK = 'FURY: read incident work notes';
    var ROLE = 'itil';

    if (!gs.hasRole('admin')) { gs.print('STOP: run this as a user with the admin role.'); return; }

    // 1. show what already governs this table, so the result is never a surprise
    var existing = new GlideRecord('sys_security_acl');
    existing.addQuery('name', 'STARTSWITH', TABLE);
    existing.addQuery('operation', 'read');
    existing.query();
    var mine = null;
    gs.print('Existing read ACLs on ' + TABLE + ':');
    while (existing.next()) {
        gs.print('  - ' + existing.getValue('name') + '  active=' + existing.getValue('active') + '  ' + (existing.getValue('description') || ''));
        if ((existing.getValue('description') || '').indexOf(MARK) === 0) mine = existing.getUniqueValue();
    }

    var role = new GlideRecord('sys_user_role');
    role.addQuery('name', ROLE); role.setLimit(1); role.query();
    if (!role.next()) { gs.print('PROBLEM: role "' + ROLE + '" not found'); return; }

    if (mine) { gs.print('The FURY ACL already exists (' + mine + '). Nothing to do.'); return; }
    if (DRY_RUN) { gs.print('[dry-run] Would create a read ACL on ' + TABLE + ' for role ' + ROLE + ' limited to name=incident, element=work_notes.'); return; }

    var acl = new GlideRecord('sys_security_acl');
    acl.initialize();
    acl.setValue('name', TABLE);
    acl.setValue('operation', 'read');
    acl.setValue('type', 'record');
    acl.setValue('active', true);
    acl.setValue('admin_overrides', true);
    acl.setValue('condition', 'name=incident^element=work_notes');
    acl.setValue('description', MARK + ' (itil). Lets the FURY integration account show the notes FURY writes. Created by fury_snow_acl.js.');
    var aclId = acl.insert();
    if (!aclId) { gs.print('PROBLEM: could not create the ACL'); return; }

    var link = new GlideRecord('sys_security_acl_role');
    link.initialize();
    link.setValue('sys_security_acl', aclId);
    link.setValue('sys_user_role', role.getUniqueValue());
    if (!link.insert()) { gs.print('PROBLEM: ACL created (' + aclId + ') but the role could not be attached. Add role itil to it by hand.'); return; }
    gs.print('Created ACL ' + aclId + ' (read ' + TABLE + ' for ' + ROLE + ', incident work notes only).');
})();

/**
 * FURY ServiceNow setup (run ONCE per instance, as an admin).
 *
 * WHERE:  All > System Definition > Scripts - Background   (scope: Global)  -> paste this file -> Run script.
 *
 * WHAT IT CREATES (idempotent: safe to re-run; existing records are reused, never duplicated):
 *   1. Group  "SRE-Platform L2"            : the team FURY assigns escalated incidents to.
 *   2. User   "fury_integration"           : service account for FURY's API calls.
 *                                            - Web service access only (cannot log in to the UI)
 *                                            - no password reset prompt, not locked out, active
 *                                            - role: itil  (create / update / resolve incidents, add work notes)
 *   3. Optional: members for the L2 group (people who should see escalated tickets), see L2_MEMBERS below.
 *   4. A report of the Resolution code choices on your instance, so you can set SNOW_CLOSE_CODE in FURY's .env.
 *
 * SAFETY:
 *   - DRY_RUN is true by default: the script only REPORTS what it would do. Run it once, read the output,
 *     then set DRY_RUN = false and run it again.
 *   - The service account password is read from SERVICE_PASSWORD below. Do NOT commit a real password to git:
 *     paste it in the background-script window only, run, and close the window. The password is never printed.
 *   - It never grants admin or any role other than the ones listed in SERVICE_ROLES.
 *
 * After running: put these in FURY's .env
 *     SNOW_URL=https://<your-instance>.service-now.com
 *     SNOW_USER=fury_integration
 *     SNOW_PASSWORD=<the password you set below>
 *     SNOW_CLOSE_CODE=<one of the resolution codes printed at the end>
 *
 * Written in plain ES5 so it works on every ServiceNow release. Not run against a live instance by its author:
 * review the DRY_RUN output first, and verify with the curl command printed at the end.
 */
(function () {
    // ----------------------------------------------------------------------------- configuration
    var DRY_RUN = true;                          // <-- set to false to actually create/update records
    var SERVICE_USER = 'fury_integration';
    var SERVICE_PASSWORD = 'CHANGE-ME-before-running';   // <-- set a strong password (not committed anywhere)
    var SERVICE_ROLES = ['itil'];                // roles the integration user needs (nothing more)
    var GROUP_NAME = 'SRE-Platform L2';
    var GROUP_DESCRIPTION = 'Level-2 SRE team. FURY assigns escalated incidents here.';
    var L2_MEMBERS = [];                         // optional: user_name values of humans who work escalations, e.g. ['abel.tuter']
    var RESET_PASSWORD = false;                  // true = set SERVICE_PASSWORD even if the user already exists

    // ----------------------------------------------------------------------------- helpers
    var problems = [];
    var actions = 0;
    function say(msg) { gs.print((DRY_RUN ? '[dry-run] ' : '') + msg); }
    function plan(msg) { if (DRY_RUN) say(msg); }   // 'Would ...' lines only in a dry run
    function fail(msg) { problems.push(msg); gs.print('PROBLEM: ' + msg); }

    function findOne(table, field, value) {
        var gr = new GlideRecord(table);
        gr.addQuery(field, value);
        gr.setLimit(1);
        gr.query();
        return gr.next() ? gr : null;
    }

    // ----------------------------------------------------------------------------- pre-flight
    if (!gs.hasRole('admin')) {
        gs.print('STOP: run this as a user with the admin role.');
        return;
    }
    if (!DRY_RUN && (!SERVICE_PASSWORD || SERVICE_PASSWORD === 'CHANGE-ME-before-running' || SERVICE_PASSWORD.length < 12)) {
        gs.print('STOP: set SERVICE_PASSWORD to a strong password (12+ characters) before running with DRY_RUN = false.');
        return;
    }

    // ----------------------------------------------------------------------------- 1. group
    var group = findOne('sys_user_group', 'name', GROUP_NAME);
    var groupId = null;
    if (group) {
        groupId = group.getUniqueValue();
        say('Group "' + GROUP_NAME + '" already exists (' + groupId + ').' + (group.getValue('active') === '1' || group.getValue('active') === 'true' ? '' : ' It is INACTIVE: activate it.'));
    } else {
        plan('Would create group "' + GROUP_NAME + '".');
        if (!DRY_RUN) {
            var g = new GlideRecord('sys_user_group');
            g.initialize();
            g.setValue('name', GROUP_NAME);
            g.setValue('description', GROUP_DESCRIPTION);
            g.setValue('active', true);
            groupId = g.insert();
            if (groupId) { say('Created group "' + GROUP_NAME + '" (' + groupId + ').'); actions++; }
            else fail('could not create group "' + GROUP_NAME + '"');
        }
    }

    // ----------------------------------------------------------------------------- 2. service user
    var user = findOne('sys_user', 'user_name', SERVICE_USER);
    var userId = null;
    if (user) {
        userId = user.getUniqueValue();
        say('User "' + SERVICE_USER + '" already exists (' + userId + ').');
        var needsFix = [];
        if (user.getValue('web_service_access_only') !== '1' && user.getValue('web_service_access_only') !== 'true') needsFix.push('web_service_access_only');
        if (user.getValue('active') !== '1' && user.getValue('active') !== 'true') needsFix.push('active');
        if (user.getValue('locked_out') === '1' || user.getValue('locked_out') === 'true') needsFix.push('locked_out');
        if (user.getValue('password_needs_reset') === '1' || user.getValue('password_needs_reset') === 'true') needsFix.push('password_needs_reset');
        if (needsFix.length || RESET_PASSWORD) {
            plan('Would fix on existing user: ' + needsFix.join(', ') + (RESET_PASSWORD ? (needsFix.length ? ', ' : '') + 'password' : ''));
            if (!DRY_RUN) {
                user.setValue('web_service_access_only', true);
                user.setValue('active', true);
                user.setValue('locked_out', false);
                user.setValue('password_needs_reset', false);
                if (RESET_PASSWORD) user.user_password.setDisplayValue(SERVICE_PASSWORD);
                user.update();
                say('Updated user "' + SERVICE_USER + '": fixed ' + needsFix.join(', ') + (RESET_PASSWORD ? (needsFix.length ? ', ' : '') + 'password' : '') + '.');
                actions++;
            }
        }
    } else {
        plan('Would create user "' + SERVICE_USER + '" (web service access only, active, no password reset).');
        if (!DRY_RUN) {
            var u = new GlideRecord('sys_user');
            u.initialize();
            u.setValue('user_name', SERVICE_USER);
            u.setValue('first_name', 'FURY');
            u.setValue('last_name', 'Integration');
            u.setValue('active', true);
            u.setValue('web_service_access_only', true);
            u.setValue('locked_out', false);
            u.setValue('password_needs_reset', false);
            u.user_password.setDisplayValue(SERVICE_PASSWORD);   // stored hashed by the platform; never printed
            userId = u.insert();
            if (userId) { say('Created user "' + SERVICE_USER + '" (' + userId + ').'); actions++; }
            else fail('could not create user "' + SERVICE_USER + '"');
        }
    }

    // ----------------------------------------------------------------------------- 3. roles for the service user
    for (var i = 0; i < SERVICE_ROLES.length; i++) {
        var roleName = SERVICE_ROLES[i];
        var role = findOne('sys_user_role', 'name', roleName);
        if (!role) { fail('role "' + roleName + '" not found on this instance'); continue; }
        var roleId = role.getUniqueValue();
        var has = null;
        if (userId) {
            has = new GlideRecord('sys_user_has_role');
            has.addQuery('user', userId);
            has.addQuery('role', roleId);
            has.setLimit(1);
            has.query();
        }
        if (has && has.next()) {
            say('User "' + SERVICE_USER + '" already has role "' + roleName + '".');
        } else {
            plan('Would grant role "' + roleName + '" to "' + SERVICE_USER + '".');
            if (!DRY_RUN && userId) {
                var ur = new GlideRecord('sys_user_has_role');
                ur.initialize();
                ur.setValue('user', userId);
                ur.setValue('role', roleId);
                if (ur.insert()) { say('Granted role "' + roleName + '".'); actions++; }
                else fail('could not grant role "' + roleName + '"');
            }
        }
    }

    // ----------------------------------------------------------------------------- 4. L2 group members (optional)
    for (var m = 0; m < L2_MEMBERS.length; m++) {
        var member = findOne('sys_user', 'user_name', L2_MEMBERS[m]);
        if (!member) { fail('L2 member "' + L2_MEMBERS[m] + '" not found'); continue; }
        var already = null;
        if (groupId) {
            already = new GlideRecord('sys_user_grmember');
            already.addQuery('group', groupId);
            already.addQuery('user', member.getUniqueValue());
            already.setLimit(1);
            already.query();
        }
        if (already && already.next()) { say('"' + L2_MEMBERS[m] + '" is already in "' + GROUP_NAME + '".'); continue; }
        plan('Would add "' + L2_MEMBERS[m] + '" to "' + GROUP_NAME + '".');
        if (!DRY_RUN && groupId) {
            var gm = new GlideRecord('sys_user_grmember');
            gm.initialize();
            gm.setValue('group', groupId);
            gm.setValue('user', member.getUniqueValue());
            if (gm.insert()) { say('Added "' + L2_MEMBERS[m] + '" to the group.'); actions++; }
            else fail('could not add "' + L2_MEMBERS[m] + '" to the group');
        }
    }

    // ----------------------------------------------------------------------------- 5. resolution code choices (for SNOW_CLOSE_CODE)
    var choices = [];
    var ch = new GlideRecord('sys_choice');
    ch.addQuery('name', 'incident');
    ch.addQuery('element', 'close_code');
    ch.addQuery('inactive', false);
    ch.orderBy('sequence');
    ch.query();
    while (ch.next()) choices.push(ch.getValue('value'));
    gs.print('');
    if (choices.length) {
        gs.print('Resolution codes on this instance (pick one for SNOW_CLOSE_CODE in FURY .env):');
        for (var c = 0; c < choices.length; c++) gs.print('   ' + choices[c]);
    } else {
        gs.print('Could not read resolution codes. Open any incident > Resolution Information > Resolution code dropdown and copy a value.');
    }

    // ----------------------------------------------------------------------------- summary
    gs.print('');
    gs.print('==== SUMMARY ' + (DRY_RUN ? '(DRY RUN: nothing was changed)' : '(' + actions + ' change(s) made)') + ' ====');
    if (problems.length) {
        gs.print(problems.length + ' problem(s):');
        for (var p = 0; p < problems.length; p++) gs.print('  - ' + problems[p]);
    } else {
        gs.print('No problems found.');
    }
    if (DRY_RUN) {
        gs.print('Next: set DRY_RUN = false (and a real SERVICE_PASSWORD) and run again.');
    } else {
        gs.print('Verify from a terminal (replace host and password):');
        gs.print('  curl -s -u ' + SERVICE_USER + ':PASSWORD -H "Accept: application/json" "https://<instance>.service-now.com/api/now/table/incident?sysparm_limit=1"');
        gs.print('A JSON result (even empty) means API access and the itil role work. 401/403 means the user, password or role is wrong.');
    }
})();

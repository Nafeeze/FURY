/**
 * FURY ServiceNow: publish the three sample knowledge articles created by fury_snow_kb_seed.js (they start as drafts).
 * Run in Scripts - Background (scope Global) as admin. Touches ONLY articles whose short description matches the three seed titles.
 * This skips the KB approval workflow, which is fine for sample content in a developer instance; for real articles let your
 * knowledge managers publish through the normal workflow.
 */
(function () {
    var TITLES = ['Windows Server: recovering disk space safely', 'IIS: when to restart the W3SVC service', 'Reading the Windows System event log after a service failure'];
    if (!gs.hasRole('admin')) { gs.print('STOP: run this as a user with the admin role.'); return; }
    for (var i = 0; i < TITLES.length; i++) {
        var g = new GlideRecord('kb_knowledge');
        g.addQuery('short_description', TITLES[i]);
        g.query();
        if (!g.next()) { gs.print('not found: ' + TITLES[i]); continue; }
        if (g.getValue('workflow_state') !== 'published') {
            g.setValue('workflow_state', 'published');
            g.update();
        }
        var chk = new GlideRecord('kb_knowledge'); chk.get(g.getUniqueValue());
        gs.print(chk.getValue('number') + ' -> ' + chk.getValue('workflow_state') + '  (' + TITLES[i] + ')');
    }
})();

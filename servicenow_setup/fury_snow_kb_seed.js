/**
 * FURY ServiceNow: seed a few PUBLISHED knowledge articles for FURY to ingest (informational SRE guidance).
 *
 * WHERE:  All > System Definition > Scripts - Background   (scope: Global)  -> paste -> Run script.  (admin only)
 *
 * These are general-purpose guidance articles, NOT approved fix procedures: FURY uses them to answer "how / why" questions
 * and always labels them informational. Only runbooks (in the FURY repo, human-approved) can drive a change on the server.
 * Idempotent: an article with the same short description is skipped. Replace or extend them with your own real articles.
 * DRY_RUN = true only reports.
 */
(function () {
    var DRY_RUN = false;
    var ARTICLES = [
        { title: 'Windows Server: recovering disk space safely',
          html: '<p>When a data volume is nearly full, first find what is growing before deleting anything. Check the largest folders and the age of the files.</p>' +
                '<p>Temporary and cache folders are normally safe to clear once nothing is writing to them. Never delete application data, databases, or logs that are still required for audit.</p>' +
                '<p>After clearing space, confirm the volume is below 85 percent used and watch it for a day: a volume that fills again quickly points to a runaway writer such as a log without rotation.</p>' },
        { title: 'IIS: when to restart the W3SVC service',
          html: '<p>W3SVC is the Windows service that hosts IIS web sites. If a site returns 503 or does not respond, check whether the service is stopped before anything else.</p>' +
                '<p>Restarting W3SVC briefly drops active web connections, so do it when the service is stopped or unresponsive, not as a first reaction to slowness.</p>' +
                '<p>If the service stops again soon after a restart, look at the System and Application event logs for the cause before restarting repeatedly.</p>' },
        { title: 'Reading the Windows System event log after a service failure',
          html: '<p>Service Control Manager writes the key events. Event 7031 and 7034 mean a service terminated unexpectedly. Event 7036 records a service entering the running or stopped state.</p>' +
                '<p>Read the entries around the time of the failure, newest first. A burst of errors from the same provider usually shows the real cause; later entries are often consequences.</p>' +
                '<p>Record the event IDs and timestamps in the incident work notes so the next engineer does not have to repeat the search.</p>' }
    ];
    if (!gs.hasRole('admin')) { gs.print('STOP: run this as a user with the admin role.'); return; }

    var kb = new GlideRecord('kb_knowledge_base');
    kb.addQuery('active', true); kb.orderBy('title'); kb.query();
    var kbId = null, kbName = '';
    while (kb.next()) { if (!kbId || kb.getValue('title') === 'IT') { kbId = kb.getUniqueValue(); kbName = kb.getValue('title'); } }
    if (!kbId) { gs.print('PROBLEM: no active knowledge base found'); return; }
    gs.print('Using knowledge base "' + kbName + '" (' + kbId + ')');

    var made = 0;
    for (var i = 0; i < ARTICLES.length; i++) {
        var a = ARTICLES[i];
        var dup = new GlideRecord('kb_knowledge');
        dup.addQuery('short_description', a.title); dup.setLimit(1); dup.query();
        if (dup.next()) { gs.print('exists: ' + a.title + ' (' + dup.getValue('number') + ', ' + dup.getValue('workflow_state') + ')'); continue; }
        if (DRY_RUN) { gs.print('[dry-run] would create: ' + a.title); continue; }
        var g = new GlideRecord('kb_knowledge');
        g.initialize();
        g.setValue('short_description', a.title);
        g.setValue('text', a.html);
        g.setValue('kb_knowledge_base', kbId);
        g.setValue('article_type', 'text');
        g.setValue('workflow_state', 'published');
        var id = g.insert();
        if (id) { made++; var chk = new GlideRecord('kb_knowledge'); chk.get(id); gs.print('created ' + chk.getValue('number') + ' [' + chk.getValue('workflow_state') + ']: ' + a.title); }
        else gs.print('PROBLEM: could not create "' + a.title + '"');
    }
    gs.print('Done. ' + made + ' article(s) created. If one shows a state other than published, open it and publish it from the form.');
})();

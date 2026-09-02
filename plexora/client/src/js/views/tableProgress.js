/**
 * tableProgress.js -- the bar shown while a project's cell table is prepared.
 *
 * The counterpart to segmentationProgress.js, over the same panel
 * (views/jobProgress.js). What it watches is different in one important way:
 * the segmentation job runs on a background thread and is polled until it
 * finishes, whereas preparing a table happens INSIDE the save request. So this
 * polls alongside a promise rather than until a terminal status, and stops when
 * that promise settles -- whatever the server ends up saying.
 *
 * Keeping the work in the request is deliberate. The save is also what
 * validates the user's answer and puts the previous project back when the
 * answer turns out to be unreadable (tool_routes._reload_or_restore); moving it
 * to a job would have meant reporting that failure somewhere else, later.
 *
 * Loaded as a plain <script>, like the pages that use it, so it exports one
 * global rather than an ES module binding.
 */
window.PlexoraTableProgress = (function () {
    /** Matches data_model.TABLE_STAGES. */
    const STAGES = [
        { key: 'opening', label: 'Opening file' },
        { key: 'metadata', label: 'Reading metadata' },
        { key: 'preparing', label: 'Loading cells' },
        { key: 'loading', label: 'Indexing' },
        { key: 'finalizing', label: 'Finishing' },
    ];

    const POLL_MS = 500;

    /**
     * Show the panel until `work` settles, then take it down.
     *
     * @param {Object} options
     * @param {string} options.datasource  Project name to poll for.
     * @param {Promise} options.work       The save already in flight.
     * @param {Element} [options.mount]    Where to draw. Defaults to an overlay.
     * @param {string} [options.title]
     * @returns {Promise} whatever `work` resolves to; rejections pass through.
     */
    async function watch(options) {
        const panel = window.PlexoraJobProgress.create({
            title: options.title || 'Preparing this project’s cells',
            detail: 'Opening the data file',
            stages: STAGES,
            mount: options.mount,
        });

        let stopped = false;
        const poll = async () => {
            if (stopped) return;
            try {
                const response = await fetch(
                    plexoraUrl('get_table_status') + '?'
                    + new URLSearchParams({ datasource: options.datasource }));
                const status = await response.json();
                if (stopped) return;
                // "ready" before the work has settled means the server has not
                // reached its first stage yet -- the poll can beat it there --
                // so it is not an ending, only nothing to report.
                if (status && status.status === 'pending') {
                    panel.setProgress(
                        typeof status.progress === 'number' ? status.progress : null,
                        status.message || '', status.stage);
                }
            } catch (e) {
                // The save is the thing that reports failure; a poll that
                // cannot get through is not worth interrupting it for.
            }
            if (!stopped) window.setTimeout(poll, POLL_MS);
        };
        poll();

        try {
            return await options.work;
        } finally {
            stopped = true;
            panel.remove();
        }
    }

    return { watch: watch, STAGES: STAGES };
})();

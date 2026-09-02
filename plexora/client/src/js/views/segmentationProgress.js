/**
 * Shared step-2 wait for segmentation-mask generation.
 *
 * Both import flows start the mask job when their first form page is submitted
 * (see data_model.start_segmentation_job), so by the time the user finishes the
 * second page the conversion is usually well underway or already done. This
 * shows what is left of it rather than dropping the user into a viewer whose
 * cell layer would pop in some seconds later.
 *
 * The panel itself lives in jobProgress.js -- shared with the table-preparation
 * job, which needs exactly the same bar and rail. What stays here is the part
 * that is actually about segmentation: which endpoint to poll, which stages the
 * conversion goes through, and what to offer when it fails.
 *
 * Loaded as a plain <script>, like the pages that use it, so it exports one
 * global rather than an ES module binding.
 */

/** The conversion's phases, in order, matching data_model.SEGMENTATION_STAGES. */
const SEGMENTATION_STAGES = [
    { key: 'loading', label: 'Loading mask' },
    { key: 'inspecting', label: 'Inspecting' },
    { key: 'preparing', label: 'Reading' },
    { key: 'building', label: 'Building pyramid' },
    { key: 'writing', label: 'Writing' },
];

/**
 * Take over the page with a progress panel until the mask is ready, then go to
 * `redirectUrl`.
 *
 * @param {Object} options
 * @param {string} options.datasource   Dataset name to poll for.
 * @param {string} options.redirectUrl  Where to go once the mask is ready.
 * @param {number} [options.pollIntervalMs=1000]
 */
function awaitSegmentationThenOpen(options) {
    const datasource = options.datasource;
    const redirectUrl = options.redirectUrl;
    const pollIntervalMs = options.pollIntervalMs || 1000;

    const panel = window.PlexoraJobProgress.create({
        title: 'Preparing segmentation mask',
        detail: 'Converting segmentation mask',
        stages: SEGMENTATION_STAGES,
    });
    let stopped = false;

    function go() {
        stopped = true;
        // Deliberately NOT PlexoraRouter.go. Both callers reach here having just
        // changed what the project IS -- an import that created it, or an edit
        // that attached this very mask -- and a viewer that is still running was
        // built against the version before that. This is the reload the router
        // exists to leave intact.
        window.location = redirectUrl;
    }

    function poll() {
        if (stopped) return;
        fetch(plexoraUrl('get_segmentation_status') + '?' + new URLSearchParams({datasource: datasource}))
            .then(function (response) { return response.json(); })
            .then(function (status) {
                if (stopped) return;
                if (!status || status.status === 'ready') {
                    panel.setProgress(100, 'Segmentation mask ready', 'writing');
                    // Let the finished bar actually render before navigating,
                    // otherwise a fast conversion just looks like a flicker.
                    window.setTimeout(go, 350);
                    return;
                }
                if (status.status === 'error') {
                    panel.setError(status.error, {
                        // The rest of the import succeeded, so offer the viewer
                        // without a cell layer rather than leaving the user
                        // stuck on this panel.
                        label: 'Open viewer without segmentation',
                        onClick: go,
                    });
                    return;
                }
                panel.setProgress(
                    typeof status.progress === 'number' ? status.progress : null,
                    // The server's message says which kind of mask is being
                    // built and, when the supplied one could not be used
                    // as-is, which requirement it missed.
                    status.message || 'Converting segmentation mask',
                    status.stage
                );
                window.setTimeout(poll, pollIntervalMs);
            })
            .catch(function () {
                // A transient fetch failure shouldn't abandon a job that is
                // still running server-side -- keep polling.
                if (!stopped) window.setTimeout(poll, pollIntervalMs);
            });
    }

    poll();
}

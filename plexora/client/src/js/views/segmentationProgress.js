/**
 * Shared step-2 wait for segmentation-mask generation.
 *
 * Both import flows start the mask job when their first form page is submitted
 * (see data_model.start_segmentation_job), so by the time the user finishes the
 * second page the conversion is usually well underway or already done. This
 * shows what is left of it rather than dropping the user into a viewer whose
 * cell layer would pop in some seconds later.
 *
 * Loaded as a plain <script>, like the pages that use it, so it exports one
 * global rather than an ES module binding.
 */

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

    const panel = renderPanel();
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
                    panel.setProgress(100, 'Segmentation mask ready');
                    // Let the finished bar actually render before navigating,
                    // otherwise a fast conversion just looks like a flicker.
                    window.setTimeout(go, 350);
                    return;
                }
                if (status.status === 'error') {
                    panel.setError(status.error, go);
                    return;
                }
                panel.setProgress(
                    typeof status.progress === 'number' ? status.progress : null,
                    // The server's message says which kind of mask is being
                    // built and, when the supplied one could not be used
                    // as-is, which requirement it missed.
                    status.message || 'Converting segmentation mask'
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

function renderPanel() {
    const overlay = document.createElement('div');
    overlay.className = 'segmentation-progress-overlay';
    overlay.setAttribute('role', 'status');
    overlay.setAttribute('aria-live', 'polite');
    overlay.innerHTML = [
        '<div class="segmentation-progress-card">',
        '  <div class="segmentation-progress-title">Preparing segmentation mask</div>',
        '  <div class="segmentation-progress-detail">Converting segmentation mask</div>',
        '  <div class="segmentation-progress-track">',
        '    <div class="segmentation-progress-fill is-indeterminate"></div>',
        '  </div>',
        '  <div class="segmentation-progress-actions"></div>',
        '</div>',
    ].join('');
    document.body.appendChild(overlay);

    const detail = overlay.querySelector('.segmentation-progress-detail');
    const fill = overlay.querySelector('.segmentation-progress-fill');
    const title = overlay.querySelector('.segmentation-progress-title');
    const actions = overlay.querySelector('.segmentation-progress-actions');

    return {
        setProgress: function (percent, message) {
            detail.textContent = message;
            if (percent === null) {
                fill.classList.add('is-indeterminate');
                fill.style.width = '';
                return;
            }
            fill.classList.remove('is-indeterminate');
            fill.style.width = Math.max(0, Math.min(100, percent)) + '%';
        },
        setError: function (message, onSkip) {
            title.textContent = 'Segmentation mask failed';
            title.classList.add('has-error');
            detail.textContent = message || 'The mask could not be converted.';
            fill.classList.remove('is-indeterminate');
            fill.classList.add('has-error');
            fill.style.width = '100%';
            // The rest of the import succeeded, so offer the viewer without a
            // cell layer rather than leaving the user stuck on this panel.
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'btn btn-secondary';
            button.textContent = 'Open viewer without segmentation';
            button.addEventListener('click', onSkip);
            actions.appendChild(button);
        },
    };
}

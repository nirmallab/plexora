/**
 * jobProgress.js -- one progress panel, for every long job.
 *
 * Extracted from segmentationProgress.js rather than copied beside it. Two jobs
 * now need the same thing -- converting a segmentation mask, and preparing a
 * project's cell table -- and a second hand-rolled bar would be a second set of
 * class names, a second indeterminate rule and a second place to fix a bug.
 *
 * What this adds over the bar it came from is the STAGE RAIL. A percentage
 * alone cannot describe either job honestly: both spend minutes in phases with
 * nothing countable in them (reading a whole-slide mask plane, walking a
 * multi-image .h5ad's obs), and a bar that sits still for that long reads as a
 * hang. Naming the phase is what tells the two apart.
 *
 * Loaded as a plain <script>, like the pages that use it, so it exports one
 * global rather than an ES module binding.
 */
window.PlexoraJobProgress = (function () {
    /**
     * Build a panel.
     *
     * @param {Object} options
     * @param {string} options.title    Heading, e.g. "Preparing segmentation mask".
     * @param {string} [options.detail] Resting line under it.
     * @param {Array}  [options.stages] `[{key, label}]` in order; omit for no rail.
     * @param {Element} [options.mount] Where to put it. Defaults to a full-page
     *   overlay on document.body, which is what the import pages want; the
     *   requirements modal passes its own node instead.
     * @returns {{element, setProgress, setStage, setError, remove}}
     */
    function create(options) {
        const stages = options.stages || [];
        const root = document.createElement('div');
        root.className = options.mount
            ? 'job-progress' : 'segmentation-progress-overlay job-progress';
        root.setAttribute('role', 'status');
        root.setAttribute('aria-live', 'polite');
        root.innerHTML = [
            '<div class="segmentation-progress-card">',
            '  <div class="segmentation-progress-title"></div>',
            '  <div class="segmentation-progress-detail"></div>',
            '  <div class="segmentation-progress-track">',
            '    <div class="segmentation-progress-fill is-indeterminate"></div>',
            '  </div>',
            '  <ol class="connect-steps job-progress-stages" hidden></ol>',
            '  <div class="segmentation-progress-actions"></div>',
            '</div>',
        ].join('');
        (options.mount || document.body).appendChild(root);

        const title = root.querySelector('.segmentation-progress-title');
        const detail = root.querySelector('.segmentation-progress-detail');
        const fill = root.querySelector('.segmentation-progress-fill');
        const rail = root.querySelector('.job-progress-stages');
        const actions = root.querySelector('.segmentation-progress-actions');

        title.textContent = options.title || '';
        detail.textContent = options.detail || '';

        if (stages.length) {
            rail.hidden = false;
            stages.forEach((stage) => {
                const item = document.createElement('li');
                item.className = 'connect-step';
                item.dataset.stage = stage.key;
                item.innerHTML = '<span class="connect-step-mark"></span>'
                    + '<span class="connect-step-label"></span>';
                item.querySelector('.connect-step-label').textContent = stage.label;
                rail.appendChild(item);
            });
        }

        function setStage(key) {
            if (!stages.length) return;
            // Derived from the CURRENT stage only, never accumulated: a poll
            // that misses a stage (they can be milliseconds apart) must still
            // leave every earlier one marked done rather than skipped.
            const at = stages.findIndex((stage) => stage.key === key);
            rail.querySelectorAll('.connect-step').forEach((item, index) => {
                item.classList.toggle('is-done', at >= 0 && index < at);
                item.classList.toggle('is-active', at >= 0 && index === at);
            });
        }

        return {
            element: root,
            setProgress: function (percent, message, stage) {
                if (message) detail.textContent = message;
                if (stage) setStage(stage);
                if (percent === null || percent === undefined) {
                    fill.classList.add('is-indeterminate');
                    fill.style.width = '';
                    return;
                }
                fill.classList.remove('is-indeterminate');
                fill.style.width = Math.max(0, Math.min(100, percent)) + '%';
            },
            setStage: setStage,
            setError: function (message, action) {
                title.classList.add('has-error');
                detail.textContent = message || 'This job could not be completed.';
                fill.classList.remove('is-indeterminate');
                fill.classList.add('has-error');
                fill.style.width = '100%';
                rail.querySelectorAll('.connect-step.is-active')
                    .forEach((item) => item.classList.add('is-failed'));
                if (!action) return;
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'btn btn-secondary';
                button.textContent = action.label;
                button.addEventListener('click', action.onClick);
                actions.appendChild(button);
            },
            setTitle: function (text) { title.textContent = text; },
            remove: function () { root.remove(); },
        };
    }

    return { create: create };
})();

/**
 * columnClassifier.js -- which columns are markers, and which are metadata.
 *
 * Two boxes side by side. The server predicts the split (adapters/classify.py)
 * and the user corrects it by dragging; the metadata box additionally carries
 * the role slots, because a role always names a metadata column -- the cell id
 * and the coordinates are measurements *about* a cell, never markers measured
 * on one.
 *
 * Mounted by three callers -- the CSV import step, the requirements modal, and
 * the project edit page -- which is why it is a component rather than page
 * code. All three are asking the same question and must store the same answer.
 *
 * Uses window.Sortable (vendored via webpack's vendor bundle, loaded by
 * base.html on every page).
 */
window.PlexoraColumnClassifier = (function () {
    /**
     * @param mount        element to render into
     * @param options.markers    predicted marker columns, in table order
     * @param options.metadata   predicted metadata columns, in table order
     * @param options.roles      role -> column name, as already recorded
     * @param options.roleLabels role -> human label (from the server, so the
     *                           wording matches the requirements modal)
     * @param options.onChange   called after every edit
     * @returns { value(), destroy() }
     */
    function mount(mount, options = {}) {
        const roleLabels = options.roleLabels || {};
        const roleNames = Object.keys(roleLabels);
        const roles = { ...(options.roles || {}) };
        let markers = [...(options.markers || [])];
        let metadata = [...(options.metadata || [])];
        const sortables = [];

        mount.innerHTML = `
            <div class="column-classifier">
                <div class="column-box" data-group="metadata">
                    <div class="column-box-head">
                        <h2>Metadata</h2>
                        <span class="field-hint">Identifiers, coordinates, morphology, annotations.</span>
                    </div>
                    <div class="column-roles"></div>
                    <ul class="column-list" data-group="metadata"></ul>
                </div>
                <div class="column-box" data-group="markers">
                    <div class="column-box-head">
                        <h2>Markers</h2>
                        <span class="field-hint">Per-cell intensities that can be plotted or thresholded.</span>
                    </div>
                    <ul class="column-list" data-group="markers"></ul>
                </div>
            </div>
            <p class="column-classifier-hint">
                Drag a column between the boxes to correct it.
            </p>
        `;

        const lists = {
            metadata: mount.querySelector('.column-list[data-group="metadata"]'),
            markers: mount.querySelector('.column-list[data-group="markers"]'),
        };
        const rolesMount = mount.querySelector('.column-roles');

        function item(name) {
            const li = document.createElement('li');
            li.className = 'column-chip';
            li.dataset.column = name;
            li.textContent = name;
            return li;
        }

        function renderList(group, names) {
            lists[group].replaceChildren(...names.map(item));
        }

        function renderRoles() {
            // Rebuilt whenever the metadata box changes: a role can only name a
            // column that is in it, and dragging a column out has to clear any
            // role pointing at it rather than leave a dangling name.
            rolesMount.replaceChildren(...roleNames.map((role) => {
                const field = document.createElement('label');
                field.className = 'column-role';
                field.textContent = roleLabels[role];

                const select = document.createElement('select');
                select.className = 'form-select';
                select.dataset.role = role;
                select.append(new Option('—', ''));
                metadata.forEach((name) => {
                    select.append(new Option(name, name, false, roles[role] === name));
                });
                if (roles[role] && !metadata.includes(roles[role])) {
                    roles[role] = null;
                }
                select.addEventListener('change', () => {
                    roles[role] = select.value || null;
                    options.onChange?.();
                });
                field.appendChild(select);
                return field;
            }));
        }

        function readLists() {
            metadata = [...lists.metadata.children].map((li) => li.dataset.column);
            markers = [...lists.markers.children].map((li) => li.dataset.column);
            renderRoles();
            options.onChange?.();
        }

        renderList('metadata', metadata);
        renderList('markers', markers);
        renderRoles();

        // One shared group name is what makes the two lists exchange items;
        // without it each list only reorders within itself.
        Object.values(lists).forEach((list) => {
            sortables.push(new Sortable(list, {
                group: 'plexora-columns',
                animation: 150,
                ghostClass: 'column-chip-ghost',
                onSort: readLists,
            }));
        });

        return {
            value() {
                return { markers: [...markers], metadata: [...metadata], roles: { ...roles } };
            },
            destroy() {
                sortables.forEach((s) => s.destroy());
                mount.innerHTML = '';
            },
        };
    }

    return { mount };
})();

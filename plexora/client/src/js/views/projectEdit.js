/**
 * projectEdit.js -- editing an existing project.
 *
 * The page is generated from the project record, not from a fixed form: a
 * section renders only when `project.has` says it applies. A project with just
 * an image shows an image summary and two empty slots, not a wall of
 * coordinate pickers it has no columns for; an AnnData project shows its read
 * spec and never the CSV column classifier.
 *
 * The image is deliberately the one thing that cannot change — everything a
 * project derives (channel names, the tile pyramid, the mask's geometry) is
 * keyed to it, so swapping it is a new project rather than an edit.
 *
 * Saving posts only what changed. The server merges, so a field this page
 * never renders can never be cleared by saving.
 */
(function () {
    function ready(fn) {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", fn);
        } else {
            fn();
        }
    }

    ready(() => {
        const root = document.getElementById("project-edit");
        if (!root) return;

        const project = JSON.parse(root.dataset.project);
        const needs = root.dataset.needs ? JSON.parse(root.dataset.needs) : null;
        const state = { roles: {}, columns: null };
        let classifier = null;
        let coordinates = null;

        // Mirrors requirementsModal.js's option value for "only one image" --
        // both surfaces translate it into the payload's `single_image` flag
        // and neither ever sends it as it stands.
        const SINGLE_IMAGE = "\u0000single-image";
        // And the cell id's "number the rows" choice -- see requirementsModal.js
        // for why it is an option rather than the wording on the blank.
        const ROW_NUMBER = "\u0000row-number";

        const maskInput = document.getElementById("edit_segmentation");
        if (maskInput) {
            // Nothing else on this page wires a Browse button: the upload
            // page's script is what attaches [data-browse-target], and it does
            // not load here -- so both buttons sat there doing nothing.
            const maskBrowse = maskInput.parentElement
                .querySelector(".browse-button");
            if (typeof attachBrowseButton === "function") {
                attachBrowseButton(maskBrowse, maskInput,
                                   { mode: "file", filter: "image" });
            }
        }

        // -- Data file ------------------------------------------------------
        // The shared control (dataSourceField.js), not a bare input: a .zarr
        // store with several tables, or a table spanning several images, needs
        // narrowing before it can be read at all, and swapping one in here used
        // to post a path the server could only refuse.
        const dataMount = document.getElementById("edit_data_field");
        const dataField = dataMount && window.PlexoraDataSourceField.mount(dataMount, {
            id: "edit_data",
            value: project.data?.src || "",
            hint: dataMount.dataset.hint,
        });

        // -- Column classifier (CSV only) ---------------------------------
        const classifierMount = document.getElementById("edit_classifier");
        if (classifierMount && project.has.columns) {
            classifier = PlexoraColumnClassifier.mount(classifierMount, {
                markers: project.columns.markers,
                metadata: project.columns.metadata,
                roles: project.roles,
                roleLabels: project.roleLabels,
                onChange: () => {
                    const value = classifier.value();
                    state.columns = { markers: value.markers, metadata: value.metadata };
                    Object.assign(state.roles, value.roles);
                },
            });
        }

        // -- Role selects (AnnData / SpatialData) --------------------------
        // The classifier already owns roles for CSV, so these render only for
        // the formats whose columns are not user-classified. Options, current
        // answer and blank-option wording all come from the server, which is
        // what keeps this identical to the requirements modal -- for these
        // formats the question is about the file's own obs columns and the
        // answer goes into the read spec, and neither surface knows that.
        const rolesMount = document.getElementById("edit_roles");
        if (rolesMount && !project.has.columns && project.has.data) {
            const candidates = project.roleColumns || [];

            function configField(labelText) {
                const field = document.createElement("div");
                field.className = "config-field";
                const label = document.createElement("label");
                label.textContent = labelText;
                field.appendChild(label);
                rolesMount.appendChild(field);
                return field;
            }

            // x/y are not column roles for these formats -- an obsm array holds
            // both axes, so they are one question with its own control. Shared
            // with the requirements modal (coordinateField.js) so the two
            // surfaces ask and post the same thing.
            const coordinateOptions = project.coordinateOptions;
            const asksCoordinates = !!coordinateOptions
                && Object.keys(coordinateOptions).length > 0;
            const roles = (project.editableRoles || []).filter(
                (role) => !(asksCoordinates && (role === "x" || role === "y")));

            roles.forEach((role) => {
                const field = configField(project.roleLabels[role]);
                const select = document.createElement("select");
                select.className = "form-select";
                select.append(new Option("—", ""));
                if (role === "image_id") {
                    // The other answer, and the only true one for data with no
                    // such column. See the same option in requirementsModal.js.
                    select.append(new Option("This data has only one image",
                                             SINGLE_IMAGE, false, project.singleImage));
                }
                // Likewise for the cell id: an option, not the wording on the
                // blank, so choosing it is something the user did.
                const fallback = (project.roleDefaults || {})[role];
                if (fallback) {
                    select.append(new Option(fallback, ROW_NUMBER, false,
                                             project.rowNumberIds));
                }
                const current = (project.roleAnswers || {})[role];
                candidates.forEach((name) => {
                    select.append(new Option(name, name, false, current === name));
                });
                select.addEventListener("change", () => {
                    if (role === "image_id") {
                        state.single_image = select.value === SINGLE_IMAGE;
                        state.roles[role] =
                            select.value === SINGLE_IMAGE ? null : (select.value || null);
                        return;
                    }
                    if (fallback) {
                        state.row_number_ids = select.value === ROW_NUMBER;
                        state.roles[role] =
                            select.value === ROW_NUMBER ? null : (select.value || null);
                        return;
                    }
                    state.roles[role] = select.value || null;
                });
                field.appendChild(select);
            });

            if (asksCoordinates) {
                const field = configField("Cell coordinates");
                coordinates = window.PlexoraCoordinateField.mount(field, {
                    ...coordinateOptions,
                    name: "edit-coordinates",
                    onChange: (value) => { state.coordinates = value; },
                });
            }
        }

        // -- Cell layer ----------------------------------------------------
        const cellLayerInput = document.getElementById("edit_cell_layer");

        // -- Expression matrix ---------------------------------------------
        const featuresInput = document.getElementById("edit_features_layer");
        const featuresLogInput = document.getElementById("edit_features_log");

        // -- Highlight what a tool is waiting for --------------------------
        // Reached from the Tools menu without JavaScript: the same requirement
        // list the modal would have rendered, pointed at the fields that
        // answer it. `confirm` counts as well as `missing` -- a tool opened for
        // the first time on a well-named table has nothing missing and still
        // wants its guesses looked at.
        const outstanding = [...(needs?.missing || []), ...(needs?.confirm || [])];
        if (needs && outstanding.length) {
            const banner = document.getElementById("edit_needs");
            banner.hidden = false;
            banner.textContent = needs.missing.length
                ? `${needs.label} needs: ${needs.missing.map((r) => r.label).join(", ")}.`
                : `Check what ${needs.label} will use, then save.`;
            const first = outstanding[0];
            const target = first.kind === "segmentation" ? maskInput
                : first.kind === "data" ? dataMount
                    : first.kind === "features" ? (featuresInput || featuresLogInput)
                        : classifierMount || rolesMount;
            target?.scrollIntoView({ block: "center", behavior: "smooth" });
            target?.classList.add("field-wanted");
        }

        // -- Save ----------------------------------------------------------
        const save = document.getElementById("edit_save");
        const error = document.getElementById("edit_error");

        /**
         * The requirement keys this page put in front of the user, in the
         * server's vocabulary (plexora/api/plugin.py). Derived from what was
         * rendered rather than hardcoded, so a section that does not apply to
         * this project never claims to have asked about it.
         */
        function renderedKeys() {
            const keys = [];
            if (classifierMount && project.has.columns) keys.push("markers");
            if (project.has.features) keys.push("features");
            const roleFields = classifier ? Object.keys(project.roleLabels)
                : (project.editableRoles || []);
            keys.push(...roleFields.map((role) => `role:${role}`));
            // Its own key, because for these formats it is its own question --
            // and role:x/role:y are never asked here, so confirming them would
            // claim the user was shown something the page did not render.
            if (coordinates) {
                keys.push("coordinates");
            }
            return keys;
        }

        save.addEventListener("click", async () => {
            save.disabled = true;
            error.hidden = true;

            // A data file the user has not finished describing -- a store
            // whose table is still unpicked -- is not something to post. The
            // server would refuse it by asking for a choice, which reads as
            // nonsense with the control offering that choice right there.
            const waiting = dataField?.blocking();
            if (waiting) {
                error.textContent = waiting;
                error.hidden = false;
                save.disabled = false;
                return;
            }

            const task = window.PlexoraStatus?.begin("Saving project");

            const payload = {};
            // Only send a path field when its value differs from what is
            // stored. Sending it unchanged would be harmless but would make
            // the server re-read the file and rebuild the mask pyramid, which
            // is minutes of work for a no-op save.
            if (maskInput && maskInput.value.trim() !== (project.segmentation.src || "")) {
                payload.segmentation = maskInput.value.trim();
            }
            // The data file carries three companions when it changes -- which
            // table inside the store, and which image inside the table. They
            // go only with a changed path, since that is the only thing they
            // describe.
            const data = dataField?.value();
            if (data && data.data !== (project.data?.src || "")) {
                payload.data = data.data;
                if (data.table) payload.table = data.table;
                if (data.subset_column) {
                    payload.subset_column = data.subset_column;
                    payload.subset_value = data.subset_value;
                }
            }
            if (Object.keys(state.roles).length) payload.roles = state.roles;
            if (state.columns) payload.columns = state.columns;
            if (state.single_image !== undefined) {
                payload.single_image = state.single_image;
            }
            if (state.row_number_ids !== undefined) {
                payload.row_number_ids = state.row_number_ids;
            }
            // Same rule as the path fields above: only when it differs. The
            // control seeds itself from the stored spec at mount, so sending it
            // unconditionally would re-read the file on every no-op save.
            const chosen = state.coordinates;
            const stored = (project.coordinateOptions || {}).current || {};
            if (chosen && JSON.stringify(chosen) !== JSON.stringify(stored)) {
                payload.coordinates = chosen;
            }
            // The same rule as every other field here, and it was the only one
            // not following it: sending the select's value unconditionally
            // turned a default into an override on every save, including saves
            // that were about something else entirely. "" is a real value to
            // send -- it clears an override -- so this compares rather than
            // testing for truth.
            if (cellLayerInput
                && cellLayerInput.value !== (cellLayerInput.dataset.stored || "")) {
                payload.cellLayer = cellLayerInput.value;
            }
            // Sent only when they differ: the server re-reads the file for
            // either, and saving an unchanged form should stay free.
            if (featuresInput && featuresInput.value !== project.featureSource) {
                payload.features_layer = featuresInput.value;
            }
            if (featuresLogInput && featuresLogInput.checked !== Boolean(project.featureLog)) {
                payload.features_log = featuresLogInput.checked;
            }
            // Saving is the user looking at these values and accepting them,
            // which is the same act the requirements modal asks for -- so a
            // tool opened afterwards must not ask about them again. Sent as the
            // keys this page rendered, whether or not their value changed.
            payload.confirm = renderedKeys();

            try {
                const response = await fetch(
                    plexoraUrl(`project/${encodeURIComponent(project.name)}`),
                    {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(payload),
                    },
                );
                const result = await response.json();
                if (!response.ok || !result.success) {
                    throw new Error(result.error || "Could not save the project");
                }
                task?.done();

                const viewerUrl = plexoraUrl(encodeURIComponent(project.name));
                if (result.segmentation_pending) {
                    awaitSegmentationThenOpen({
                        datasource: project.name,
                        redirectUrl: viewerUrl,
                    });
                } else {
                    window.location.href = viewerUrl;
                }
            } catch (e) {
                // The old save reported failure as HTTP 200 with no message and
                // the client navigated away regardless. Say what went wrong and
                // stay put.
                task?.fail("Save failed");
                error.textContent = e.message;
                error.hidden = false;
                save.disabled = false;
            }
        });
    });
})();

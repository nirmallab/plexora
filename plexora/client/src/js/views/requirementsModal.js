/**
 * requirementsModal.js -- asking for what a plugin needs and the project lacks.
 *
 * A plugin declares its requirements (plexora/api/plugin.py's Requires); the
 * server works out which are unmet and hands over a list of typed descriptors.
 * This renders a form from that list and posts the answers back, which the
 * server stores on the project -- so the next plugin wanting the same thing
 * never asks again.
 *
 * Core knows nothing about any particular plugin here. Every field is built
 * from a requirement's `kind`, which is why adding a requirement to a plugin
 * needs no change in this file.
 *
 * Three properties the design turns on:
 *
 * - **Nothing already answered is shown.** A requirement the user has answered
 *   is absent from the payload rather than rendered as a filled-in field, so
 *   they are never asked to re-confirm what they already gave.
 * - **A guess is not an answer.** Most of a conventionally-named table is
 *   filled in by the column predictor, and those arrive in `confirm`:
 *   prefilled, shown once, and never again once saved. Without this tier the
 *   first launch of a tool would run silently on five guesses, which is what a
 *   well-named file used to produce.
 * - **It loops.** Attaching a data file makes new questions askable (which
 *   column is the cell id — meaningless before there were columns), so after
 *   each save it re-asks the server what remains and re-renders. That is why
 *   the server reports roles and markers only once a table exists.
 */
window.PlexoraRequirements = (function () {
    let openDialog = null;

    // The image-id select's "only one image" choice. Written as an escape so
    // this file stays plain ASCII: a literal NUL byte makes grep and diff
    // treat the whole source as binary. A leading NUL cannot occur in a real
    // obs column name, so the option can never collide with one -- and the
    // value is client-only regardless, translated into the payload's
    // `single_image` flag rather than sent as it stands.
    const SINGLE_IMAGE = "\u0000single-image";

    // The cell-id select's "number the rows" choice, on the same terms and for
    // the same reason. It used to be the wording on the blank option -- which
    // made a select nobody touched indistinguishable from one deliberately
    // left on the row number, and is how a project kept a positional cell id
    // while its mask carried the label values from an obs column. Translated
    // into the payload's `row_number_ids` flag, never sent as it stands.
    const ROW_NUMBER = "\u0000row-number";

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text) node.textContent = text;
        return node;
    }

    /**
     * One row of the form: its label on the left, its control on the right.
     *
     * Every field is a row rather than a label stacked over a control, because
     * the form is a short list of "which column is this?" questions and reading
     * them down a single column of labels is how it stays scannable. "Optional"
     * rides on the label for the same reason -- as its own line under the
     * control it doubled the height of every field that had it.
     */
    function fieldRow(requirement) {
        const field = el("div", "requirement-field");
        const label = el("label", null, requirement.label);
        if (requirement.optional) {
            label.appendChild(el("span", "field-hint", "optional"));
        }
        field.appendChild(label);
        return field;
    }

    /** A mask path: one input, one Browse, nothing to ask about it. */
    function maskField(requirement, state) {
        const field = fieldRow(requirement);

        const row = el("div", "import-field-row");
        const input = el("input", "form-control");
        input.type = "text";
        input.placeholder = "/path/to/segmentation.ome.tif";
        row.appendChild(input);

        const button = el("button", "browse-button", "Browse…");
        button.type = "button";
        row.appendChild(button);
        field.appendChild(row);

        // "Which machine is the mask on?" -- same control the edit page and
        // the import form use, so a mask on the user's own computer can be
        // named here too. A tool asking for one is exactly the moment somebody
        // discovers their mask never left their laptop.
        let location = null;
        if (window.PlexoraDataLocation && window.PlexoraDataLocation.available()) {
            location = window.PlexoraDataLocation.attach(input, {
                kind: "segmentation",
                onChange: (value) => { state.segmentation = value; },
            });
        }
        input.addEventListener("input", () => {
            // When the box holds a path on a machine this server cannot read,
            // the value that means anything here arrives through onChange
            // above once a node has been asked for it.
            if (!location || location.isPlainPath()) {
                state.segmentation = input.value.trim();
            }
        });
        if (typeof attachBrowseButton === "function") {
            attachBrowseButton(button, input, {
                // "any": a .zarr mask is a directory, and a file-only dialog
                // left no way to point at one.
                mode: "any", filter: "image",
                node: () => (location ? location.browseNode() : null),
            });
        }
        return field;
    }

    /**
     * The data file, and whatever the file itself leaves open.
     *
     * Rendered by the shared control (dataSourceField.js) rather than as a
     * bare input, because a path is not always enough to read a file by: a
     * .zarr store with several tables, or a table spanning several images,
     * has to be narrowed before anything can be loaded. This used to be an
     * input and two buttons, so picking such a store posted a path the server
     * could only refuse -- with a message telling the user to choose, and
     * nothing anywhere to choose from.
     */
    function dataField(requirement, state, controls) {
        const field = fieldRow(requirement);
        // The control grows a row per open question, so the label aligns to
        // the path input rather than to the middle of the stack.
        field.classList.add("requirement-field-stacked");
        const mount = el("div", "requirement-stack");
        field.appendChild(mount);
        controls.data = window.PlexoraDataSourceField.mount(mount, {
            onChange: (value) => {
                state.data = value.data;
                state.table = value.table;
                state.subset_column = value.subset_column;
                state.subset_value = value.subset_value;
            },
        });
        return field;
    }

    /**
     * A select over the columns this project answers role questions with.
     *
     * The list arrives ready-made in `roleColumns` and is used exactly as
     * given: for AnnData and SpatialData it is the source file's `obs`,
     * unfiltered, and which of those columns holds the cell id is not
     * something a name heuristic here gets to narrow down. The prefilled
     * answer is the guess; the list is everything.
     */
    function roleField(requirement, needs, state) {
        const field = fieldRow(requirement);

        const select = el("select", "form-select");
        select.append(new Option("Choose a column…", ""));

        // The answers that name no column. Each is offered as its own option
        // rather than carried by the blank, because a blank that satisfies a
        // question is indistinguishable from one the user never looked at --
        // and both of these are the answer a conventionally-shaped file needs,
        // so the blank was reached by default and read as agreement.
        //
        // "There is only one image" cannot be inferred either: deciding it
        // needs to know which column identifies the image, which is this
        // question. "Number the rows" is right only when the mask's labels run
        // in file order, which nothing here can check.
        const isImageId = requirement.role === "image_id";
        if (isImageId) {
            select.append(new Option("This data has only one image", SINGLE_IMAGE));
        }
        // Present only for the formats whose adapter can number the rows
        // itself; a CSV's cell id is always one of its own columns.
        const fallback = (needs.roleDefaults || {})[requirement.role];
        if (fallback) {
            select.append(new Option(fallback, ROW_NUMBER));
        }

        const candidates = needs.roleColumns || [];
        candidates.forEach((name) => select.append(new Option(name, name)));

        // Prefilled with whatever the project holds, which for a `confirm`
        // requirement is the predictor's guess -- the thing the user is here to
        // look at. Seeded into state too, so accepting it unchanged (no change
        // event) still posts the value rather than clearing the role.
        const current = (needs.roleAnswers || {})[requirement.role] || "";
        if (current && candidates.includes(current)) {
            select.value = current;
            state.roles[requirement.role] = current;
        } else if (isImageId && needs.singleImage) {
            select.value = SINGLE_IMAGE;
            state.single_image = true;
        } else if (fallback && needs.rowNumberIds) {
            select.value = ROW_NUMBER;
            state.row_number_ids = true;
        }
        select.addEventListener("change", () => {
            if (isImageId) {
                // Mutually exclusive: each write clears the other, so the
                // payload never claims one image AND names a column.
                state.single_image = select.value === SINGLE_IMAGE;
                state.roles[requirement.role] =
                    select.value === SINGLE_IMAGE ? null : (select.value || null);
                return;
            }
            if (fallback) {
                // Same pairing for the cell id.
                state.row_number_ids = select.value === ROW_NUMBER;
                state.roles[requirement.role] =
                    select.value === ROW_NUMBER ? null : (select.value || null);
                return;
            }
            state.roles[requirement.role] = select.value || null;
        });

        field.appendChild(select);
        return field;
    }

    /**
     * Where each cell's position is read from.
     *
     * One question, not the two column roles it used to be -- see
     * coordinateField.js for why, and for the shape of the answer. Rendered by
     * the shared control so this surface and the project edit page cannot
     * disagree about what they are asking or what they post back.
     */
    function coordinatesField(requirement, needs, state) {
        const field = fieldRow(requirement);
        field.classList.add("requirement-field-stacked");
        const mount = el("div", "requirement-stack");
        field.appendChild(mount);
        // Seeded up front, like every other field here: accepting a prefilled
        // value fires no change event, and without this a confirmed-unchanged
        // answer would post nothing and be asked again on the next open.
        const control = window.PlexoraCoordinateField.mount(mount, {
            ...(needs.coordinateOptions || {}),
            name: `coordinates-${requirement.key}`,
            onChange: (value) => { state.coordinates = value; },
        });
        state.coordinates = control.value();
        return field;
    }

    /**
     * Which matrix the marker values are read from, and whether to log them.
     *
     * Two controls on one row because they are one decision: "what numbers am
     * I thresholding". A file commonly holds raw counts in `X` and a
     * log-transformed copy in a layer, and nothing about the numbers themselves
     * says which is which -- so a threshold set on the wrong one is not a
     * cosmetic mistake, it is a meaningless number. The log switch covers the
     * other half: a file with only raw counts has no layer to pick, and the
     * values still need transforming before a threshold on them reads sensibly.
     *
     * The select is dropped when the file carries only `X` -- one option is not
     * a choice -- but the switch stays, because it is still a real question.
     */
    function featuresField(requirement, needs, state) {
        const field = fieldRow(requirement);
        const row = el("div", "requirement-inline");

        const options = needs.featureOptions || [];
        // Seeded into state up front: accepting what is shown fires no change
        // event, and the answer still has to reach the server.
        state.features_layer = needs.featureSource || "X";
        if (options.length > 1) {
            const select = el("select", "form-select");
            options.forEach((option) => select.append(new Option(option.label, option.value)));
            select.value = state.features_layer;
            select.addEventListener("change", () => {
                state.features_layer = select.value;
            });
            row.appendChild(select);
        }

        const toggle = el("label", "requirement-inline-check");
        const box = el("input");
        box.type = "checkbox";
        box.checked = Boolean(needs.featureLog);
        box.title = "Apply log1p to the values as they are read.";
        state.features_log = box.checked;
        box.addEventListener("change", () => {
            state.features_log = box.checked;
        });
        toggle.append(box, el("span", null, "log1p"));
        row.appendChild(toggle);

        field.appendChild(row);
        return field;
    }

    /**
     * The marker/metadata split -- and only that.
     *
     * The classifier can also carry the role selects (the edit page uses it
     * that way), but not here: this form already renders a field per role the
     * plugin asked for, so handing it labels drew every one of them a second
     * time inside the metadata box. It also drew the ones nothing asked for --
     * a CSV import showed a "Cell type column" select that no installed plugin
     * reads and the user had no reason to answer.
     */
    function classificationField(requirement, needs, state) {
        const field = el("div", "requirement-field requirement-field-wide");
        field.appendChild(el("label", null, requirement.label));
        const mount = el("div");
        field.appendChild(mount);

        const classifier = PlexoraColumnClassifier.mount(mount, {
            markers: (needs.columns || {}).markers || [],
            metadata: (needs.columns || {}).metadata || [],
            roleLabels: {},
            onChange: () => {
                const value = classifier.value();
                state.columns = { markers: value.markers, metadata: value.metadata };
                Object.assign(state.roles, value.roles);
            },
        });
        // Seed immediately: a user who accepts the prediction unchanged never
        // fires onChange, and the form would otherwise post nothing.
        const initial = classifier.value();
        state.columns = { markers: initial.markers, metadata: initial.metadata };
        Object.assign(state.roles, initial.roles);
        return field;
    }

    /**
     * Draw the form, and hand back the controls the save step has to consult.
     *
     * Only the data field is in there: it is the one control whose answer is
     * not complete the moment it is typed (see dataSourceField.js), so saving
     * has to ask it whether it is ready. Kept out of `state`, which is posted
     * as it stands.
     */
    function render(dialog, needs, state) {
        const controls = {};
        const body = dialog.querySelector(".requirements-body");
        body.replaceChildren();

        // Missing first, then the prefilled ones, then what is merely offered:
        // the empty fields are what block the tool, so they lead.
        const all = [...(needs.missing || []), ...(needs.confirm || []),
                     ...(needs.optional || [])];
        // Every key rendered is a key the user has now been asked about, blank
        // answer included -- that is what stops an optional field they chose to
        // skip from reappearing on every open.
        state.confirm = all.map((requirement) => requirement.key);

        all.forEach((requirement) => {
            if (requirement.kind === "data") {
                body.appendChild(dataField(requirement, state, controls));
            } else if (requirement.kind === "segmentation") {
                body.appendChild(maskField(requirement, state));
            } else if (requirement.kind === "role") {
                body.appendChild(roleField(requirement, needs, state));
            } else if (requirement.kind === "coordinates") {
                body.appendChild(coordinatesField(requirement, needs, state));
            } else if (requirement.kind === "features") {
                body.appendChild(featuresField(requirement, needs, state));
            } else if (requirement.kind === "classification") {
                body.appendChild(classificationField(requirement, needs, state));
            }
        });

        const blocking = (needs.missing || []).length;
        dialog.querySelector(".requirements-title").textContent =
            blocking ? "Before this tool can open" : `Set up ${needs.label}`;
        // Three cases, not two. Blocking is core's to word. A prefilled form is
        // core's too -- "we guessed, check it" is true of every plugin. A form
        // made entirely of things nobody has to fill in is the one core cannot
        // word: the generic line claims Plexora filled them in from the data,
        // and for a plugin whose fields are all optional nothing was filled in
        // and nothing is required, so the sentence is false twice over. That
        // one comes from the plugin (Plugin.intro).
        const optionalOnly = !blocking && !(needs.confirm || []).length;
        dialog.querySelector(".requirements-subtitle").textContent = blocking
            ? `${needs.label} needs a little more about this project.`
            : (optionalOnly && needs.intro)
                ? needs.intro
                : `Check what ${needs.label} will use. Plexora filled these in from the data — you will not be asked again.`;

        // On a form where nothing is required, "Cancel" is the wrong word for
        // the only button that gets you to the tool. It reads as "do not open
        // this", and taking it literally would leave a plugin that requires
        // nothing permanently unopenable: the caller re-enters on a true
        // result, so declining has to be RECORDED, not just obeyed. `data-skip`
        // routes the button through the same save as Continue, with whatever
        // the user did not fill in left blank -- which is what marks the offer
        // answered and stops it coming back on every open.
        const cancel = dialog.querySelector('[data-action="cancel"]');
        cancel.dataset.skip = optionalOnly ? "true" : "false";
        cancel.textContent = optionalOnly ? "Skip" : "Cancel";

        return controls;
    }

    function buildDialog() {
        const dialog = document.createElement("dialog");
        dialog.className = "requirements-modal";
        dialog.innerHTML = `
            <form method="dialog" class="requirements-form">
                <h2 class="requirements-title">Before this tool can open</h2>
                <p class="requirements-subtitle"></p>
                <div class="requirements-body"></div>
                <div class="requirements-error" role="alert" hidden></div>
                <div class="requirements-actions">
                    <button type="button" class="btn btn-secondary" data-action="cancel">Cancel</button>
                    <button type="button" class="btn btn-primary" data-action="save">Continue</button>
                </div>
            </form>
        `;
        document.body.appendChild(dialog);
        return dialog;
    }

    /**
     * Ask for everything `needs` reports missing; resolve true once the tool
     * can open.
     *
     * @param datasource the project name
     * @param needs      the payload from tool_routes' `_needs()`
     * @returns Promise<boolean> — false if the user cancelled
     */
    function collect(datasource, needs) {
        if (openDialog) openDialog.remove();
        const dialog = openDialog = buildDialog();
        const error = dialog.querySelector(".requirements-error");
        const save = dialog.querySelector('[data-action="save"]');
        let state = { roles: {} };
        let controls = render(dialog, needs, state);
        dialog.showModal();

        return new Promise((resolve) => {
            function close(result) {
                dialog.close();
                dialog.remove();
                if (openDialog === dialog) openDialog = null;
                resolve(result);
            }

            const cancel = dialog.querySelector('[data-action="cancel"]');
            cancel.addEventListener("click", () => {
                // "Skip" on an all-optional form is an empty Continue: it saves
                // nothing but the list of keys the form showed, which is what
                // records the offer as declined. See render().
                if (cancel.dataset.skip === "true") return submit();
                close(false);
            });
            // Escape stays a plain way out, on every form. It leaves the offer
            // standing for next time, which is the right reading of a keypress
            // that might have been aimed at something else -- and it is safe
            // because a false result means the caller does not re-enter.
            dialog.addEventListener("cancel", () => close(false));

            save.addEventListener("click", () => submit());

            async function submit() {
                save.disabled = true;
                cancel.disabled = true;
                error.hidden = true;
                // A data file the user has not finished describing -- a store
                // whose table is still unpicked -- is not something to post.
                // The server would reject it with a message asking for a
                // choice, which reads as nonsense when the control offering
                // that choice is on screen and untouched.
                // Wait for a reading that is merely still running, rather
                // than refusing the click for being early. Only a question the
                // user has to answer -- an unpicked table, an unchosen image --
                // stops the save below.
                await controls.data?.settled?.();
                const waiting = controls.data?.blocking?.();
                if (waiting) {
                    error.textContent = waiting;
                    error.hidden = false;
                    save.disabled = false;
                    cancel.disabled = false;
                    return;
                }
                const task = window.PlexoraStatus?.begin("Saving");
                try {
                    const payload = { tool: needs.tool, ...state };
                    const send = async () => {
                        const response = await fetch(
                            plexoraUrl(`${encodeURIComponent(datasource)}/requirements`),
                            {
                                method: "POST",
                                headers: { "Content-Type": "application/json" },
                                body: JSON.stringify(payload),
                            },
                        );
                        return response;
                    };
                    // Naming a data file makes this save read that file, which
                    // on a multi-image table is the longest thing this dialog
                    // ever does. Show the stages over the form rather than a
                    // disabled button and nothing else.
                    const body = dialog.querySelector(".requirements-body");
                    const response = payload.data && window.PlexoraTableProgress
                        ? await window.PlexoraTableProgress.watch({
                            datasource, work: send(), mount: body,
                            title: "Preparing this project’s cells",
                        })
                        : await send();
                    const result = await response.json();
                    if (!response.ok || !result.success) {
                        throw new Error(result.error || "Could not save");
                    }
                    task?.done();

                    // An answer that changed what gets read means the page is
                    // holding the previous table's statistics, and the tool
                    // about to open is drawn from them. Re-fetch before handing
                    // back, so the caller never renders a panel of the numbers
                    // the user just stopped reading.
                    if (result.reloaded) {
                        await window.__plexora?.refreshDataset?.();
                    }

                    // Both lists, because they are different questions and the
                    // second one only becomes askable once the first is
                    // answered: naming a data file is what makes "which column
                    // is the cell id" a question with answers, and for a plugin
                    // that merely OFFERS that column it arrives as optional
                    // rather than missing. Closing on `stillMissing` alone shut
                    // the form the instant the file was attached, one question
                    // short of the point.
                    if (!(result.stillMissing || []).length
                        && !(result.stillOptional || []).length) {
                        return close(true);
                    }
                    // Answering one question can reveal others -- naming the
                    // data file is what makes "which column is the cell id"
                    // answerable. Re-ask rather than guessing what is next.
                    needs = await fetchNeeds(datasource, needs.tool);
                    if (!needs) return close(true);
                    state = { roles: {} };
                    controls = render(dialog, needs, state);
                    // Say why the form came back. Leaving a blocking select on
                    // "Choose a column…" lands here, and re-rendering the same
                    // fields with nothing said reads as the button doing
                    // nothing at all.
                    const open = (needs.missing || []).map((r) => r.label);
                    if (open.length) {
                        error.textContent = `Still needed: ${open.join(", ")}.`;
                        error.hidden = false;
                    }
                    save.disabled = false;
                    cancel.disabled = false;
                } catch (e) {
                    task?.fail("Save failed");
                    error.textContent = e.message;
                    error.hidden = false;
                    save.disabled = false;
                    cancel.disabled = false;
                }
            }
        });
    }

    /** The tool's outstanding requirements, or null when it can just open. */
    async function fetchNeeds(datasource, tool) {
        const response = await fetch(plexoraUrl(
            `${encodeURIComponent(datasource)}/tools/${encodeURIComponent(tool)}/requirements`));
        const payload = await response.json();
        if (!payload.success) return null;
        // `optional` counts. It is the only list a plugin that requires nothing
        // ever fills, so leaving it out made "outstanding" mean "blocking" and
        // handed back null the moment a data file was attached -- closing the
        // form on the pass that was about to ask which column holds the cell id.
        const outstanding = (payload.missing || []).length
            + (payload.confirm || []).length
            + (payload.optional || []).length;
        return outstanding ? payload : null;
    }

    /**
     * Ask for named requirements mid-session, after a tool is already open.
     *
     * Gating uses this: an image-id column only matters when the user chooses
     * to write gates back to the source file, long after the panel opened.
     * Resolves true once the named keys are satisfied.
     */
    async function require(datasource, tool, keys) {
        // `keys` asks a different question from the ordinary lists: not "what
        // has the user not been asked yet" but "what does this action need
        // right now". An optional field they were offered and skipped is
        // absent from the first and present in the second, which is the whole
        // reason this takes the explicit route rather than filtering `optional`.
        const url = `${encodeURIComponent(datasource)}/tools/${encodeURIComponent(tool)}`
            + `/requirements?keys=${encodeURIComponent(keys.join(","))}`;
        const full = await fetch(plexoraUrl(url)).then((r) => r.json());
        if (!full.success) return false;

        const wanted = full.requested || [];
        if (!wanted.length) return true;
        // Shown as required here even for an optional input: the caller cannot
        // do what the user just asked for without it.
        return collect(datasource, {
            ...full,
            missing: wanted.map((r) => ({ ...r, optional: false })),
            confirm: [],
            optional: [],
        });
    }

    return { collect, require };
})();

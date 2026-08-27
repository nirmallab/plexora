/**
 * importFormValidation.js -- the upload page's one form.
 *
 * The page asks for a name, an image, an optional mask and an optional data
 * file. It does not ask what format the data is in: /inspect_data answers that
 * from the file, and the two controls that cannot be answered from the file --
 * which table inside a multi-table .zarr store, and which image inside a table
 * that spans several -- are revealed only when the file forces the choice.
 *
 * Submission is a native POST navigation rather than fetch(). That is
 * deliberate: this page's scripts share a realm with base.html's stack, and an
 * AJAX submit here previously had to reconstruct a whole page's worth of state
 * from the response. Letting the browser navigate keeps the server free to
 * redirect wherever import decided the user should go next -- the viewer, or
 * the column-classification screen.
 */

(function () {
    'use strict'

    // Bootstrap validation styling; the browser handles the actual submit.
    Array.prototype.slice.call(document.querySelectorAll('.needs-validation'))
        .forEach(function (form) {
            form.addEventListener('submit', function (event) {
                if (!form.checkValidity()) {
                    event.preventDefault();
                    event.stopPropagation();
                } else {
                    setSubmitting(form, true);
                }
                form.classList.add('was-validated');
            }, true);
        });
})();

function setSubmitting(form, busy) {
    const button = form.querySelector('button[type="submit"]');
    if (!button) return;
    button.disabled = busy;
    // Reading a large .h5ad to work out its structure takes a moment, and the
    // page navigates rather than streaming progress -- so say something.
    button.textContent = busy ? 'Importing…' : 'Create Project';
}

// --------------------------------------------------------------------------
// Project name, suggested from the image path
// --------------------------------------------------------------------------

let datasetNameManuallyEdited = false;

function markDatasetNameEdited() {
    datasetNameManuallyEdited = true;
}

function deriveDatasetName(path) {
    if (!path) return "";
    const base = path.split(/[\\/]/).pop() || "";
    return base.replace(/\.(ome\.tiff|ome\.tif|ome\.zarr|tiff|tif|svs|zarr|png|qptiff)$/i, "");
}

function suggestDatasetName(caller, targetFieldId) {
    if (datasetNameManuallyEdited) return;
    const nameField = document.getElementById(targetFieldId || "name");
    if (!nameField) return;
    const suggested = deriveDatasetName(caller && caller.value);
    if (suggested) nameField.value = suggested;
}

PlexoraPage.register(function () {
    // Wire every "Browse..." button (see browsePicker.js) to fill its paired
    // text field via the native OS file/folder dialog.
    document.querySelectorAll('[data-browse-target]').forEach(function (button) {
        const input = document.getElementById(button.dataset.browseTarget);
        attachBrowseButton(button, input, {
            mode: button.dataset.browseMode || 'file',
            filter: button.dataset.browseFilter || 'any',
        });
        // A path arriving from the dialog has to go through the same
        // inspection a typed one does, or the table/subset pickers never
        // appear for anyone who used Browse.
        if (input && input.id === 'data_file') {
            button.addEventListener('click', () => setTimeout(() => inspectDataFile(input), 0));
        }
    });

    const dataField = document.getElementById('data_file');
    if (dataField && dataField.value) inspectDataFile(dataField);
});

// --------------------------------------------------------------------------
// Path checks
// --------------------------------------------------------------------------

function markValidity(input, valid) {
    input.classList.remove('is-valid', 'is-invalid');
    if (valid !== null) input.classList.add(valid ? 'is-valid' : 'is-invalid');
}

async function checkFileExistence(caller) {
    if (!caller.value) return markValidity(caller, null);
    const response = await fetch(plexoraUrl('check_file_existence'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: caller.value }),
    });
    const { exists } = await response.json();
    markValidity(caller, exists);
}

/** Blank is fine for an optional field -- an empty box is not an error. */
async function checkOptionalFileExistence(caller) {
    if (!caller.value) return markValidity(caller, null);
    return checkFileExistence(caller);
}

async function checkDatasetExistence(caller) {
    const response = await fetch(plexoraUrl('dataset_existence'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ datasetName: caller.value }),
    });
    const { exists } = await response.json();
    markValidity(caller, !exists);
}

// --------------------------------------------------------------------------
// Inspecting the data file
// --------------------------------------------------------------------------

const DATA_TYPE_LABELS = {
    csv: 'CSV table',
    anndata: 'AnnData',
    spatialdata: 'SpatialData store',
};

// Replies can arrive out of order when someone types quickly, and a stale one
// would re-show a picker for a file that is no longer in the box.
let inspectToken = 0;
let inspectTimer = null;

function inspectDataFile(caller) {
    clearTimeout(inspectTimer);
    // Every inspection opens the file, and an .h5ad is not cheap to open --
    // so wait for a pause in typing rather than firing per keystroke.
    inspectTimer = setTimeout(() => runInspection(caller), 250);
}

/**
 * @param caller the data path input
 * @param table  which table inside a .zarr store to look at, on the second
 *   pass. Everything below the table picker is a question about a table, so a
 *   multi-table store cannot answer any of it until one is named -- and the
 *   picker's own selection has to survive, which is why the table field is
 *   left alone when this is set.
 */
async function runInspection(caller, table) {
    const token = ++inspectToken;
    const hint = document.getElementById('data_file_hint');
    const error = document.getElementById('data_file_error');
    if (!table) hideField('data_table_field');
    hideField('subset_field');

    if (!caller.value) {
        markValidity(caller, null);
        caller.setCustomValidity('');
        hint.textContent = 'CSV, AnnData (.h5ad) or SpatialData (.zarr).';
        return;
    }

    let payload;
    try {
        const response = await fetch(plexoraUrl('inspect_data'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: caller.value, table: table || null }),
        });
        payload = await response.json();
    } catch (e) {
        return;  // transport failures are reported by PlexoraStatus
    }
    if (token !== inspectToken) return;

    if (!payload.ok) {
        markValidity(caller, false);
        // setCustomValidity is what stops the form submitting -- the red
        // border alone would let an unreadable file through to the server.
        caller.setCustomValidity(payload.error || 'Unreadable');
        error.textContent = payload.error || '';
        hint.textContent = '';
        return;
    }

    markValidity(caller, true);
    caller.setCustomValidity('');
    error.textContent = '';
    hint.textContent = DATA_TYPE_LABELS[payload.data_type] || payload.data_type;

    if (!table && (payload.tables || []).length > 1) {
        // Choosing re-runs this with the table, which is what puts the subset
        // question. Returning here used to be the end of it, so a multi-table
        // store could import loading every image at once.
        //
        // The re-run also matters for what this form does NOT ask: the chosen
        // table's obs columns and layer names are recorded from this same
        // inspection, and the requirements modal asks about them later.
        showTablePicker(payload.tables, caller);
        return;
    }
    if ((payload.ambiguous || []).length) {
        showSubsetPicker(payload.ambiguous[0]);
    }
}

function hideField(id) {
    const field = document.getElementById(id);
    if (!field) return;
    field.hidden = true;
    // A hidden required control blocks submission with no visible cause.
    field.querySelectorAll('select, input').forEach((el) => {
        el.required = false;
    });
}

function showTablePicker(tables, dataInput) {
    const field = document.getElementById('data_table_field');
    const select = document.getElementById('data_table');
    select.innerHTML = '';
    select.append(new Option('Choose a table…', ''));
    tables.forEach((table) => {
        select.append(new Option(
            `${table.name} — ${table.n_obs} cells × ${table.n_var} markers`,
            table.name,
        ));
    });
    // Assigned rather than added: this runs again on every re-inspection, and
    // addEventListener would stack a fresh handler each time.
    select.onchange = () => {
        hideField('subset_field');
        if (select.value) runInspection(dataInput, select.value);
    };
    select.required = true;
    field.hidden = false;
}

function showSubsetPicker(ambiguous) {
    const field = document.getElementById('subset_field');
    const select = document.getElementById('subset_value');
    document.getElementById('subset_column').value = ambiguous.column;
    document.getElementById('subset_hint').textContent =
        `This table covers several images (column "${ambiguous.column}"). ` +
        'Choose the one this image shows.';
    select.innerHTML = '';
    select.append(new Option('Choose an image…', ''));
    (ambiguous.values || []).forEach((value) => select.append(new Option(value, value)));
    select.required = true;
    field.hidden = false;
}


/**
 * Fill a path field with a data node's address.
 *
 * The field accepts `node://<node>/<resource>` exactly as it accepts a path, so
 * these buttons are a convenience rather than a mode -- somebody who knows the
 * syntax can type it, and somebody who does not never has to learn it.
 *
 * Validation is skipped deliberately: `checkFileExistence` asks the server
 * whether a PATH exists, and a node address is not one. The import itself
 * checks that the node is serving the resource, which is the question that
 * actually matters and the only place it can be answered.
 */
document.addEventListener("click", (event) => {
    const pick = event.target.closest("[data-node-locator]");
    if (!pick) return;
    const field = document.getElementById(pick.dataset.nodeTarget);
    if (!field) return;
    field.value = pick.dataset.nodeLocator;
    field.classList.remove("is-invalid");
    field.dispatchEvent(new Event("change", { bubbles: true }));
    if (pick.dataset.nodeTarget === "image_file") {
        const name = document.getElementById("name");
        // A node address has no filename to take a project name from, so the
        // resource id is the best suggestion there is.
        if (name && !name.value) {
            name.value = pick.dataset.nodeLocator.split("/").pop();
        }
    }
});

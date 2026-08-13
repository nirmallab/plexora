let datasourceConfigData;

function initDatasourceConfig(data) {
    datasourceConfigData = data;

    document.getElementById('config-dataset-name').textContent = data.name;
    document.getElementById('config-summary').textContent =
        data.obs_count + ' observations, ' + data.n_var + ' features -- ' + data.features;

    populateSelect('coordinate-source', buildCoordinateSourceOptions(data));
    populateSelect('obsm-key', (data.obsm_keys || []).map(toOption));
    populateSelect('x-column', columnOptions(data, true));
    populateSelect('y-column', columnOptions(data, true));
    populateSelect('cell-id-column', columnOptions(data, true), true);
    populateSelect('layer-select', (data.layers || []).map(toOption));
    populateSelect('feature-obs-columns', columnOptions(data, true));

    // Default X/Y to the common CellProfiler/MCMICRO naming convention when
    // present, rather than leaving both selects on whatever numeric column
    // happens to sort first (previously caused X and Y to silently default
    // to the same column). Cell ID defaults to the segmentation-derived
    // 'CellID' obs column when present -- required so exported cell IDs
    // line up with the segmentation mask's label values instead of an
    // arbitrary positional index.
    setDefaultBy('x-column', function (name) { return /^x[_ ]?centroid$/i.test(name); });
    setDefaultBy('y-column', function (name) { return /^y[_ ]?centroid$/i.test(name); });
    setDefaultBy('cell-id-column', function (name) { return /^cell[_ ]?id$/i.test(name); });

    // Every obs column is offered as a subset choice, not just the ones
    // that look like a plausible image/sample identifier -- narrowing that
    // list previously hid legitimate columns. "None" (load everything)
    // stays the default; likely_multi_image_identifier below still governs
    // whether the ambiguity warning shows.
    const allColumns = data.obs_columns || [];
    const subsetOptions = allColumns.map(function (c) {
        return {value: c.name, label: c.name + (c.values ? ' (' + c.values.length + ' values)' : '')};
    });
    populateSelect('subset-column', subsetOptions, true);

    // Only show the "may be rejected" warning for columns that look like a
    // genuine image/sample identifier (matches the backend adapter's own
    // ambiguity guard) -- not for every categorical column. Ordinary
    // annotations like cell_type/leiden cluster are common subset candidates
    // in a perfectly normal single-image file and would otherwise trigger a
    // misleading "multiple images" warning.
    const likelyIdentifierCandidates = allColumns.filter(function (c) { return c.likely_multi_image_identifier; });
    if (likelyIdentifierCandidates.length > 0) {
        document.getElementById('subset-warning').style.display = 'block';
    }

    document.getElementById('coordinate-source').value = (data.obsm_keys || []).indexOf('spatial') !== -1 ? 'obsm' : 'obs';
    if (data.obsm_keys && data.obsm_keys.indexOf('spatial') !== -1) {
        document.getElementById('obsm-key').value = 'spatial';
    }
    updateCoordinateVisibility();
    updateFeatureVisibility();

    document.getElementById('coordinate-source').addEventListener('change', updateCoordinateVisibility);
    document.getElementById('feature-source').addEventListener('change', updateFeatureVisibility);
    document.getElementById('subset-column').addEventListener('change', updateSubsetVisibility);
    document.getElementById('save-datasource-config').addEventListener('click', saveDatasourceConfig);
}

function setDefaultBy(selectId, predicate) {
    const select = document.getElementById(selectId);
    if (!select) return;
    const match = Array.from(select.options).find(function (o) { return o.value && predicate(o.value); });
    if (match) select.value = match.value;
}

function toOption(value) {
    return {value: value, label: value};
}

function buildCoordinateSourceOptions(data) {
    const options = [];
    if ((data.obsm_keys || []).length > 0) {
        options.push({value: 'obsm', label: 'Spatial key (adata.obsm)'});
    }
    options.push({value: 'obs', label: 'Observation columns (adata.obs)'});
    return options;
}

function columnOptions(data, numericOnly) {
    return (data.obs_columns || [])
        .filter(function (c) { return !numericOnly || /int|float/i.test(c.dtype); })
        .map(function (c) { return {value: c.name, label: c.name + ' (' + c.dtype + ')'}; });
}

//keepFirstOption: preserve the template's static first <option> (a "None"/
//"Default" placeholder) and append real options after it, instead of wiping
//the select entirely.
function populateSelect(id, options, keepFirstOption) {
    const select = document.getElementById(id);
    if (!select) return;
    select.innerHTML = keepFirstOption && select.options.length ? select.options[0].outerHTML : '';
    options.forEach(function (option) {
        const el = document.createElement('option');
        el.value = option.value;
        el.textContent = option.label;
        select.appendChild(el);
    });
}

function updateCoordinateVisibility() {
    const source = document.getElementById('coordinate-source').value;
    document.getElementById('obsm-key-field').style.display = source === 'obsm' ? 'block' : 'none';
    document.getElementById('x-column-field').style.display = source === 'obs' ? 'block' : 'none';
    document.getElementById('y-column-field').style.display = source === 'obs' ? 'block' : 'none';
}

function updateFeatureVisibility() {
    const source = document.getElementById('feature-source').value;
    document.getElementById('layer-field').style.display = source === 'layer' ? 'block' : 'none';
    document.getElementById('feature-obs-field').style.display = source === 'obs' ? 'block' : 'none';
}

function updateSubsetVisibility() {
    const column = document.getElementById('subset-column').value;
    const valueField = document.getElementById('subset-value-field');
    if (!column) {
        valueField.style.display = 'none';
        return;
    }
    const candidate = (datasourceConfigData.obs_columns || []).find(function (c) { return c.name === column; });
    populateSelect('subset-value', (candidate && candidate.values || []).map(toOption));
    valueField.style.display = 'block';
}

function showConfigError(message) {
    const errorBox = document.getElementById('config-error');
    errorBox.textContent = message;
    errorBox.style.display = 'block';
}

function saveDatasourceConfig() {
    const button = document.getElementById('save-datasource-config');
    document.getElementById('config-error').style.display = 'none';

    const coordinateSource = document.getElementById('coordinate-source').value;
    const featureSource = document.getElementById('feature-source').value;
    const subsetColumn = document.getElementById('subset-column').value;
    const subsetValue = subsetColumn ? document.getElementById('subset-value').value : null;

    const payload = {
        name: datasourceConfigData.name,
        image: datasourceConfigData.image,
        segmentation: datasourceConfigData.segmentation,
        features: datasourceConfigData.features,
        coordinate_source: coordinateSource,
        obsm_key: coordinateSource === 'obsm' ? document.getElementById('obsm-key').value : null,
        x: coordinateSource === 'obs' ? document.getElementById('x-column').value : null,
        y: coordinateSource === 'obs' ? document.getElementById('y-column').value : null,
        feature_source: featureSource,
        layer: featureSource === 'layer' ? document.getElementById('layer-select').value : null,
        feature_obs_columns: featureSource === 'obs'
            ? Array.from(document.getElementById('feature-obs-columns').selectedOptions).map(function (o) { return o.value; })
            : null,
        celltype_column: null,
        obs_id_field: document.getElementById('cell-id-column').value || null,
        subset_by: subsetColumn || null,
        subset_value: subsetValue,
        apply_log_transform: document.getElementById('apply-log-transform').checked,
        channel_names: datasourceConfigData.channel_names || null,
        attach_to: datasourceConfigData.attach_to || null,
        return_tool: datasourceConfigData.return_tool || null,
    };

    button.disabled = true;
    button.innerHTML = 'Saving... <span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>';

    fetch(plexoraUrl('save_datasource_config'), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
    })
        .then(function (response) {
            return response.json().then(function (result) {
                return {ok: response.ok, result: result};
            });
        })
        .then(function (outcome) {
            if (outcome.ok && outcome.result.success) {
                if (outcome.result.attach_to && outcome.result.return_tool) {
                    window.location = plexoraUrl(outcome.result.attach_to + '?tool=' + outcome.result.return_tool);
                } else {
                    window.location = plexoraUrl(outcome.result.name);
                }
            } else {
                showConfigError(outcome.result.error || 'Failed to save datasource configuration.');
                button.disabled = false;
                button.textContent = 'Save & Open Viewer';
            }
        })
        .catch(function (error) {
            showConfigError('Request failed: ' + error);
            button.disabled = false;
            button.textContent = 'Save & Open Viewer';
        });
}

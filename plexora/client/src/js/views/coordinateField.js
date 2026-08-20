/**
 * The "where are the cell coordinates" control.
 *
 * Shared by the requirements modal and the project edit page, the same way
 * columnClassifier.js is -- both ask this question and neither should own a
 * private version of the answer shape, which is a read spec the server has to
 * be able to apply either way.
 *
 * Why it is one control rather than the two column selects it replaces:
 *
 *   A file can put a cell's position in either of two places. `obs` holds it as
 *   a pair of columns, which two selects express fine. `obsm` holds it as one
 *   (n, 2) array holding BOTH axes -- and "which column is X" has no answer for
 *   that. So the obsm case could previously only appear as a blank option with
 *   the detected key printed on it: visible, unpickable, never confirmed. The
 *   source got decided by matching the key name against a hardcoded list.
 *
 *   That is not a safe way to decide it. A store routinely carries `spatial`
 *   and `X_umap` as identically-shaped (n, 2) float32 arrays, so nothing about
 *   the data separates a position from an embedding -- and a file whose UMAP
 *   happens to be named `spatial` would have had every cell drawn in UMAP space
 *   with nothing said. Hence: list every candidate with its shape, and let the
 *   person who knows the file choose.
 *
 * mount() returns { value() }, where value() is the payload the server takes:
 *
 *     { source: "obsm", obsm_key: "spatial" }
 *     { source: "obs", x_column: "X_centroid", y_column: "Y_centroid" }
 *
 * or null while the choice is incomplete, so a caller can tell "not answered
 * yet" from "answered" without inspecting the controls.
 */
window.PlexoraCoordinateField = (function () {

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text) node.textContent = text;
        return node;
    }

    /** `obsm["spatial"] · 42609 × 2` -- the name, and the shape that is the
     *  only other thing on offer to tell candidates apart. */
    function describe(entry) {
        const shape = (entry.shape || []).join(" × ");
        return shape ? `obsm["${entry.name}"] · ${shape}` : `obsm["${entry.name}"]`;
    }

    /**
     * @param container   element to render into
     * @param options     { obsm: [{name, shape}], obs: [name], current: {...},
     *                      name: unique radio-group name, onChange(value) }
     */
    function mount(container, options) {
        const obsmEntries = options.obsm || [];
        const obsColumns = options.obs || [];
        const current = options.current || {};
        const onChange = options.onChange || function () {};
        const groupName = options.name || "coordinates";

        const root = el("div", "coordinate-field");

        const obsmSelect = el("select", "form-select");
        obsmSelect.append(new Option("Choose an array…", ""));
        obsmEntries.forEach((entry) => {
            obsmSelect.append(new Option(describe(entry), entry.name));
        });

        const obsRow = el("div", "coordinate-field-pair");
        const xSelect = el("select", "form-select");
        const ySelect = el("select", "form-select");
        [[xSelect, "X column…"], [ySelect, "Y column…"]].forEach(([select, blank]) => {
            select.append(new Option(blank, ""));
            obsColumns.forEach((name) => select.append(new Option(name, name)));
            obsRow.appendChild(select);
        });

        let source = current.source === "obs" ? "obs" : "obsm";

        function value() {
            if (source === "obsm") {
                return obsmSelect.value
                    ? { source: "obsm", obsm_key: obsmSelect.value } : null;
            }
            return (xSelect.value && ySelect.value)
                ? { source: "obs", x_column: xSelect.value, y_column: ySelect.value }
                : null;
        }

        function show(next) {
            source = next;
            obsmSelect.hidden = next !== "obsm";
            obsRow.hidden = next !== "obs";
            onChange(value());
        }

        // A radio rather than a third dropdown: the choice changes what the
        // control below it is asking, and the user has to see both places at
        // once to know the other one exists.
        const radios = el("div", "coordinate-field-source");
        [["obsm", ".obsm"], ["obs", ".obs"]].forEach(([choice, label]) => {
            const wrapper = el("label", "coordinate-field-radio");
            const input = document.createElement("input");
            input.type = "radio";
            input.name = groupName;
            input.value = choice;
            input.checked = choice === source;
            input.addEventListener("change", () => {
                if (input.checked) show(choice);
            });
            wrapper.appendChild(input);
            wrapper.appendChild(el("span", null, label));
            radios.appendChild(wrapper);
        });

        if (current.obsm_key) obsmSelect.value = current.obsm_key;
        if (current.x_column) xSelect.value = current.x_column;
        if (current.y_column) ySelect.value = current.y_column;
        [obsmSelect, xSelect, ySelect].forEach((select) => {
            select.addEventListener("change", () => onChange(value()));
        });

        root.appendChild(radios);
        root.appendChild(obsmSelect);
        root.appendChild(obsRow);
        container.appendChild(root);

        show(source);
        return { value };
    }

    return { mount };
}());

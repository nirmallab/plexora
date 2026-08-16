/**
 * datasetContext.js - the dataset a plugin is handed, client-side.
 *
 * Mirrors plexora/api/dataset.py so a plugin sees the same shape on both
 * sides: image data always, segmentation and a feature table when the project
 * has them, and a role->column map resolved from what the import wizard
 * recorded.
 *
 * The point is that plugins read ROLES, not column names. `schema.x` resolves
 * to whatever featureData[0].xCoordinate holds; a plugin that hardcodes "X"
 * breaks on the next dataset. Unrecognised keys survive in `extra`, so adding
 * a role later needs no plugin change.
 *
 * It also replaces the bare globals plugins used to reach for. csvGatingList
 * read `imageChannels`, `datasource` and `__plexora` straight off window,
 * which meant core could not rename or scope any of them, and a plugin loaded
 * out of order got undefined rather than an error.
 */
window.PlexoraDataset = (function () {
    // featureData[0] keys the import wizard writes, in role order. image_id is
    // deliberately last-resort: nothing in the upload form collects it yet, so
    // plugins must tolerate null.
    const ROLE_KEYS = {
        cellId: ["idField"],
        x: ["xCoordinate"],
        y: ["yCoordinate"],
        celltype: ["celltype"],
        imageId: ["imageId", "imageid", "image_id"],
    };

    // featureData[0] keys that are NOT column roles. `extra` exists so a role
    // added later still reaches plugins; without this it would also hand them
    // file paths and processing flags -- `src` is an absolute server path.
    const NON_ROLE_KEYS = new Set(["src", "celltypeData", "normalization", "isTransformed"]);

    function resolveSchema(config) {
        const spec = (config.featureData || [])[0];
        if (!spec) return null;
        const schema = { extra: {} };
        const known = new Set();
        for (const [role, keys] of Object.entries(ROLE_KEYS)) {
            keys.forEach((k) => known.add(k));
            schema[role] = keys.map((k) => spec[k]).find(Boolean) || null;
        }
        for (const [key, value] of Object.entries(spec)) {
            if (!known.has(key) && !NON_ROLE_KEYS.has(key) && typeof value === "string") {
                schema.extra[key] = value;
            }
        }
        return schema;
    }

    /**
     * @param config - this datasource's /config entry
     * @param imageChannels - fullname -> index lookup built by main.js
     * @param databaseDescription - per-column stats, or {} before it arrives
     */
    function build(config, imageChannels, databaseDescription = {}) {
        const schema = resolveSchema(config);

        // Reserved columns are numeric but are not markers. Kept in step with
        // the server's TableHandle.markers, which likewise reports only
        // columns a histogram could be built for.
        const reserved = new Set(["id"]);
        [schema?.cellId, schema?.x, schema?.y].forEach((c) => c && reserved.add(c));

        return {
            name: config.name || window.datasource,
            image: {
                channels: (config.imageData || []).filter((c) => c.fullname !== "Area"),
                get channelNames() {
                    return this.channels.map((c) => c.fullname);
                },
                kind: config.image_kind || null,
                index: imageChannels || {},
                /** Whether a marker name is also a real image channel. */
                has(fullName) {
                    return (imageChannels || {})[fullName] !== undefined;
                },
            },
            segmentation: {
                available: Boolean(config.segmentation),
                pending: config.segmentation_status === "pending",
            },
            table: {
                available: Boolean((config.featureData || []).length) && config.has_feature_data !== false,
                sourceKind: config.data_type || "csv",
                describe: databaseDescription,
                /**
                 * Columns a plugin can threshold or plot. Deliberately not
                 * image.channelNames -- a structural channel like DNA is a real
                 * image channel with no feature column, and conflating the two
                 * has caused bugs here.
                 */
                get markers() {
                    return Object.keys(databaseDescription).filter(
                        (column) => !reserved.has(column) && databaseDescription[column]?.histogram
                    );
                },
            },
            schema,
        };
    }

    return { build, resolveSchema };
})();

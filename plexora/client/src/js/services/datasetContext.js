/**
 * datasetContext.js - the dataset a plugin is handed, client-side.
 *
 * Mirrors plexora/api/dataset.py so a plugin sees the same shape on both
 * sides: image data always, segmentation and a feature table when the project
 * has them, and a role->column map read from the project record.
 *
 * The point is that plugins read ROLES, not column names. `schema.x` resolves
 * to whatever the project recorded for the x role; a plugin that hardcodes
 * "X_centroid" breaks on the next dataset. Roles this version has no field for
 * survive in `extra`, so adding one later needs no plugin change.
 *
 * It also replaces the bare globals plugins used to reach for. csvGatingList
 * read `imageChannels`, `datasource` and `__plexora` straight off window,
 * which meant core could not rename or scope any of them, and a plugin loaded
 * out of order got undefined rather than an error.
 */
window.PlexoraDataset = (function () {
    // Role name on the wire -> the camelCase name plugins read. The record
    // stores roles under one explicit key (server/models/project.py's
    // ROLE_NAMES), so unlike the old shape there is no alias list and no
    // denylist -- a role can no longer be confused with a file path or a
    // processing flag, because they are not in the same dict any more.
    const ROLE_KEYS = {
        cell_id: "cellId",
        x: "x",
        y: "y",
        celltype: "celltype",
        image_id: "imageId",
    };

    function resolveSchema(config) {
        const spec = config.dataset;
        if (!spec) return null;
        const roles = spec.roles || {};
        const schema = { extra: {} };
        for (const [wire, name] of Object.entries(ROLE_KEYS)) {
            schema[name] = roles[wire] || null;
        }
        // A role the record has learned but this version has no field for
        // still reaches plugins, so adding one needs no client change.
        for (const [wire, value] of Object.entries(roles)) {
            if (!ROLE_KEYS[wire] && typeof value === "string") schema.extra[wire] = value;
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
            // Getters, not values: a mask whose pyramid was still building when
            // the page opened is adopted in place when the job lands (main.js's
            // adoptSegmentation), and a plugin holding this object from before
            // that would otherwise go on being told there is no mask.
            segmentation: {
                get available() {
                    return Boolean(config.segmentation);
                },
                get pending() {
                    return config.segmentation_status === "pending";
                },
            },
            table: {
                // No dataset block IS the image-only state; there is no
                // separate flag that could disagree with it.
                available: Boolean(config.dataset),
                sourceKind: config.dataset?.type || "csv",
                describe: databaseDescription,
                /**
                 * Columns a plugin can threshold or plot -- the classification
                 * the project recorded at import, so every plugin sees the same
                 * answer. Deliberately not image.channelNames: a structural
                 * channel like DNA is a real image channel with no feature
                 * column, and conflating the two has caused bugs here.
                 *
                 * Narrowed to columns the server could describe, because that
                 * is what "can be thresholded" actually means: a gate needs a
                 * range and a histogram to draw, and a recorded marker the
                 * loaded table no longer holds has neither.
                 *
                 * The derivation is the fallback, not the answer. It cannot
                 * tell a stain from a measurement -- Area and Eccentricity are
                 * as numeric as CD3 is, and in a CSV they sit in the same
                 * header, which is the entire reason the import step asks. It
                 * covers a project whose columns were never classified; better
                 * a usable guess than an empty panel.
                 */
                get markers() {
                    const describable = (column) => Boolean(databaseDescription[column]?.histogram);
                    const recorded = (config.dataset?.columns?.markers || []).filter(describable);
                    if (recorded.length) return recorded;
                    return Object.keys(databaseDescription).filter(
                        (column) => !reserved.has(column) && describable(column)
                    );
                },
                get metadataColumns() {
                    return [...(config.dataset?.columns?.metadata || [])];
                },
            },
            schema,
        };
    }

    /**
     * Whether this project has per-cell positions to draw.
     *
     * Not the same question as "has a feature table": a table whose coordinate
     * columns nobody has identified yet has no positions either, and the
     * server's centroid manifest reports `missing` for both cases. Callers use
     * it to skip a centroid round trip that was never going to return points.
     */
    function hasCentroids(config) {
        const roles = config?.dataset?.roles;
        return Boolean(roles && roles.x && roles.y);
    }

    return { build, resolveSchema, hasCentroids };
})();

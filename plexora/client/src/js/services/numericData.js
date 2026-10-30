class NumericData {
  //: Columns kept by getColumn: enough for a multi-marker gate plus the
  //: marker being switched to, at ~5 MB each for a million cells.
  static COLUMN_CACHE_SIZE = 8;
  //: Markers kept by getNotCalled: the same depth as the columns, at a
  //: quarter of the size (one byte a cell).
  static NOT_CALLED_CACHE_SIZE = 8;

  /**
   * Constructor for NumericDataLayer.
   *
   * @param config - the cinfiguration file (json)
   * @param dataLayer - the data layer (stub) that executes server requests and holds client side data
   */
  constructor(config, dataLayer) {
      // Roles, not literal column names -- the same map plugins read.
      this.schema = PlexoraDataset.resolveSchema(config);
      this.dataLayer = dataLayer;
      this.cellsPromise = null;
      // Whole float32 columns by key, most recently used last. See getColumn.
      this.columns = new Map();
      // QC's not-called bytes by marker, kept the same way. See getNotCalled.
      this.notCalled = new Map();
  }

  /**
   * @function getColumn - one whole column as a Float32Array, aligned with
   * loadCells()' ids (the server sends both in table order).
   *
   * Cached, a few columns deep: the browser-side gate (ImageViewer.
   * evaluateGateLocally) reads the same marker on every tick of a drag, and a
   * column is 4 bytes per cell -- 5 MB at 1.26 M cells. The first tick on a
   * marker pays one request; the rest pay nothing. Concurrent callers share
   * one request. A failure is not cached.
   *
   * Values change only if the table does; invalidateColumns() is called where
   * that happens in a session (a refreshed dataset, cells mapped from ROIs).
   */
  getColumn(key) {
      const hit = this.columns.get(key);
      if (hit) {
          this.columns.delete(key);
          this.columns.set(key, hit);
          return hit;
      }
      const promise = this.getAllFloat32Entries([key]).then((values) => {
          if (!values || !values.length) throw new Error(`no values for ${key}`);
          return values;
      });
      promise.catch(() => {
          if (this.columns.get(key) === promise) this.columns.delete(key);
      });
      this.columns.set(key, promise);
      while (this.columns.size > NumericData.COLUMN_CACHE_SIZE) {
          this.columns.delete(this.columns.keys().next().value);
      }
      return promise;
  }

  /** Forget every cached column (the table's values changed). The not-called
   *  bytes go too: they are per row, and a table that changed may have
   *  changed its rows. */
  invalidateColumns() {
      this.columns = new Map();
      this.invalidateNotCalled();
  }

  /**
   * The rows are keyed or placed differently now (a cell-id or coordinate
   * role answered mid-session): read the roles again, and fetch the cells
   * afresh on the next loadCells(). Without this the gate went on colouring
   * the labels of the ids loaded first -- row numbers after the record said
   * global_cell_id, or the reverse (ImageLSP23362, 2026-10-28).
   */
  invalidateCells(config) {
      this.schema = PlexoraDataset.resolveSchema(config);
      this.cellsPromise = null;
      this.invalidateColumns();
  }

  /**
   * @function getNotCalled - which cells QC says cannot be called for
   * `marker`: one byte per table row, aligned with loadCells()' ids like a
   * column (1 = not called -- inside a QC region, or the marker flagged
   * unreliable there; 0 = called). All zeros without QC.
   *
   * Cached beside the columns and for the same reason (getColumn): a drag
   * asks for the gated markers on every tick. Concurrent callers share one
   * request. Unlike a column, a failure is not an exception: it resolves to
   * null -- nothing known to be not called -- so the gate still draws, and
   * it is not cached, so the next tick asks again.
   *
   * Stale whenever QC's calls move: a caller drops them with
   * invalidateNotCalled() on `plexora:qc-changed`. The viewer's gate does
   * not read them: it draws the threshold alone (ImageViewer.evaluateGateLocally).
   *
   * @returns {Promise<Uint8Array|null>}
   */
  getNotCalled(marker) {
      const hit = this.notCalled.get(marker);
      if (hit) {
          this.notCalled.delete(marker);
          this.notCalled.set(marker, hit);
          return hit;
      }
      const promise = this.fetchNotCalled(marker).catch(() => null).then((bytes) => {
          if (!bytes && this.notCalled.get(marker) === promise) this.notCalled.delete(marker);
          return bytes;
      });
      this.notCalled.set(marker, promise);
      while (this.notCalled.size > NumericData.NOT_CALLED_CACHE_SIZE) {
          this.notCalled.delete(this.notCalled.keys().next().value);
      }
      return promise;
  }

  async fetchNotCalled(marker) {
      const response = await fetch(plexoraUrl('get_not_called') + '?' + new URLSearchParams({
          datasource: datasource,
          marker: marker
      }));
      if (!response.ok) throw new Error(`get_not_called answered ${response.status}`);
      return new Uint8Array(await response.arrayBuffer());
  }

  /** Forget every marker's not-called bytes (QC's calls moved). */
  invalidateNotCalled() {
      this.notCalled = new Map();
  }

  /*
   * Load cell segmentation data
   */
  async loadCells() {
      if (this.cellsPromise) {
          return this.cellsPromise;
      }
      this.cellsPromise = this.fetchCells();
      return this.cellsPromise;
  }

  /**
   * @function hasCellTable - whether there are per-cell rows to fetch at all.
   *
   * A project can have a segmentation mask and no table: attach an image, then
   * attach a mask from the edit page, and that is exactly what you get. The
   * mask is drawable in that state -- renderLabelTile reads cell ids out of the
   * label pyramid itself and needs nothing from here -- so "no table" is an
   * ordinary project shape rather than a broken one.
   *
   * `schema` is null outright for a project with no data block, and carries
   * nulls for one whose roles nobody has answered yet. Both mean the same thing
   * to this class: there is no column to ask the server for.
   */
  hasCellTable() {
      const { cellId, x, y } = this.schema || {};
      return Boolean(cellId && x && y);
  }

  async fetchCells() {
      // Empty rather than an exception. This used to destructure `schema`
      // unguarded, so attaching a mask to an image-only project made the first
      // click on Outlines throw before the pyramid was ever requested -- the
      // mask never drew, and the reason surfaced as a TypeError about
      // 'cellId'. Everything downstream already handles no cells:
      // bindSegmentationBuffers returns early on empty and forceRepaint skips
      // loadBuffers while idCount is 0.
      if (!this.hasCellTable()) {
          return { ids: new Uint32Array(0), centers: new Uint32Array(0) };
      }
      const { cellId, x, y } = this.schema;
      const fields = [ cellId, x, y ];
      const idsCenters = await this.getAllUInt32Entries(fields);
      // Deinterleave in one linear pass instead of two full-array .filter()
      // passes (was O(2 * cellCount * fields.length)).
      const count = idsCenters.length / fields.length;
      const ids = new Uint32Array(count);
      const centers = new Uint32Array(count * (fields.length - 1));
      for (let i = 0, o = 0, c = 0; i < idsCenters.length; i += fields.length, o++) {
          ids[o] = idsCenters[i];
          for (let k = 1; k < fields.length; k++) {
              centers[c++] = idsCenters[i + k];
          }
      }
      return { ids, centers };
  }

  /*
   * Access DataLayer bitrange as floating point.
   */
  get floatRange() {
      return this.dataLayer.getImageBitRange(true);
  }

  /*
   * Access DataLayer bitrange as an integer.
   */
  get intRange() {
      return this.dataLayer.getImageBitRange(false);
  }

  /*
   * @function getNearestCell - return nearest cell to point
   *
   * @param x - cell x position in image coordinates
   * @param y - cell y position in image coordinates
   */
  getNearestCell(x, y) {
      return this.dataLayer.getNearestCell(x, y);
  }

  /*
   * @function getAllFloat32Ids - all integer entries
   * @param keys - list of keys to access
   */
  async getAllFloat32Entries(keys) {
      return this.getAllEntries(keys, false);
  }

  /*
   * @function getAllUInt32Ids - all integer entries
   * @param keys - list of keys to access
   */
  async getAllUInt32Entries(keys) {
      return this.getAllEntries(keys, true);
  }

  /*
   * @function getAllEntries - all cell entries by keys
   * @param keys - list of keys to access
   * @param useInt - whether requesting integers
   */
  async getAllEntries(keys, useInt) {
      if (!keys.length) {
          return [];
      }
      const { dataLayer } = this;
      const arr = await dataLayer.getAllCells(keys, useInt);
      if (useInt) {
          return new Uint32Array(arr);
      }
      return new Float32Array(arr);
  }
}

/**
 * `plexora:qc-changed`, raised on window whenever QC's calls may have moved: a
 * QC region drawn, traced, approved or deleted (in QC's panel or as an ROI), a
 * finding set aside, the strictness changed, or an agent doing any of that.
 * The not-called bytes above are stale from then on. One helper, so every writer -- QC's
 * client (qcApi.js), the ROI store's saves, the agent bridge -- raises the
 * same event with the same detail, `{datasource}`.
 */
const PLEXORA_QC_CHANGED = {
    EVENT: "plexora:qc-changed",
    announce(datasource) {
        window.dispatchEvent(new CustomEvent(PLEXORA_QC_CHANGED.EVENT, { detail: { datasource } }));
    },
};
if (typeof window !== "undefined") {
    window.PlexoraQcChanged = PLEXORA_QC_CHANGED;
}

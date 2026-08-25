class NumericData {
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

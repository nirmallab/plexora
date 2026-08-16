/**
 * @class CSVGatingList - A view to select and deselect channels for gating, and to set gates (filter ranges) in the tabular data
 */
class CSVGatingList {

    /**
     * @constructor
     * @param config the cinfiguration file (json)
     * @param columns - all the channel names
     * @param dataLayer - the data layer (stub) that executes server requests and holds client side data
     * @param eventHandler - the event handler for distributing interface and data updates
     */
    constructor(ctx) {
        // Everything arrives through the plugin context. This class used to
        // read `__plexora`, `imageChannels` and `datasource` straight off
        // window, which tied it to core's global names and to script order.
        this.ctx = ctx;
        this.config = ctx.config;
        this.columns = [...ctx.columns];
        this.databaseDescription = {};
        this.maxSelections = ctx.config.maxSelections;
        this.eventHandler = ctx.eventHandler;
        this.dataLayer = ctx.dataLayer;
        this.dataset = ctx.dataset;
        this.datasource = ctx.datasource;
        this.selections = {};
        this.hasGatingGMM = {};
        this.gatingIDs = {};
        this.sliders = new Map();
        this.container = d3.select("#csv_gating_list");
        // Gating vars
        this.global_channel_list = ctx.channelList;
        this.global_image_channels = ctx.dataset.image.index;
        this.gating_default_range = [0, 65536];
        this.gating_channels = this.initGatingChannels();
        this.gating_list = null;
        // Download vars
        this.download_panel_visible = false;
        this.download_input1 = null;
        this.download_input2 = null;
        // Eval settings
        this.eval_mode = 'and'
    }

    /**
    * Selects a channel as active and adds the respective visual components to the channel panel in the list view
    * @param name - the channel to set and display as selected
    */
    selectChannel(name) {
        const fullName = this.dataLayer.getFullChannelName(name);
        const values = this.gating_channels[fullName];
        this.selections[fullName] = values;
        this.sliders.get(name).value(values);
        this.eventHandler.trigger(CSVGatingList.events.GATING_BRUSH_MOVE, this.selections);
        if (!(name in this.hasGatingGMM)) {
            this.getAndDrawGatingGMM(name).then(() => {
                this.eventHandler.trigger(CSVGatingList.events.SELECTION_CHANGED, this.selections);
            });
        }
    }

    /**
    * Removes a channel form the current selection
    * @param name - the name of the channel to remove
    */
    removeChannel(name) {
        // Delete
        const fullName = this.dataLayer.getFullChannelName(name);
        delete this.selections[fullName];

        // Trigger
        this.eventHandler.trigger(CSVGatingList.events.SELECTION_CHANGED, this.selections);
    }

    /**
    * initializes the view (channel list)
    * @param dd - database description
    * @param seaDragonViewer - the ImageViewer instance
    * @returns {Promise<void>}
    */
    init(dd, seaDragonViewer) {
        this.databaseDescription = dd;
        this.seaDragonViewer = seaDragonViewer;
        document.getElementById('drag-and-drop-info').style.display = "none";
        // Hide the Loader
        document.getElementById('csv_gating_list_loader').style.display = "none";
        this.gating_list = document.getElementById("csv_gating_list");
        let list = document.createElement("ul");
        list.classList.add("list-group")
        list.setAttribute("id", "gating_list_ul")
        this.gating_list.appendChild(list)
        const gatingListEl = document.getElementById("csv_gating_list");
        const swidth = gatingListEl.getBoundingClientRect().width;
        // Will show the picker when you click on a color rect
        let showPicker = () => {
            this.colorTransfrHandle = d3.select(d3.event.target);
            let color = this.colorTransferHandle.style('fill');
            let hsl = d3.hsl(color);
            this.rainbow.show(d3.event.clientX, d3.event.clientY);
        };
        // Draws rows in the gating list
        // Gating markers are the feature table's own columns (e.g.
        // adata.var_names), never the image channel list -- an image
        // channel's display name (from OME-XML/antibody nomenclature) and
        // its feature-table column (from adata.var_names/gene symbols, or a
        // plain CSV header) are frequently different strings for the same
        // marker, and every gating query (get_gated_cells, get_gating_gmm)
        // already keys directly off the feature-table column, never the
        // image channel name. get_datasource_description() only attaches a
        // 'histogram' to columns it could build marker-expression stats
        // for, which is exactly the set of real, numeric feature columns --
        // id/X/Y are numeric too but aren't markers, so they're excluded
        // explicitly by name. Any correspondence to a specific image
        // channel (if ever needed) would be positional (marker i <-> image
        // channel i), not by matching names.
        // Roles, not literal column names -- schema.cellId/x/y resolve to
        // whatever this datasource actually recorded.
        const reservedColumns = new Set(['id']);
        const schema = this.dataset.schema || {};
        [schema.cellId, schema.x, schema.y].forEach(col => {
            if (col) reservedColumns.add(col);
        });
        this.columns = Object.keys(this.databaseDescription).filter(column => {
            return !reservedColumns.has(column) && this.databaseDescription[column].histogram;
        });
        _.each(this.columns, (column, index) => {

            let channelID = `channel_${index}`;
            this.gatingIDs[column] = channelID;
            // div for each row in gating list
            let listItemParentDiv = document.createElement("div");
            listItemParentDiv.classList.add("list-group-item");
            listItemParentDiv.classList.add("container");
            listItemParentDiv.classList.add("gating-list-content");
            // row
            let row = document.createElement("div");
            row.classList.add("row");
            listItemParentDiv.appendChild(row);
            // row
            let row2 = document.createElement("div");
            row2.classList.add("row");
            listItemParentDiv.appendChild(row2);

            // column within row that contains the name of the gating
            let nameCol = document.createElement("div");
            nameCol.classList.add("col-md-4");
            nameCol.classList.add("gating-col");
            row.appendChild(nameCol);

            // column within row that cintains the slider for the gating
            let sliderCol = document.createElement("div");
            sliderCol.classList.add("col-md-12");
            sliderCol.classList.add("csv_gating-slider");
            sliderCol.setAttribute('id', "csv_gating-slider_" + channelID)
            row2.appendChild(sliderCol);

            // column within row that contains svg for color pickers
            let svgCol = document.createElement("div");
            svgCol.classList.add("col-md-4");
            svgCol.classList.add("ms-auto");
            svgCol.classList.add("gating-col");
            svgCol.classList.add("gating-svg-wrapper");
            svgCol.classList.add("col-svg-wrapper");
            row.appendChild(svgCol);


            let svg = d3.select(svgCol)
                .append("svg")
                .attr("width", 30)
                .attr("height", 15)
            svgCol.style.display = "none";

            let gatingName = document.createElement("span");
            gatingName.classList.add('gating-name');
            gatingName.classList.add('list-button');
            gatingName.textContent = column;
            nameCol.appendChild(gatingName);
            listItemParentDiv.addEventListener("click", e => {
                return this.toggleChannelPanel(e, svgCol);
            })
            list.appendChild(listItemParentDiv);

            //add and hide gating sliders (will be visible when gating is active)
            const fullName = this.dataLayer.getFullChannelName(column);
            const sliderRange = [this.databaseDescription[fullName].min, this.databaseDescription[fullName].max];
            this.gating_channels[fullName] = sliderRange;
            const gatingListEl = document.getElementById("csv_gating_list");
            const swidth = gatingListEl.getBoundingClientRect().width;
            this.addSlider(column, swidth, sliderRange, sliderRange);
            d3.select('div#csv_gating-slider_' + channelID).style('display', "none");

            let autoCol = document.createElement("div");
            autoCol.classList.add("col-md-4");
            autoCol.classList.add("ms-auto");
            autoCol.classList.add("csv_gating-auto")
            autoCol.setAttribute('id', "csv_gating-auto_" + channelID)
            autoCol.classList.add("gating-col");
            autoCol.classList.add("gating-svg-wrapper");
            row.appendChild(autoCol);

            let autoBtn = document.createElement("button");
            autoBtn.classList.add('auto-btn');
            autoBtn.classList.add('auto-loading');
            autoBtn.setAttribute('id', "auto-btn-gating_" + channelID);
            autoBtn.textContent = "auto";
            autoBtn.addEventListener("click", async () => {
                const shortName = this.dataLayer.getShortChannelName(fullName);
                await this.autoGate(shortName);
            });

            autoCol.appendChild(autoBtn);
            autoBtn.addEventListener("click", e => e.stopPropagation());
            d3.select(autoCol).style('display', "none");
        });

        var dropzone = new Dropzone("#csv_gating_list", {
            url: this.ctx.url("plugins/gating/upload_gates"),
            clickable: false,
            disablePreview: true,
            createImageThumbnails: false
        });
        dropzone.on("sending", (file, xhr, formData) => {
            formData.append("datasource", this.datasource);
        });
        dropzone.on("queuecomplete", (file, xhr, formData) => {
            return this.applyGates()
        });

        // Adding upload when you press on the up arrow
        let arrow = document.getElementById('gating_upload_icon')
        arrow.onclick = () => {
            let elem = document.getElementById('gating-upload-from-arrow');
            if (elem && document.createEvent) {
                let evt = document.createEvent("MouseEvents");
                evt.initEvent("click", true, false);
                elem.dispatchEvent(evt);
            }
        }
        document.getElementById("gating-upload-from-arrow").onchange = async () => {
            if (document.getElementById("gating-upload-from-arrow").files) {
                let file = document.getElementById("gating-upload-from-arrow").files[0]
                let formData = new FormData();
                formData.append("file", file);
                await this.dataLayer.submitGatingUpload(formData);
                document.getElementById("gating-upload-from-arrow").value = []
                await this.applyGates('file')
            }
        }


        // Adding dropzone for CSV_Gating_List
        let parent = document.getElementById('csv_gating_list');
        let rect = parent.getBoundingClientRect();
        parent.addEventListener("dragover", (ev) => {
            document.getElementById('gating_list_ul').style.display = "none";
            document.getElementById('drag-and-drop-info').style.display = "block";
        })
        parent.addEventListener("dragleave", (ev) => {
            if (ev.x > rect.left + rect.width || ev.x < rect.left
                || ev.y > rect.top + rect.height || ev.y < rect.top) {
                document.getElementById('gating_list_ul').style.display = "block";
                document.getElementById('drag-and-drop-info').style.display = "none";
            }
        })
        parent.addEventListener("drop", (ev) => {
            document.getElementById('gating_list_ul').style.display = "block";
            document.getElementById('drag-and-drop-info').style.display = "none";
        })

        // Add events
        this.addDownloadEvents();
        this.addEventsLinked();
    }

    /**
    * @function applyGates
    * Applies settings (from file or db) to the gates in the tool
    * @parms {String} source Whether it is from new file upload or saved
    */
    async applyGates(source) {
        let gates;
        if (source === 'file') {
            gates = await this.dataLayer.getUploadedGatingCsvValues();
        } else {
            gates = await this.dataLayer.getSavedGatingList();
        }

        this.eventHandler.trigger(CSVGatingList.events.RESET_GATINGLIST)
        _.each(gates, async (col) => {
            if (col.channel == 'Lasso') {
                // Lasso-drawn regions are no longer supported; ignore any
                // legacy 'Lasso' rows from older saved/exported gate lists.
            } else {
                let shortName = this.dataLayer.getShortChannelName(col.channel);
                let channelID = this.gatingIDs[shortName];
                if (this.sliders.get(shortName)) {
                    let toggle_off
                    if (!col.gate_active && col.channel in this.selections) {
                        toggle_off = true;
                    } else {
                        toggle_off = false;
                    }
                    this.gating_channels[col.channel] = [col.gate_start, col.gate_end];
                    if (col.gate_active) {
                        // IF the channel isn't active, make it so
                        if (!this.selections[col.channel]) {
                            let selector = `#csv_gating-slider_${channelID}`;
                            document.querySelector(selector).click();
                        }
                        this.selections[col.channel] = [col.gate_start, col.gate_end];

                        // Update the slider values to reflect the new gate
                        const slider = this.sliders.get(shortName);
                        if (slider) {
                            // Apply the same data type conversion as in addSlider
                            const fullName = this.dataLayer.getFullChannelName(shortName);
                            const channelRange = [this.databaseDescription[fullName].min, this.databaseDescription[fullName].max];
                            const factor = Math.pow(10, this.dataLayer.gateDecimals(channelRange));
                            const v0 = Math.floor(parseFloat(col.gate_start) * factor) / factor;
                            const v1 = Math.ceil(parseFloat(col.gate_end) * factor) / factor;

                            slider.silentValue([v0, v1]);
                            // Update the input fields
                            d3.select('#gating_slider-input_' + channelID + '_0').attr('value', v0);
                            d3.select('#gating_slider-input_' + channelID + '_0').property('value', v0);
                            d3.select('#gating_slider-input_' + channelID + '_1').attr('value', v1);
                            d3.select('#gating_slider-input_' + channelID + '_1').property('value', v1);
                        }

                    } else {
                        // If channel is currently active, but shouldn't be, update it
                        if (toggle_off) {
                            let selector = `#csv_gating-slider_${channelID}`;
                            document.querySelector(selector).click();
                        }
                        delete this.selections[col.channel];
                    }
                }
            }
        })
        // Trigger brush
        this.eventHandler.trigger(CSVGatingList.events.SELECTION_CHANGED, this.selections);
    }

    /**
     * @function autoGate - applies thresholds based on Gaussian Mixture Model
     * @param name - the name of the channel to apply it to
     */
    async autoGate(shortName) {
        const fullName = this.dataLayer.getFullChannelName(shortName);
        const input = this.hasGatingGMM[shortName]['gate'].toFixed(7);
        const channelRange = [this.databaseDescription[fullName].min, this.databaseDescription[fullName].max];
        const factor = Math.pow(10, this.dataLayer.gateDecimals(channelRange));
        const gate = Math.floor(parseFloat(input) * factor) / factor;
        if (fullName in this.selections) {
            const channelID = this.gatingIDs[shortName];
            const gate_end = this.selections[fullName][1];
            const slider = this.sliders.get(shortName);
            const values = [gate, gate_end];
            this.moveSliderHandles(slider, values, shortName, 'SELECTION_CHANGED');
            d3.select('#gating_slider-input_' + channelID + '_0').attr('value', gate)
            d3.select('#gating_slider-input_' + channelID + '_0').property('value', gate);
        }
    }

    /**
     * @function initGatingChannels - creates the data structure for channels
     * @return obj - allChannels and their default range
     */
    initGatingChannels() {

        // Init
        const obj = {};

        // Iterate to create fields
        for (let key in this.global_image_channels) {

            obj[key] = this.gating_default_range;
        }

        // Return
        return obj;
    }

    /**
     * @function toggleChannelPane - expands or collapses a channel panel in the list that was clicked on
     * @param event - the click vent
     * @param svgCol - the column to expand or collapse
     */
    toggleChannelPanel(event, svgCol) {

        // If you clicked on the svg, ignore this behavior
        if (event.target.closest("svg")) {
            return;
        }

        // Get info
        let parent = event.target.closest(".list-group-item");
        let name = parent.querySelector('.gating-name').textContent;
        let channelID = this.gatingIDs[name];
        let status = !parent.classList.contains("active");

        // If active - else inactive
        if (status) {

            // Clear everything
            // clearOut();

            // Don't add gating is the max are selected
            if (_.size(this.selections) >= this.maxSelections) {
                return;
            }

            // Update properties and add slider
            d3.select(parent).classed("active", true);
            svgCol.style.display = "block";
            d3.select('div#csv_gating-slider_' + channelID).style('display', "block")
            d3.select('div#csv_gating-auto_' + channelID).style('display', "block");

            // Add channel
            this.selectChannel(name);

        } else {
            // Clear panel visibility
            // clearOut();

            // Remove channel and rerender
            this.removeChannel(name);

            // Hide
            d3.select(parent).classed("active", false);
            svgCol.style.display = "none";
            d3.select('div#csv_gating-slider_' + channelID).style('display', "none")
            d3.select('div#csv_gating-auto_' + channelID).style('display', "none");
        }

        let selectionsHeaderDiv = document.getElementById("csv_selected-gatings-header-div");
        if (selectionsHeaderDiv) {
            if (_.size(this.selections) >= this.maxSelections) {
                selectionsHeaderDiv.classList.add('bold-selections-header');
            } else {
                selectionsHeaderDiv.classList.remove('bold-selections-header');
            }
            document.getElementById("csv_num-selected-gatings").textContent = _.size(this.selections);

            // Trigger event
            const packet = { selections: this.selections, name, status };
            this.eventHandler.trigger(CSVGatingList.events.GATING_CHANNELS_CHANGE, packet);

        }
    }

    /**
     * @function addDownloadEvents - adds eventl listeners an functionality to the download buttons
     */
    addDownloadEvents() {

        // Els
        const gating_download_icon = document.querySelector('#gating_download_icon');
        const gating_download_panel = document.querySelector('#gating_download_panel');
        const gating_exit = document.querySelector('#gating_exit');
        const download_gated_channel_ranges = document.querySelector('#download_gated_channel_ranges');
        const download_gated_cell_encodings = document.querySelector('#download_gated_cell_encodings');
        const download_input1 = document.querySelector('#download_input1');
        const download_input2 = document.querySelector('#download_input2');

        // Events ::

        // Open / close download panel
        gating_download_icon.addEventListener('click', () => {
            // Update class var
            this.download_panel_visible = !this.download_panel_visible;
            // Condition to update download panel visibility
            if (this.download_panel_visible) {
                gating_download_panel.style.visibility = 'visible';
            } else {
                gating_download_panel.style.visibility = 'hidden';
            }
        });

        // Close download panel
        gating_exit.addEventListener('click', () => {
            // Update class var
            this.download_panel_visible = !this.download_panel_visible;
            // Hide download panel
            gating_download_panel.style.visibility = 'hidden';
        });

        // Download gated channel ranges
        download_gated_channel_ranges.addEventListener('click', () => {
            this.dataLayer.downloadGatingCSV(this.gating_channels, this.selections, false);
        })

        // Download gated channel ranges
        download_gated_cell_encodings.addEventListener('click', () => {
            this.dataLayer.downloadGatingCSV(this.gating_channels, this.selections, this.seaDragonViewer.pickedIds, true);
        })

    }


    /**
     * @function addEventsLinked
     * ???
     */
    addEventsLinked() {

        // Add events to channel
        const channelListContent = document.querySelectorAll('.channel-list-content');
        const gatingListContent = document.querySelectorAll('.gating-list-content');

        // Attach
        // Gating markers (adata.var_names) and image channels are matched
        // by name only when the strings happen to coincide -- the two lists
        // are frequently different lengths (e.g. structural channels like
        // DNA/AF1 have no marker, or var_names uses gene symbols that don't
        // match the image's antibody-named channels), so this can never be
        // a positional/index link. When a gating marker is selected and its
        // exact name also appears in the image channel list, auto-open that
        // channel for convenience; when there's no match, nothing happens
        // and the user opens the right channel manually.
        const attach = (targets, matches, target_class, match_class) => {
            targets.forEach(cLC => {
                cLC.addEventListener('click', e => {

                    // If event target is not an svg el (from slider)
                    const svgEls = ['path']
                    if (!svgEls.includes(e.currentTarget.tagName)) {

                        // Get channel name
                        const name = _.get(e.currentTarget.querySelector(`.${target_class}`), 'innerText');

                        // Find match el in channel list
                        if (name) {
                            const match = Array.from(matches).find(
                                row => row.querySelector(`.${match_class}`).innerText === name);

                            // Real click so it goes through ChannelList's own
                            // toggleChannelPanel handler (already bound per-row
                            // at row-creation time) -- no need to reconstruct
                            // its event/closure state here.
                            if (match && !Array.from(match.classList).includes('active')) {
                                match.click();
                            }
                        }
                    }
                });
            });
        }
        attach(gatingListContent, channelListContent, 'gating-name', 'channel-name');

    }

    /**
    * @function addSlider - add a slider
    * @param data - the min and max range of the slider
    * @param activeRange - the predefined values for the lower and upper handle
    * @param name - the name of the slider (used as part of the id)
    * @param swidth - the pixel width of the slider
     */
    addSlider(name, swidth, data, activeRange) {

        if (!data) return;

        const fullName = this.dataLayer.getFullChannelName(name);
        const { xDomain, yDomain, histogramData } = this.histogramData(fullName);
        let channelID = this.gatingIDs[name];

        // data is already [min, max] (see call sites) -- gateDecimals derives
        // slider precision from that observed span instead of the old
        // isTransformed config flag (see dataLayer.gateDecimals for why).
        const channelRange = [d3.min(data), d3.max(data)];
        const gateFactor = Math.pow(10, this.dataLayer.gateDecimals(channelRange));
        const data_min = Math.floor(channelRange[0] * gateFactor) / gateFactor;
        const data_max = Math.ceil(channelRange[1] * gateFactor) / gateFactor;
        const handle_min = Math.floor(parseFloat(activeRange[0]) * gateFactor) / gateFactor;
        const handle_max = Math.ceil(parseFloat(activeRange[1]) * gateFactor) / gateFactor;
        let f = d3.format("d")
        //add range slider row content
        const sliderSimple = d3.sliderBottom()
            .min(data_min)
            .max(data_max)
            .width(swidth - 75)
            .tickFormat(f)
            .fill('orange')
            .ticks(1)
            .default([handle_min, handle_max])
            .handle(
                d3.symbol()
                    .type(d3.symbolCircle)
                    .size(100))
            .tickValues([])
            .on('end', (range) => {
                const v0 = Math.floor(parseFloat(range[0]) * gateFactor) / gateFactor;
                const v1 = Math.ceil(parseFloat(range[1]) * gateFactor) / gateFactor;
                this.moveSliderHandles(sliderSimple, [v0, v1], name, "SELECTION_CHANGED");
            }).on('onchange', (range) => {
                const v0 = Math.floor(parseFloat(range[0]) * gateFactor) / gateFactor;
                const v1 = Math.ceil(parseFloat(range[1]) * gateFactor) / gateFactor;
                d3.select('#gating_slider-input_' + channelID + '_0').attr('value', v0)
                d3.select('#gating_slider-input_' + channelID + '_0').property('value', v0);
                d3.select('#gating_slider-input_' + channelID + '_1').attr('value', v1);
                d3.select('#gating_slider-input_' + channelID + '_1').property('value', v1);
                this.moveSliderHandles(sliderSimple, [v0, v1], name, "GATING_BRUSH_MOVE");
            });

        this.sliders.set(name, sliderSimple);

        //create the slider svg and call the slider
        var gSimple = d3
            .select('#csv_gating-slider_' + channelID)
            .append('svg')
            .attr('class', 'svgslider')
            .attr('id', 'csv_gating-slider_svg_' + channelID)
            .attr('width', swidth)
            .attr('height', 80)
            .append('g')
            .attr('transform', 'translate(20,40)');

        let xScale = d3.scaleLinear()
            .domain(xDomain)
            .range([0, swidth - 73])

        let yScale = d3.scaleLinear()
            .domain(yDomain)
            .range([0, 25])

        let line = d3.line()
            .x(d => xScale(d.x))
            .y(d => yScale(d.y))
            .curve(d3.curveMonotoneX)

        const lines = gSimple.selectAll('.distribution_line');
        const paths = lines.data([histogramData]).enter().append('path');
        paths
            .append('path')
            .attr('d', line)
            .attr('class', 'distribution_line')
            .attr('transform', 'translate(0,-31)')
            .attr('fill', 'none')

        gSimple.call(sliderSimple);

        //slider value to be displayed closer to the slider than default
        d3.selectAll('.parameter-value').select('text')
            .attr("y", 10);

        //both handles
        const { sliders } = this;
        const handles = d3.select('#csv_gating-slider_' + channelID).selectAll(".parameter-value");
        handles.each(function (d, i) {
            d3.select(this).append("foreignObject")
                .attr('id', 'c_foreignObject_' + channelID + i)
                .attr("width", 50)
                .attr("height", 40)
                .attr('x', -25)
                .attr('y', -17)
                .style('padding', "10px")
                .append("xhtml:body")
                .attr('xmlns', 'http://www.w3.org/1999/xhtml')
                .style('background', 'none')
                .append('input')
                .attr('y', -17)
                .attr('id', 'gating_slider-input_' + channelID + '_' + i)
                .attr('type', 'text')
                .attr('class', 'input')
                .attr('value', () => {
                    return sliders.get(name).value()[i]
                });
            //remove the previous text label
            d3.select(this).select('text').remove();
        });

        //entering a value in the input field of a slider handle
        const moveSliderHandles = this.moveSliderHandles.bind(this);
        handles.selectAll('.input').on('keydown', function (event, d) {
            // Note: `this` here is the DOM <input> (d3's `function` handler
            // convention, needed below for `this.value`) -- gateFactor is
            // captured from the enclosing addSlider closure instead of
            // going through `this.dataLayer`, which doesn't exist on a DOM
            // node (a pre-existing bug: this handler would have thrown on
            // every Enter keypress before gateFactor existed to close over).
            if (event.key == "Enter") {
                const val = Math.round(parseFloat(this.value.replace("%", "")) * gateFactor) / gateFactor;
                const vals = sliderSimple.silentValue();
                vals[d.index] = val;
                moveSliderHandles(sliderSimple, vals, name, "SELECTION_CHANGED");
            }
        })

        return sliderSimple;
    };

    histogramData(fullName) {
        const histogramData = this.databaseDescription[fullName].histogram;
        const xMin = Math.floor(Math.min(...histogramData.map(e => e.x)));
        const xMax = Math.ceil(Math.max(...histogramData.map(e => e.x)));
        const yMax = Math.max(...histogramData.map(e => e.y));
        return {
            histogramData,
            xDomain: [xMin, xMax],
            yDomain: [yMax, 0],
        };
    }

    async getGatingGMM(name, selection_ids = []) {
        const fullName = this.dataLayer.getFullChannelName(name);
        let packet = await this.dataLayer.getGatingGMM(fullName, selection_ids);
        this.hasGatingGMM[name] = packet;
        return packet;
    }

    drawGatingGMM(name) {
        let channelID = this.gatingIDs[name];
        const fullName = this.dataLayer.getFullChannelName(name);
        const { xDomain, yDomain } = this.histogramData(fullName);
        const packet = this.hasGatingGMM[name];
        let gmm1Data = packet['gmm_1'];
        let gmm2Data = packet['gmm_2'];

        const gatingListEl = document.getElementById("csv_gating_list");
        const swidth = gatingListEl.getBoundingClientRect().width;

        let xScale = d3.scaleLinear()
            .domain(xDomain)
            .range([0, swidth - 73])

        let yScale = d3.scaleLinear()
            .domain(yDomain)
            .range([0, 25])

        let line = d3.line()
            .x(d => xScale(d.x))
            .y(d => yScale(d.y))
            .curve(d3.curveMonotoneX)

        let gSimple = d3.select('#csv_gating-slider_svg_' + channelID + ' g')

        gSimple.selectAll('.gmm1_line')
            .data([gmm1Data])
            .enter()
            .append('path')
            .attr('d', line)
            .attr('class', 'gmm_line')
            .attr('class', 'gmm_line_' + name)
            .attr('id', 'gmm1_line_' + name)
            .attr('transform', 'translate(0,-31)')
            .attr('fill', 'none')
            .attr('stroke', 'blue')

        gSimple.selectAll('.gmm2_line')
            .data([gmm2Data])
            .enter()
            .append('path')
            .attr('d', line)
            .attr('class', 'gmm_line')
            .attr('class', 'gmm_line_' + name)
            .attr('id', 'gmm2_line_' + name)
            .attr('transform', 'translate(0,-31)')
            .attr('fill', 'none')
            .attr('stroke', 'red')
    }

    async getAndDrawGatingGMM(name) {
        await this.getGatingGMM(name);

        const channelID = this.gatingIDs[name];
        const autoBtn = document.getElementById(`auto-btn-gating_${channelID}`);
        autoBtn.classList.remove("auto-loading")

        this.drawGatingGMM(name);
    }

    /**
     * @function resetGatingList - resets all channels in the list to its initial range
     */
    resetGatingList() {
        let gatingList = Object.keys(this.selections);
        _.each(gatingList, col => {
            let shortName = this.dataLayer.getShortChannelName(col);
            let channelID = this.gatingIDs[shortName];
            let gating_selector = `#csv_gating-slider_${channelID}`;
            document.querySelector(gating_selector).click();
        });
    };

    /**
     * @function moveSliderHandles - move the slider handles and input fields so that input fields don't overlap when handles are close
     *
     * @param slider - the slider affected
     * @param vals - holds the new positions
     * @param name - the name of the slider
     * @param eventName - the name of the event
     */
    moveSliderHandles(slider, vals, name, eventName) {
        const fullName = this.dataLayer.getFullChannelName(name);
        const channelID = this.gatingIDs[name];
        this.gating_channels[fullName] = vals;
        this.selections[fullName] = vals;
        slider.silentValue(vals);
        const diff = Math.abs(vals[1] - vals[0]);
        const total = Math.abs(slider.max() - slider.min());
        const percentage = diff / total;
        if (percentage < 0.15) {
            console.log('slider handles overlap..do something');
            d3.select('#c_foreignObject_' + channelID + 1).attr('x', 5);
        } else {
            d3.select('#c_foreignObject_' + channelID + 1).attr('x', -25);
        }
        const packet = this.selections;
        this.eventHandler.trigger(CSVGatingList.events[eventName], packet);
    }

    /**
     * @function dist - caclulates the distance between two rects
     * @param el1
     * @param el2
     * @param buffer
     * @returns {number}
     */
    dist(el1, el2, buffer) {
        var rect1 = el1.getBoundingClientRect();
        var rect2 = el2.getBoundingClientRect();
        return rect2.left - rect1.right;
    }

    // ==== ImageViewer selectionProvider contract (see imageViewer.js) ====
    // Replaces ImageViewer's former direct dataLayer.getGatedCellIds() calls --
    // the gating-specific "gates" shape now stays entirely on this side of the seam.

    /**
     * @function rebuildSliders - re-measure and redraw every legacy-list slider.
     *
     * Slider widths come from a measured bounding box, so a window resize
     * invalidates them. Called by the resize listener the plugin registers in
     * bindEvents(), which is torn down with the plugin -- this used to be a
     * script-scope listener reading a global, with no way to detach it.
     */
    rebuildSliders() {
        const gatingListEl = document.getElementById("csv_gating_list");
        if (!gatingListEl) return;
        const swidth = gatingListEl.getBoundingClientRect().width;
        this.sliders.forEach((slider, name) => {
            const gatingID = this.gatingIDs[name];
            d3.select('div#csv_gating-slider_' + gatingID).select('svg').remove();
            const fullName = this.dataLayer.getFullChannelName(name);
            const sliderRange = [this.databaseDescription[fullName].min, this.databaseDescription[fullName].max];
            this.addSlider(name, swidth, sliderRange, slider.value());
            if (this.hasGatingGMM[name]) {
                this.drawGatingGMM(name);
            }
        });
    }

    /**
     * @function getSelectedIds - resolve a gates filter to matching cell ids
     * @param filter - gates dict ({channel: [min, max]}); defaults to the
     *   currently active selections when omitted
     * @returns {Promise<Set<number>>}
     */
    async getSelectedIds(filter) {
        const gates = filter || this.selections;
        const { idField } = this.config.featureData[0];
        const rows = await this.dataLayer.getGatedCellIds(gates, [idField]);
        if (!Array.isArray(rows)) return new Set();
        return new Set(rows.map((row) => Number(row[idField] ?? row.id ?? row.CellID)));
    }

    /**
     * @function supportsColorCoding - gating owns the multi-range colorized
     * rendering path (u_cell_range_shape/texture_ranges); always true here.
     * @returns {boolean}
     */
    supportsColorCoding() {
        return true;
    }

    /**
     * @function getColorCodedRanges - per-channel gate ranges for the
     * colorized rendering path.
     * @returns {object}
     */
    getColorCodedRanges() {
        return this.selections;
    }
}


//hide gating control panel when scrolled down to access all channels..
// $(document).ready(function()
// {
//    $('#csv_gating_list').scroll(function()
//    {
//       var div = $(this);
//       if (div[0].scrollHeight - div.scrollTop() < div.height()+10)
//       {
//             $('#seg_controls_panel').hide();
//       }else{
//             $('#seg_controls_panel').show();
//       }
//    });
// });

//static vars: events introduced in this class and used across the app
CSVGatingList.events = {
    GATING_BRUSH_MOVE: "GATING_BRUSH_MOVE",
    SELECTION_CHANGED: "SELECTION_CHANGED",
    GATING_COLOR_TRANSFER_CHANGE_MOVE: "GATING_TRANSFER_CHANGE_MOVE",
    GATING_COLOR_TRANSFER_CHANGE: "GATING_TRANSFER_CHANGE",
    GATING_CHANNELS_CHANGE: "GATING_CHANNELS_CHANGE",
    RESET_GATINGLIST: "RESET_GATINGLIST"
};

// Self-registers the gating plugin (see pluginRegistry.js for the shape and main.js
// for the call sites). Only ever loaded when gating's tool is open for the current
// page view, so this always runs alongside GatingSidebarController.
if (window.Plexora) {
    window.Plexora.registerPlugin({
        name: "gating",
        // Gating colours cells by marker threshold, so it claims the viewer's
        // single cell layer. See ImageViewer.claimCellLayer.
        ownsCellLayer: true,
        createInstance(ctx) {
            return new CSVGatingList(ctx);
        },
        createSidebarController(ctx) {
            return new GatingSidebarController(ctx);
        },
        bindEvents(ctx) {
            const { eventHandler, moduleInstance, seaDragonViewer, updateSeaDragonSelection, updateCentroidsForGate, runSegmentationGate, onCleanup } = ctx;
            // The legacy list's sliders are sized from measured widths, so they
            // need rebuilding on resize. Registered through onCleanup so
            // deactivating the plugin actually removes it -- this listener used
            // to be attached at script scope with no way to detach.
            const onResize = () => moduleInstance?.rebuildSliders?.();
            window.addEventListener("resize", onResize);
            onCleanup?.(() => window.removeEventListener("resize", onResize));
            eventHandler.bind(CSVGatingList.events.SELECTION_CHANGED, () => {
                updateSeaDragonSelection();
                updateCentroidsForGate();
                runSegmentationGate(true);
            });
            eventHandler.bind(CSVGatingList.events.GATING_BRUSH_MOVE, () => {
                updateSeaDragonSelection();
                runSegmentationGate(false);
            });
            eventHandler.bind(CSVGatingList.events.RESET_GATINGLIST, () => {
                moduleInstance.resetGatingList();
                seaDragonViewer.forceRepaint();
            });
            eventHandler.bind(ctx.coreEvents.RESET_LISTS, () => {
                moduleInstance.resetGatingList();
            });
        },
    });
}

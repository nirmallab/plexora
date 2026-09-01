import {Buffer} from 'buffer/';
import {PNG} from 'pngjs'
import UPNG from 'upng-js'
import 'bootstrap'
import 'bootstrap/dist/css/bootstrap.min.css';
import 'pngjs'
import "regenerator-runtime";
import * as d3 from 'd3';
import {sliderBottom} from 'd3-simple-slider';
import 'lodash'
import '@fortawesome/fontawesome-free/js/all'
import Sortable from 'sortablejs';
import OpenSeadragon from 'openseadragon';
import {jsPDF} from 'jspdf';
import {ViewerManager, RGB_TILE_FORMAT} from './views/viewerManager';
import {GLRenderer} from './services/glRenderer';
import Dropzone from 'dropzone';

window.d3 = d3;
window.d3.sliderBottom = sliderBottom;
window.PNG = PNG;
window.UPNG = UPNG;
window.Buffer = Buffer;
window.Sortable = Sortable;
window.OpenSeadragon = OpenSeadragon;
window.jsPDF = jsPDF;
window.Dropzone = Dropzone;
window.ViewerManager = ViewerManager;
// imageViewer.js is served straight from client/src and is not part of this
// bundle, so the one constant the two share has to cross here. See
// viewerManager.js for what 24 means.
window.RGB_TILE_FORMAT = RGB_TILE_FORMAT;
window.GLRenderer = GLRenderer;

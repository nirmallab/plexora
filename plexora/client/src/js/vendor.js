import {Buffer} from 'buffer/';
import {PNG} from 'pngjs'
import UPNG from 'upng-js'
import 'jquery'
import 'bootstrap'
import 'bootstrap/dist/css/bootstrap.min.css';
import 'pngjs'
import {regeneratorRuntime} from "regenerator-runtime";
import * as d3 from 'd3';
import {sliderBottom} from 'd3-simple-slider';
import 'lodash'
import '@fortawesome/fontawesome-free/js/all'
import Sortable from 'sortablejs';
import Mark from 'mark.js'
import $ from 'jquery'
import OpenSeadragon from 'openseadragon';
import {ViewerManager} from './views/viewerManager';
import {GLRenderer} from './services/glRenderer';
import Dropzone from 'dropzone';

window.$ = $;
window.d3 = d3;
window.d3.sliderBottom = sliderBottom;
window.PNG = PNG;
window.UPNG = UPNG;
window.Buffer = Buffer;
window.Sortable = Sortable;
window.Mark = Mark;
window.OpenSeadragon = OpenSeadragon;
window.Dropzone = Dropzone;
window.ViewerManager = ViewerManager;
window.GLRenderer = GLRenderer;

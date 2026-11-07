// The website's browser node worker (workers/tiffWorker.js): geotiff.js and the
// TIFF reader in one worker script, built apart from vendor.js so neither
// bundle's chunk numbering moves when the other changes. geotiff.js imports its
// codecs on demand; they are folded into the one file, which every worker of
// the pool loads from the HTTP cache after the first.
const webpack = require('webpack')
const base = require('./webpack.config.js')

module.exports = {
    ...base,
    entry: {
        tiffWorker: './src/js/workers/tiffWorker.js'
    },
    target: 'webworker',
    performance: { hints: false },
    plugins: [
        ...base.plugins,
        new webpack.optimize.LimitChunkCountPlugin({ maxChunks: 1 })
    ]
};

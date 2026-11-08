// The website's SSH client (src/js/ssh/sshEntry.js): Microsoft's
// dev-tunnels-ssh and the few lines that drive it, in one file of its own.
// Built apart from vendor.js so neither bundle's chunk numbering moves when the
// other changes, and loaded only when a connection starts (services/
// webRemote.js), never with a page. Minified: it is about a megabyte of
// library before that.
//
//     cd plexora/client && npx webpack --config webpack.ssh.config.js
const webpack = require('webpack')
const base = require('./webpack.config.js')

module.exports = {
    ...base,
    mode: 'production',
    entry: {
        ssh: './src/js/ssh/sshEntry.js'
    },
    performance: { hints: false },
    resolve: {
        ...base.resolve,
        fallback: {
            ...base.resolve.fallback,
            // Node modules the library names but never reaches in a browser:
            // its Node crypto and socket paths are chosen at run time.
            crypto: false,
            net: false,
            os: false,
            path: false,
            'node-rsa': false,
        }
    },
    plugins: [
        ...base.plugins,
        new webpack.optimize.LimitChunkCountPlugin({ maxChunks: 1 })
    ]
};

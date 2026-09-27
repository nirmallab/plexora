thinking-orbs 0.3.2 -- vendored engine chunk
=============================================

Origin   : npm package "thinking-orbs" 0.3.2 (MIT, (c) Jakub Antalik)
           https://www.npmjs.com/package/thinking-orbs
           https://github.com/Jakubantalik/Libraries.dev  (demo: https://libraries.dev/orbs)
Obtained : `npm pack thinking-orbs@0.3.2`, 2026-09-26
File     : orbs.js is dist/index-B8WsUNf5.js copied verbatim (no edits).
           It is the package's pure 2D-canvas engine: no imports, no React,
           no DOM. The React component (dist/index.es.js) is not vendored.
sha256   : 4b43963f6409d310b9d80e592d4fb0f1435884a59d0c252f6f1b75926c09b949  orbs.js
           915a283980628a0ca9e7b423ebafc6f3a0fa1e630ff17d34e59034808d92011c  LICENSE

The chunk exports minified aliases. The ones Plexora uses
(plexora/client/src/js/services/orbDriver.js):
  M = MODE_FRAMES    S = STATE_TO_MODE    p = paintFrame    r = resolvePreset
(see dist/engine.es.js of the package for the full alias table).

Do not edit orbs.js. To upgrade: npm pack the new version, copy its engine
chunk here under a new versioned directory, update the alias table above and
the import path in orbDriver.js.

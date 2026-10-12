# Vendored: mediabunny 1.60.0

`mediabunny-1.60.0.mjs` is [mediabunny](https://github.com/Vanilagy/mediabunny) 1.60.0
(npm `mediabunny@1.60.0`, MPL-2.0, `LICENSE-mediabunny`), **unmodified**,
bundled with webpack 5 down to the exports Video Studio's encoder worker
(`../videoEncodeWorker.js`) imports. The source of every file in it is the
published npm package of that version.

Rebuild (from `plexora/client`, with the package unpacked as
`node_modules/mediabunny` in a scratch directory):

```js
// entry.mjs
export {
    Output, Mp4OutputFormat, WebMOutputFormat, BufferTarget, StreamTarget,
    VideoSampleSource, VideoSample, canEncodeVideo, QUALITY_LOW, QUALITY_MEDIUM,
    QUALITY_HIGH, QUALITY_VERY_HIGH,
} from "mediabunny";
```

webpack: `mode: "production"`, `experiments.outputModule`,
`output.library.type: "module"`, `optimization.usedExports/sideEffects`.

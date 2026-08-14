/**
 * rgbImageViewer.js
 *
 * @class RgbImageViewer -- minimal pan/zoom viewer for a flat RGB quick-view
 * datasource (config.image_kind === 'rgb', see main.js's init()). Kept as
 * its own small class rather than a mode flag inside ImageViewer: that
 * class unconditionally builds one grayscale transfer function per channel
 * and a custom WebGL colorize pass (see its constructor) -- none of that
 * applies to a flat image with imageData: [], so it's simpler and safer to
 * hand OpenSeadragon its native single-image tile source directly and skip
 * DataLayer/ChannelList/ViewerSidebar/gating entirely.
 */
class RgbImageViewer {
    constructor(config) {
        this.config = config;
    }

    async init() {
        this.viewer = OpenSeadragon({
            id: "openseadragon",
            prefixUrl: plexoraUrl("client/external/openseadragon-bin-2.4.0/openseadragon-flat-toolbar-icons-master/images/"),
            tileSources: {
                type: "image",
                url: plexoraUrl(`generated/rgb/${datasource}`),
            },
            minZoomImageRatio: 0.9,
            visibilityRatio: 1,
            homeFillsViewer: false,
            showNavigator: false,
        });
        const loader = document.getElementById("openseadragon_loader");
        this.viewer.addHandler("open", () => {
            if (loader) {
                loader.style.display = "none";
            }
        });
    }

    downloadCurrentView() {
        const canvas = this.viewer?.drawer?.canvas;
        if (!canvas) return;
        const link = document.createElement("a");
        link.download = `${datasource || "plexora"}_current_view.png`;
        link.href = canvas.toDataURL("image/png");
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    }
}

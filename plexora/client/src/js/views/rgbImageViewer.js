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
        window.PlexoraStatus?.watchViewer(this.viewer);
        const loader = document.getElementById("openseadragon_loader");
        this.viewer.addHandler("open", () => {
            if (loader) {
                loader.style.display = "none";
            }
        });
        this.initProjectLabel();
    }

    initProjectLabel() {
        const wrapper = document.getElementById("openseadragon_wrapper");
        if (!wrapper || document.getElementById("viewer_project_label")) return;
        const label = document.createElement("div");
        label.id = "viewer_project_label";
        label.className = "viewer-project-label";
        label.textContent = datasource || "";
        wrapper.appendChild(label);
    }

    downloadCurrentView(format = "png") {
        const canvas = this.viewer?.drawer?.canvas;
        if (!canvas) return;

        if (format === "pdf") {
            const pdf = new jsPDF({
                orientation: canvas.width >= canvas.height ? "landscape" : "portrait",
                unit: "px",
                format: [canvas.width, canvas.height],
            });
            pdf.addImage(canvas.toDataURL("image/png"), "PNG", 0, 0, canvas.width, canvas.height);
            // No channels/scale bar on a flat RGB image -- the project label
            // is the only overlay, drawn as real vector text/shape rather
            // than baked into the image pixels (matches ImageViewer's PDF
            // export, see imageViewer.js's drawProjectLabelVector).
            this.drawProjectLabelVector(pdf);
            pdf.save(`${datasource || "plexora"}_current_view.pdf`);
            return;
        }
        const link = document.createElement("a");
        link.download = `${datasource || "plexora"}_current_view.png`;
        link.href = canvas.toDataURL("image/png");
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    }

    getOverlayRectInCanvasSpace(el) {
        const canvasEl = this.viewer?.drawer?.canvas;
        if (!canvasEl || !el) return null;
        const canvasRect = canvasEl.getBoundingClientRect();
        const elRect = el.getBoundingClientRect();
        if (!canvasRect.width || !canvasRect.height) return null;
        const scaleX = canvasEl.width / canvasRect.width;
        const scaleY = canvasEl.height / canvasRect.height;
        return {
            x: (elRect.left - canvasRect.left) * scaleX,
            y: (elRect.top - canvasRect.top) * scaleY,
            width: elRect.width * scaleX,
            height: elRect.height * scaleY,
            scale: scaleX,
        };
    }

    drawProjectLabelVector(pdf) {
        const labelEl = document.getElementById("viewer_project_label");
        if (!labelEl) return;
        const rect = this.getOverlayRectInCanvasSpace(labelEl);
        if (!rect || !rect.width) return;

        pdf.saveGraphicsState();
        pdf.setGState(new pdf.GState({ opacity: 0.86 }));
        pdf.setFillColor(17, 24, 39);
        pdf.roundedRect(rect.x, rect.y, rect.width, rect.height, 3 * rect.scale, 3 * rect.scale, "F");
        pdf.restoreGraphicsState();

        pdf.setFont("helvetica", "bold");
        pdf.setFontSize(Math.max(6, 13 * rect.scale));
        pdf.setTextColor(241, 245, 249);
        pdf.text(labelEl.textContent || "", rect.x + rect.width / 2, rect.y + rect.height / 2, {
            align: "center",
            baseline: "middle",
        });
    }
}

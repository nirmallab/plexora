/**
 * Typing into a text annotation where it sits.
 *
 * Replaces the <textarea> this used to be, which could only ever hold one style
 * for the whole box. The editor is a contenteditable, and the two things that
 * usually go wrong with one are handled head on:
 *
 * **The model is the state; the DOM is a view of it.** Formatting commands
 * change `this.rich` and re-render, rather than asking `execCommand` to mutate
 * the DOM and hoping to recognise what it produced. Browsers disagree about
 * whether bold is <b>, <strong> or a style attribute, and a document whose
 * shape depends on which browser typed into it cannot be normalised into
 * anything canonical.
 *
 * **Paste is plain text only.** `workspace_body.html` already warns that
 * contenteditable accepts pasted markup; reading `text/plain` and nothing else
 * answers that outright, and a paste out of a word processor arrives as words
 * rather than as forty spans of Calibri.
 *
 * It mounts in `#fb_overlay_layer` and must stay there:
 * `FigureCanvas.render()` replaces the whole page surface on every change, and
 * an editor mounted inside it would be destroyed by the autosave triggered by
 * the last thing the user typed.
 */
class FigureTextEditor {

    constructor({ overlayEl, canvas, state, onCommit }) {
        this.overlayEl = overlayEl;
        this.canvas = canvas;
        this.state = state;
        this.onCommit = onCommit || (() => {});
        this.el = null;
        this.annotationId = null;
        this.rich = null;
        this.started = null;
        this.pending = null;
    }

    get active() { return Boolean(this.el); }

    get annotation() {
        return this.annotationId
            ? this.state.document.annotations[this.annotationId] : null;
    }

    // -- opening and closing -----------------------------------------------

    open(annotationId, options) {
        const annotation = this.state.document.annotations[annotationId];
        if (!annotation || annotation.type !== "text" || !this.overlayEl) return;
        this.close(false);

        //: Marks waiting for something to mark. Bold and italic live on RUNS,
        //: and a box with no words in it has no runs -- `normalizeRun` drops
        //: one with no text, so there is nowhere to write "this box is bold"
        //: before there is a first character. The Text card's two heading
        //: styles are exactly that case, so the weight travels with the editor
        //: and lands on whatever the first keystroke (or paste) puts there.
        //:
        //: Not a box-level `bold` in the style, which is the other way this
        //: could have gone: `resolveRun` falls back to the box for size, family
        //: and colour but not for the marks, and a run stores a mark only when
        //: it is ON -- so a bold box would have had no way to say "not this
        //: word", and unbolding inside a heading would have stopped working.
        this.pending = (options && options.marks) || null;

        const target = this.canvas.surfaceEl.querySelector(
            `[data-annotation-id="${annotationId}"]`);
        if (!target) return;

        this.annotationId = annotationId;
        this.rich = FigureRichText.normalize(annotation.text || "", annotation.rich);
        this.started = JSON.stringify(this.rich);

        this.el = document.createElement("div");
        this.el.className = "fb-text-editor";
        this.el.contentEditable = "true";
        this.el.spellcheck = false;
        this.position(target);
        this.render();
        this.overlayEl.appendChild(this.el);

        // The annotation itself is hidden while its editor is open, so the two
        // are never on screen at once and half a pixel of disagreement between
        // them does not read as a ghost.
        target.style.visibility = "hidden";

        this.el.addEventListener("keydown", (event) => this.keyDown(event));
        this.el.addEventListener("paste", (event) => this.paste(event));
        this.el.addEventListener("input", () => this.readBack());
        this.el.addEventListener("blur", () => this.commit());
        this.el.focus();
        // Everything, highlighted, the moment the editor opens -- the same thing
        // double-clicking a caption does in every other tool, and the thing that
        // makes "double-click, type" replace the caption rather than append to
        // it.
        //
        // Asserted twice. `focus()` and the click that opened the editor are
        // both still settling, and a browser that has its own idea of what the
        // double-click selected applies it after this handler returns; whichever
        // range lands last is the one the user sees, so this makes it ours. The
        // guard is the annotation id, so a quick second double-click on a
        // different box cannot be overwritten by the first one's frame.
        this.selectAll();
        const opened = annotationId;
        //: Set AFTER the call above, because `selectAll` goes through
        //: `setOffsets` and would clear it. Any later `setOffsets` clears it
        //: too, which is the point: a caller that placed the caret on purpose
        //: -- Insert Symbol opening the editor to add one character at the end
        //: -- must not be overruled a frame later by a select-all it did not
        //: ask for, leaving the whole caption highlighted and one keystroke
        //: from being replaced.
        this._reassert = true;
        window.requestAnimationFrame?.(() => {
            if (this._reassert && this.el && this.annotationId === opened) {
                this.selectAll();
            }
        });
    }

    /**
     * Put the editor exactly over the annotation.
     *
     * Measured from the rendered element rather than computed from millimetres,
     * because the annotation may be rotated -- and a rotated box's bounding
     * rectangle is the only honest place to put an upright editor over it.
     */
    position(target) {
        const box = target.getBoundingClientRect();
        const host = this.overlayEl.getBoundingClientRect();
        const annotation = this.annotation;
        this.el.style.left = (box.left - host.left) + "px";
        this.el.style.top = (box.top - host.top) + "px";
        this.el.style.width = Math.max(40, box.width) + "px";
        this.el.style.minHeight = Math.max(16, box.height) + "px";
        this.el.style.textAlign =
            annotation.style.align === "justify" ? "justify" : annotation.style.align;
        this.applyBoxType(annotation.style);
    }

    /**
     * The BOX's type, set on the editor itself, as the inherited default.
     *
     * This is what makes the size you type at the size that gets drawn.
     *
     * Every run renders inside a <span> carrying its own resolved font, so the
     * type was right for every character that was already there -- and wrong
     * for every character typed anywhere a span did not already exist. An empty
     * box renders as `<div class="fb-text-editor-line"><br></div>` with no span
     * in it at all, so the first thing typed into a caption went straight into
     * the line div and inherited the APP's type: the workspace's 13 px UI font,
     * where the canvas was about to draw 14 pt. Place a text box, type, press
     * Escape, and the words changed size in front of you. The same happened for
     * a character typed at the start of a line, or into a blank line mid-block.
     *
     * `.fb-text-editor-line` used to hard-code `line-height: 1.2` for the same
     * reason and with the same flaw: 1.2 is the DEFAULT leading, not this box's,
     * so a caption set to 1.5 was typed at 1.2 and drawn at 1.5. It comes from
     * the style now, which is where `FigureRichText.lineMetrics` reads it.
     *
     * The size is `FigureCanvas.fontPx` -- the same function the SVG on the
     * canvas is drawn with, not a second conversion that could round differently.
     */
    /**
     * Follow the annotation after a re-render.
     *
     * The box can change under an open editor -- the sidebar's leading and
     * alignment are box properties, and a commit re-lays the annotation out --
     * and the editor was placed once, at `open`, and never looked again. So
     * setting line spacing to 1.5 while typing moved the caption and left the
     * editor over where it used to be, at the leading it used to have.
     *
     * Position and type only; the DOM is not re-rendered, because rebuilding it
     * would put the caret back at the start in the middle of a sentence.
     */
    reposition(target) {
        if (!this.el || !target || !this.annotation) return;
        this.position(target);
    }

    applyBoxType(style) {
        this.el.style.fontFamily = FigureRichText.cssStack(style.font_family);
        this.el.style.fontSize = this.canvas.fontPx(style.font_size_pt) + "px";
        this.el.style.lineHeight = String(
            style.line_height || FigureRichText.LINE_HEIGHT);
        this.el.style.color = style.color;
        // The backdrop follows the INK, which is a per-caption decision and so
        // cannot live in the stylesheet. The editor had the workspace's paper
        // grey behind it and the caption's own colour on it -- and the default
        // caption on a fluorescence panel is white, so the commonest text on
        // this canvas was edited at about 1.05:1. White on paper is typing
        // blind.
        this.el.style.background = FigureTextEditor.isLightInk(style.color)
            ? "#16202e" : "#f4f5f8";
    }

    /** Whether a colour is light enough that it needs a dark surround to be
     *  typed against. Rec. 709 luma, which is close enough for a yes/no about
     *  a backdrop and does not need the full contrast formula. */
    static isLightInk(color) {
        const hex = /^#?([0-9a-f]{6})$/i.exec(String(color || ""));
        if (!hex) return false;
        const value = parseInt(hex[1], 16);
        const luma = 0.2126 * ((value >> 16) & 255)
            + 0.7152 * ((value >> 8) & 255)
            + 0.0722 * (value & 255);
        return luma > 140;
    }

    close(restore) {
        this.overlayEl?.querySelectorAll(".fb-text-editor")
            .forEach((editor) => editor.remove());
        if (restore !== false && this.annotationId) {
            const target = this.canvas.surfaceEl.querySelector(
                `[data-annotation-id="${this.annotationId}"]`);
            if (target) target.style.visibility = "";
        }
        this.el = null;
        this.annotationId = null;
        this.rich = null;
        this.started = null;
        this.pending = null;
    }

    // -- rendering the model ------------------------------------------------

    render() {
        const annotation = this.annotation;
        if (!this.el || !annotation) return;
        this.el.innerHTML = this.rich.lines.map((line) => {
            const runs = line.runs.map((run) => this.runMarkup(run, annotation.style)).join("");
            // A <br> keeps an empty line selectable: a <div> with nothing in it
            // collapses to zero height and the caret cannot be put in it.
            return `<div class="fb-text-editor-line">${runs || "<br>"}</div>`;
        }).join("");
    }

    runMarkup(run, style) {
        const resolved = FigureRichText.resolveRun(run, style);
        const css = [
            `font-family:${FigureRichText.cssStack(resolved.family)}`,
            `font-size:${this.canvas.fontPx(resolved.size_pt)}px`,
            `color:${resolved.color}`,
            resolved.bold ? "font-weight:bold" : "",
            resolved.italic ? "font-style:italic" : "",
            (resolved.underline || resolved.strike)
                ? `text-decoration:${resolved.underline ? "underline " : ""}`
                  + `${resolved.strike ? "line-through" : ""}`
                : "",
        ].filter(Boolean).join(";");
        return `<span ${FigureRichText.runAttributes(run)} style="${css}"
                >${FigureSchema.escapeHtml(run.text)}</span>`;
    }

    /** Take whatever the browser did to the DOM back into the model. */
    readBack() {
        if (!this.el) return;
        this.rich = FigureRichText.normalize(
            "", FigureRichText.linesFromDom(this.el));
        this.markPending();
    }

    /**
     * Spend the marks the editor opened with on the first words typed into it.
     *
     * Over EVERYTHING rather than over the characters that just arrived: this
     * fires once, on the first input into a box that opened empty, so
     * everything IS what just arrived -- and a paste lands as a single input,
     * which is the case that would otherwise leave one word bold and the rest
     * of the sentence not.
     *
     * The caret is read before the re-render and put back after it, the same
     * way `applyFormat` does. What is not restored is the marked RANGE: this
     * runs mid-keystroke, and highlighting the letter just typed would mean the
     * next one replaced it.
     */
    markPending() {
        if (!this.pending) return;
        const length = this.plainLength();
        if (!length) return;
        const marks = this.pending;
        this.pending = null;
        const at = this.offsets();
        this.rich = FigureRichText.applyToRange(this.rich, 0, length, marks);
        this.render();
        if (at) this.setOffsets(at);
    }

    // -- keys ----------------------------------------------------------------

    keyDown(event) {
        // Every canvas and workspace shortcut is off while this has focus. The
        // old textarea relied on both of them checking for a focused INPUT or
        // TEXTAREA first; a contenteditable is a DIV, so neither would.
        event.stopPropagation();

        const accel = event.metaKey || event.ctrlKey;
        if (event.key === "Escape") {
            event.preventDefault();
            this.cancel();
            return;
        }
        if (event.key === "Enter" && accel) {
            event.preventDefault();
            this.el.blur();
            return;
        }
        if (event.key === "Enter" && !event.shiftKey) {
            // Handled in the model rather than left to the browser: engines
            // disagree about whether Enter makes a <div>, a <p> or a <br>, and
            // a line break is the one thing here that must be unambiguous.
            event.preventDefault();
            this.splitLine();
            return;
        }
        if (accel && !event.shiftKey && !event.altKey) {
            const mark = { b: "bold", i: "italic", u: "underline" }[event.key.toLowerCase()];
            if (mark) {
                event.preventDefault();
                this.toggle(mark);
            }
        }
    }

    paste(event) {
        event.preventDefault();
        const text = (event.clipboardData || window.clipboardData)?.getData("text/plain");
        if (!text) return;
        this.replaceSelection(FigureRichText.normalizeBreaks(text));
    }

    // -- editing the model ---------------------------------------------------

    /**
     * Turn a mark on or off over the selection.
     *
     * Off when every character in the range already has it, on otherwise --
     * which is what makes a second press of Cmd+B undo the first even when the
     * selection started out half bold.
     */
    toggle(mark) {
        const range = this.offsets();
        if (!range) return;
        const format = FigureRichText.formatOfRange(
            this.rich, range.start, range.end, this.annotation.style);
        const value = format && format[mark] === true ? null : true;
        this.applyFormat({ [mark]: value }, range);
    }

    /** Set one or more properties over the selection, or over everything when
     *  nothing is selected -- which is what the sidebar's controls do. */
    applyFormat(patch, range) {
        const at = range || this.offsets();
        if (!at) return;
        const whole = at.start === at.end;
        const span = whole ? { start: 0, end: this.plainLength() } : at;
        this.rich = FigureRichText.applyToRange(this.rich, span.start, span.end, patch);
        this.render();
        this.setOffsets(at);
    }

    splitLine() {
        const at = this.offsets();
        if (!at) return;
        if (at.end > at.start) this.replaceSelection("");
        const cut = this.offsets().start;
        const lines = [];
        let offset = 0;
        for (let index = 0; index < this.rich.lines.length; index += 1) {
            const line = this.rich.lines[index];
            const length = line.runs.reduce((total, run) => total + run.text.length, 0);
            if (offset <= cut && cut <= offset + length) {
                const local = cut - offset;
                lines.push({ hard: line.hard, runs: this.sliceRuns(line.runs, 0, local) });
                lines.push({ hard: true, runs: this.sliceRuns(line.runs, local, length) });
            } else {
                lines.push(line);
            }
            offset += length + 1;
        }
        this.rich = FigureRichText.normalize("", { lines: lines });
        this.render();
        this.setOffsets({ start: cut + 1, end: cut + 1 });
    }

    sliceRuns(runs, from, to) {
        const out = [];
        let offset = 0;
        for (const run of runs) {
            const start = Math.max(from, offset);
            const end = Math.min(to, offset + run.text.length);
            if (end > start) {
                out.push(FigureRichText.orderRun(
                    { ...run, text: run.text.slice(start - offset, end - offset) }));
            }
            offset += run.text.length;
        }
        return FigureRichText.coalesce(out);
    }

    /** Replace the selection with plain text, keeping the marks at its start. */
    replaceSelection(text) {
        const at = this.offsets();
        if (!at) return;
        const pieces = text.split("\n");
        const marks = FigureRichText.runsInRange(this.rich, at.start, at.start)[0] || {};
        const carried = FigureRichText.orderRun({ ...marks, text: "" });
        delete carried.text;

        const lines = [];
        let offset = 0;
        let inserted = at.start;
        for (const line of this.rich.lines) {
            const length = line.runs.reduce((total, run) => total + run.text.length, 0);
            const from = offset;
            const to = offset + length;
            if (at.end < from || at.start > to) {
                lines.push(line);
            } else if (at.start >= from && at.start <= to) {
                const head = this.sliceRuns(line.runs, 0, at.start - from);
                const tail = at.end <= to
                    ? this.sliceRuns(line.runs, at.end - from, length) : [];
                if (pieces.length === 1) {
                    lines.push({ hard: line.hard, runs: FigureRichText.coalesce(
                        head.concat(pieces[0] ? [{ ...carried, text: pieces[0] }] : [], tail)) });
                    inserted = at.start + pieces[0].length;
                } else {
                    lines.push({ hard: line.hard, runs: FigureRichText.coalesce(
                        head.concat([{ ...carried, text: pieces[0] }])) });
                    for (const middle of pieces.slice(1, -1)) {
                        lines.push({ hard: true, runs: [{ ...carried, text: middle }] });
                    }
                    const last = pieces[pieces.length - 1];
                    lines.push({ hard: true, runs: FigureRichText.coalesce(
                        [{ ...carried, text: last }].concat(tail)) });
                    inserted = at.start + text.length;
                }
            }
            offset = to + 1;
        }
        this.rich = FigureRichText.normalize("", { lines: lines });
        this.render();
        this.setOffsets({ start: inserted, end: inserted });
    }

    plainLength() {
        return FigureRichText.plainText(this.rich).length;
    }

    // -- selection <-> offsets ----------------------------------------------

    /** Every text node in the editor, with the plain-text offset it starts at.
     *
     *  A line contributes its own length plus one for the newline that follows
     *  it, so these offsets are the same ones `applyToRange` works in. */
    textNodes() {
        const out = [];
        let offset = 0;
        const lines = Array.from(this.el.querySelectorAll(".fb-text-editor-line"));
        lines.forEach((line, index) => {
            if (index > 0) offset += 1;
            const walk = (node) => {
                if (node.nodeType === 3) {
                    out.push({ node: node, start: offset });
                    offset += node.nodeValue.length;
                    return;
                }
                Array.from(node.childNodes || []).forEach(walk);
            };
            walk(line);
            out.push({ node: line, start: offset, end: true });
        });
        return out;
    }

    offsets() {
        if (!this.el) return null;
        const selection = window.getSelection?.();
        if (!selection || !selection.rangeCount) return { start: 0, end: 0 };
        const range = selection.getRangeAt(0);
        if (!this.el.contains(range.startContainer)) return { start: 0, end: 0 };
        const start = this.offsetOf(range.startContainer, range.startOffset);
        const end = this.offsetOf(range.endContainer, range.endOffset);
        return { start: Math.min(start, end), end: Math.max(start, end) };
    }

    offsetOf(node, within) {
        const nodes = this.textNodes();
        for (const entry of nodes) {
            if (entry.node === node && !entry.end) return entry.start + within;
        }
        // A caret in an element rather than a text node -- an empty line, or the
        // gap after the last run. Take the line's own start.
        for (const entry of nodes) {
            if (entry.node === node) return entry.start;
        }
        return 0;
    }

    setOffsets(at) {
        if (!this.el || !window.getSelection) return;
        // Somebody has said where the caret goes. See `open`.
        this._reassert = false;
        const place = (offset) => {
            const nodes = this.textNodes().filter((entry) => !entry.end);
            for (const entry of nodes) {
                if (offset <= entry.start + entry.node.nodeValue.length) {
                    return { node: entry.node,
                             offset: Math.max(0, offset - entry.start) };
                }
            }
            const last = nodes[nodes.length - 1];
            return last
                ? { node: last.node, offset: last.node.nodeValue.length }
                : { node: this.el, offset: 0 };
        };
        const from = place(at.start);
        const to = place(at.end);
        const range = document.createRange();
        range.setStart(from.node, from.offset);
        range.setEnd(to.node, to.offset);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
    }

    selectAll() {
        this.setOffsets({ start: 0, end: this.plainLength() });
    }

    // -- committing ----------------------------------------------------------

    cancel() {
        this.close(true);
        this.canvas.render();
    }

    commit() {
        const annotationId = this.annotationId;
        const annotation = this.annotation;
        if (!annotationId || !annotation) return;
        this.readBack();
        // Re-broken to the box's own width before it is stored: the lines ARE
        // the document, so the wrap has to be resolved by the one thing that
        // can measure a string before anything else reads them.
        const rich = this.canvas.rewrapAnnotation({ ...annotation, rich: this.rich });
        const started = this.started;
        this.close(true);

        // A text box placed and then left empty is invisible and unselectable,
        // so cancelling out of a fresh one removes it rather than leaving a
        // ghost on the page.
        if (!FigureRichText.plainText(rich).trim()) {
            if (!annotation.text) {
                this.state.commit(
                    [{ op: "remove_annotations", annotation_ids: [annotationId] }],
                    (draft) => { delete draft.annotations[annotationId]; });
                return;
            }
        }
        if (JSON.stringify(rich) === started) {
            this.canvas.render();
            return;
        }
        this.onCommit(annotationId, rich);
    }
}

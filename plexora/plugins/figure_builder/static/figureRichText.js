/**
 * A text annotation's words, as lines of styled runs.
 *
 * This is the browser's half of a rule written twice. `server/schema.py` holds
 * the other half and `server/textmetrics.py` the constants, and
 * `test_the_client_and_the_server_normalise_rich_text_the_same_way` asserts the
 * two normalisers agree on a table of deliberately awkward inputs. They have to
 * agree exactly: the canvas draws from what this file produces and the document
 * stores what Python produces, so a disagreement shows the user one thing and
 * saves another, and they find out on reload.
 *
 * The same arrangement already exists in this plugin for arrowheads
 * (`FigureCanvas.arrowHeadPoints` and `export._arrow_head`). One rule, two
 * languages, pinned by a test.
 *
 * Everything here is PURE -- no DOM, no canvas, no measurement. That is what
 * lets the node probe exercise it. The one thing that genuinely needs a browser,
 * measuring how wide a string is, arrives as the `measure` argument to
 * `rewrap()` rather than being reached for inside it.
 */
class FigureRichText {

    // -- constants, mirrored from server/textmetrics.py --------------------

    /** Millimetres per typographic point. Computed, never the rounded literal:
     *  `FigureCanvas.PT_PER_MM` is 2.8346 and rounding here would put a floor
     *  under how exactly the canvas and the exporter can be asserted to agree. */
    static get MM_PER_PT() { return 25.4 / 72; }

    /** Line box height as a multiple of the type size. Was `line-height: 1.25`
     *  in the stylesheet; the canvas no longer leaves leading to the browser, so
     *  it has to live where both languages can read it. */
    static get LINE_HEIGHT() { return 1.2; }

    /** The type size a NEW text box starts at, in points. Not
     *  `settings.style.font_size_pt` -- that one is the furniture drawn on a
     *  panel, and has to stay small enough to sit inside the image. See
     *  textmetrics.DEFAULT_TEXT_SIZE_PT for the whole of it. */
    static get DEFAULT_SIZE_PT() { return 14.0; }

    /** Ascent and descent as a fraction of the em, from the Adobe core AFM
     *  files. Used to centre a line in its box, which is what makes vertical
     *  alignment and mixed-size lines land in the same place everywhere. */
    static get ASCENT() {
        return { "Helvetica": 0.718, "Times-Roman": 0.683, "Courier": 0.629 };
    }
    static get DESCENT() {
        return { "Helvetica": 0.207, "Times-Roman": 0.217, "Courier": 0.157 };
    }

    static get UNDERLINE_OFFSET_EM() { return 0.12; }
    static get UNDERLINE_THICKNESS_EM() { return 0.06; }
    static get STRIKE_OFFSET_EM() { return 0.26; }

    /** PostScript base names, not "sans"/"serif": `settings.style.font_family`
     *  already holds "Helvetica" and hands it to reportlab unchanged. */
    static get FAMILIES() { return ["Helvetica", "Times-Roman", "Courier"]; }
    static get DEFAULT_FAMILY() { return "Helvetica"; }

    /** What to ask the browser for, per family. Each stack names substitutes
     *  drawn to the core font's widths -- Arial and Liberation Sans were both
     *  built to Helvetica's metrics -- so a line that fits on screen fits in
     *  the PDF. That is what makes storing the browser's line breaks safe. */
    static get CSS_STACK() {
        return {
            "Helvetica": 'Helvetica, Arial, "Liberation Sans", sans-serif',
            "Times-Roman": '"Times New Roman", Times, "Liberation Serif", serif',
            "Courier": '"Courier New", Courier, "Liberation Mono", monospace',
        };
    }

    static get MAX_TEXT_LENGTH() { return 4000; }
    static get MAX_TEXT_LINES() { return 200; }
    static get MAX_RUNS_PER_LINE() { return 100; }
    static get MAX_TEXT_RUNS() { return 500; }

    /** The marks a run may carry, in the order they are written. Key order is
     *  not semantic, but keeping it fixed keeps a JSON diff readable. */
    static get MARKS() {
        return ["bold", "italic", "underline", "strike", "family", "size_pt", "color"];
    }

    static family(value) {
        return FigureRichText.FAMILIES.includes(value)
            ? value : FigureRichText.DEFAULT_FAMILY;
    }

    static cssStack(value) {
        return FigureRichText.CSS_STACK[FigureRichText.family(value)];
    }

    // -- normalising -------------------------------------------------------

    /**
     * Lines of runs, budgeted and coalesced. Mirrors `schema.normalize_rich_text`.
     *
     * `flat` is the plain string the annotation already had; `raw` is a rich
     * structure if there is one. No rich structure means an older document (or
     * an older client), and it is rebuilt from the flat string -- which is the
     * whole migration, and why this feature needed no schema version bump.
     *
     * Never throws. Oversized input is truncated, because the server's
     * equivalent skipping an annotation would delete the user's text box.
     */
    static normalize(flat, raw) {
        const lines = raw && Array.isArray(raw.lines) ? raw.lines : null;
        if (!lines) return FigureRichText.fromPlain(flat);

        let budget = FigureRichText.MAX_TEXT_LENGTH;
        let runsLeft = FigureRichText.MAX_TEXT_RUNS;
        const out = [];
        for (const rawLine of lines.slice(0, FigureRichText.MAX_TEXT_LINES)) {
            if (budget <= 0 || runsLeft <= 0) break;
            if (!rawLine || typeof rawLine !== "object") continue;
            const rawRuns = Array.isArray(rawLine.runs) ? rawLine.runs : [];
            let runs = [];
            // Not sliced: every run that survives costs at least one character,
            // so the character budget is what bounds this. Slicing the input
            // would drop a caption sitting behind a tail of empty spans.
            for (const rawRun of rawRuns) {
                if (budget <= 0) break;
                const run = FigureRichText.normalizeRun(rawRun, budget);
                if (!run) continue;
                budget -= run.text.length;
                runs.push(run);
            }
            // Coalesce BEFORE capping: a word processor marks up every letter,
            // so a pasted sentence arrives as one run per character and capping
            // first would throw away the end of it.
            runs = FigureRichText.capRuns(
                FigureRichText.coalesce(runs),
                Math.min(FigureRichText.MAX_RUNS_PER_LINE, Math.max(0, runsLeft)));
            runsLeft -= runs.length;
            // An empty line in the middle is content -- the blank line between
            // two paragraphs of a caption -- so it is kept.
            out.push({ hard: rawLine.hard === undefined ? true : Boolean(rawLine.hard),
                       runs: runs });
        }

        while (out.length && !out[out.length - 1].runs.length) out.pop();
        if (!out.length) return FigureRichText.fromPlain("");
        // Nothing sits above the first line for it to have wrapped from, so its
        // break is the author's by definition. A `hard: false` first line would
        // make a re-wrap try to join it to the line before it.
        out[0].hard = true;
        return { lines: out };
    }

    /** Lines from a flat string -- the path every pre-`rich` document takes. */
    static fromPlain(flat) {
        const text = FigureRichText.normalizeBreaks(
            typeof flat === "string" ? flat : "");
        const out = text.split("\n").slice(0, FigureRichText.MAX_TEXT_LINES)
            .map((line) => {
                const cleaned = FigureRichText.cleanRunText(
                    line, FigureRichText.MAX_TEXT_LENGTH);
                return { hard: true, runs: cleaned ? [{ text: cleaned }] : [] };
            });
        return { lines: out.length ? out : [{ hard: true, runs: [] }] };
    }

    /**
     * One styled span, or null if there is nothing left of it.
     *
     * A run carries only what it OVERRIDES. An absent `size_pt` means "whatever
     * the box says", so raising the box's font size still reaches every run the
     * user never touched.
     */
    static normalizeRun(raw, budget) {
        if (!raw || typeof raw !== "object") return null;
        const text = FigureRichText.cleanRunText(raw.text, budget);
        if (!text) return null;
        const run = { text: text };
        for (const mark of ["bold", "italic", "underline", "strike"]) {
            if (raw[mark]) run[mark] = true;
        }
        const family = FigureRichText.cleanText(raw.family, 20);
        if (FigureRichText.FAMILIES.includes(family)) run.family = family;
        if (typeof raw.size_pt === "number" && isFinite(raw.size_pt)) {
            run.size_pt = Math.max(1, Math.min(200, raw.size_pt));
        }
        if (typeof raw.color === "string" && /^#[0-9a-fA-F]{6}$/.test(raw.color)) {
            run.color = raw.color.toLowerCase();
        }
        return run;
    }

    /**
     * Adjacent runs with identical marks become one.
     *
     * Load-bearing, not tidiness. A contenteditable emits a fresh span per
     * keystroke, so without this a caption grows a run per character: the
     * document balloons, the "did anything change?" check before a commit never
     * fires because the shape differs every time, and this normaliser can no
     * longer be compared with the server's for equality at all.
     */
    static coalesce(runs) {
        const out = [];
        for (const run of runs) {
            const last = out[out.length - 1];
            if (last && FigureRichText.marksOf(last) === FigureRichText.marksOf(run)) {
                last.text += run.text;
            } else {
                out.push(run);
            }
        }
        return out;
    }

    /**
     * At most `cap` runs, keeping every word.
     *
     * The overflow folds into the last surviving run rather than being sliced
     * off. A hundred distinctly styled spans on one line is already past
     * anything a caption needs, but the words are still the user's, so the cap
     * costs them the marks and never the sentence.
     */
    static capRuns(runs, cap) {
        if (cap <= 0) return [];
        if (runs.length <= cap) return runs;
        const kept = runs.slice(0, cap);
        const tail = runs.slice(cap).map((run) => run.text).join("");
        kept[kept.length - 1] = { ...kept[kept.length - 1],
                                  text: kept[kept.length - 1].text + tail };
        return kept;
    }

    static marksOf(run) {
        return JSON.stringify(FigureRichText.MARKS.map((key) =>
            run[key] === undefined ? null : run[key]));
    }

    /** CRLF and CR become LF, tabs become a space -- a Windows paste otherwise
     *  leaves a bare CR inside a run, where it is a line break the line list
     *  knows nothing about. */
    static normalizeBreaks(value) {
        return value.replace(/\r\n/g, "\n").replace(/\r/g, "\n").replace(/\t/g, " ");
    }

    /** Run text, capped -- and NOT trimmed. The spaces around a run are the
     *  gaps between it and its neighbours on the same line, so trimming closes
     *  up "Fig. 1a" + "  DAPI" into "Fig. 1aDAPI". */
    static cleanRunText(value, budget) {
        if (value === null || value === undefined) return "";
        const text = typeof value === "string" ? value : String(value);
        // eslint-disable-next-line no-control-regex
        return text.replace(/\t/g, " ").replace(/[\x00-\x1f\x7f]/g, "")
            .slice(0, Math.max(0, budget));
    }

    /** The mirror of `schema.clean_text`: control characters out, trimmed,
     *  capped. Used for field values, never for run text. */
    static cleanText(value, limit) {
        if (value === null || value === undefined) return "";
        const text = typeof value === "string" ? value : String(value);
        // eslint-disable-next-line no-control-regex
        return text.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "")
            .trim().slice(0, limit);
    }

    // -- reading -----------------------------------------------------------

    /** The flat string this reads as. The annotation's `text` field is this. */
    static plainText(rich) {
        const lines = rich && Array.isArray(rich.lines) ? rich.lines : null;
        if (!lines) return "";
        return lines.map((line) => line.runs.map((run) => run.text).join(""))
            .join("\n").slice(0, FigureRichText.MAX_TEXT_LENGTH);
    }

    /** Whether two rich texts are the same. Only meaningful because both sides
     *  are coalesced into a canonical form first -- this is what lets a commit
     *  skip a no-op edit instead of writing a new revision per keystroke. */
    static equals(a, b) {
        return JSON.stringify(a) === JSON.stringify(b);
    }

    /** Every run's resolved style, for a box whose defaults are `style`. */
    static resolveRun(run, style) {
        return {
            text: run.text,
            family: FigureRichText.family(run.family || style.font_family),
            size_pt: run.size_pt === undefined ? style.font_size_pt : run.size_pt,
            bold: Boolean(run.bold),
            italic: Boolean(run.italic),
            underline: Boolean(run.underline),
            strike: Boolean(run.strike),
            color: run.color || style.color,
        };
    }

    // -- editing -----------------------------------------------------------

    /**
     * Apply a format patch to the characters in [start, end).
     *
     * Offsets are over the PLAIN text, newlines included, so they map directly
     * onto what a selection in the editor covers. Runs straddling either edge
     * are split, the ones inside take the patch, and the result is coalesced --
     * which is what stops "bold this word, then unbold it" leaving three runs
     * behind where there was one.
     *
     * A patch value of null removes the override and returns that property to
     * the box's; any other value sets it. This is the whole of partial-selection
     * formatting, and it is pure, so the probe can test it without a browser.
     */
    static applyToRange(rich, start, end, patch) {
        if (end <= start) return rich;
        let offset = 0;
        const lines = rich.lines.map((line, index) => {
            if (index > 0) offset += 1;             // the "\n" before this line
            const runs = [];
            for (const run of line.runs) {
                const from = offset;
                const to = offset + run.text.length;
                offset = to;
                // Three pieces, any of which may be empty: before the range,
                // inside it, after it.
                const cutA = Math.min(Math.max(start, from), to);
                const cutB = Math.min(Math.max(end, from), to);
                if (cutA > from) {
                    runs.push(FigureRichText.orderRun(
                        { ...run, text: run.text.slice(0, cutA - from) }));
                }
                if (cutB > cutA) {
                    runs.push(FigureRichText.patchRun(
                        { ...run, text: run.text.slice(cutA - from, cutB - from) }, patch));
                }
                if (to > cutB) {
                    runs.push(FigureRichText.orderRun(
                        { ...run, text: run.text.slice(cutB - from) }));
                }
            }
            return { hard: line.hard, runs: FigureRichText.coalesce(runs) };
        });
        return { lines: lines };
    }

    static patchRun(run, patch) {
        const out = { ...run };
        for (const key of Object.keys(patch)) {
            if (patch[key] === null || patch[key] === false) delete out[key];
            else out[key] = patch[key];
        }
        return FigureRichText.orderRun(out);
    }

    /**
     * A run with its keys in the canonical order: text, then MARKS.
     *
     * Key order carries no meaning to a reader, but it does to
     * `JSON.stringify`, which is how `equals()` decides whether a commit is a
     * no-op and how the parity test compares this normaliser with the server's.
     * `schema._normalize_run` builds its dicts in exactly this order, so
     * matching it here means the two produce byte-identical JSON rather than
     * merely equivalent JSON.
     */
    static orderRun(run) {
        const out = { text: run.text };
        for (const key of FigureRichText.MARKS) {
            if (run[key] !== undefined) out[key] = run[key];
        }
        return out;
    }

    /** What [start, end) currently looks like: a value where every run in the
     *  range agrees, and null where they do not. That null is what the sidebar
     *  renders as an indeterminate control. */
    static formatOfRange(rich, start, end, style) {
        const runs = FigureRichText.runsInRange(rich, start, end);
        if (!runs.length) return null;
        const first = FigureRichText.resolveRun(runs[0], style);
        const out = { ...first };
        for (const run of runs.slice(1)) {
            const resolved = FigureRichText.resolveRun(run, style);
            for (const key of Object.keys(out)) {
                if (out[key] !== resolved[key]) out[key] = null;
            }
        }
        delete out.text;
        return out;
    }

    static runsInRange(rich, start, end) {
        let offset = 0;
        const hits = [];
        rich.lines.forEach((line, index) => {
            if (index > 0) offset += 1;
            for (const run of line.runs) {
                const from = offset;
                const to = offset + run.text.length;
                offset = to;
                // A caret (start === end) reports the run it sits inside, which
                // is what makes typing pick up the formatting to its left.
                if (start === end ? (from < start && start <= to) : (from < end && to > start)) {
                    hits.push(run);
                }
            }
        });
        return hits;
    }

    // -- wrapping ----------------------------------------------------------

    /**
     * Re-break the soft lines to fit `widthMm`, leaving the author's alone.
     *
     * Only the browser can measure a string, so only the browser can decide
     * where a line breaks -- which is why the break it chooses is STORED rather
     * than recomputed by an exporter that would have to guess. `hard` is what
     * keeps the two kinds apart: a line the author ended with Enter is a
     * paragraph boundary and is never joined to its neighbour, while a line the
     * wrap produced may be rewritten freely.
     *
     * `measure(text, resolvedRun)` returns millimetres and is injected, both
     * because a node probe has no text engine and because it keeps this
     * function pure.
     */
    static rewrap(rich, widthMm, style, measure) {
        const out = [];
        for (const paragraph of FigureRichText.paragraphs(rich)) {
            const lines = FigureRichText.breakRuns(paragraph, widthMm, style, measure);
            lines.forEach((runs, index) => {
                out.push({ hard: index === 0, runs: FigureRichText.coalesce(runs) });
            });
        }
        if (!out.length) return { lines: [{ hard: true, runs: [] }] };
        out[0].hard = true;
        return { lines: out };
    }

    /** The runs of each paragraph, soft breaks dissolved. A soft break was the
     *  wrap's own decision, so it carries no content and is simply removed;
     *  joining across it is what lets a widened box reflow. */
    static paragraphs(rich) {
        const out = [];
        rich.lines.forEach((line, index) => {
            if (index === 0 || line.hard) out.push([]);
            out[out.length - 1].push(...line.runs.map((run) => ({ ...run })));
        });
        return out.length ? out : [[]];
    }

    /**
     * Greedy line breaking over one paragraph's runs.
     *
     * The whitespace a break falls on STAYS on the line it broke from, and that
     * is the whole reason a re-wrap is reversible. Dropping it -- which reads as
     * the tidy thing to do, since it renders as nothing at the end of a line --
     * means the stored lines no longer concatenate back into what the author
     * typed: widening the box then produces "aaabbbcccddd". Because the lines
     * ARE the document here, a space consumed at layout time is a space deleted
     * from the caption.
     *
     * Trailing whitespace is invisible left-aligned and is excluded from the
     * line width when the layout centres or right-aligns, so keeping it costs
     * nothing on the page.
     */
    static breakRuns(runs, widthMm, style, measure) {
        const atoms = FigureRichText.atoms(runs);
        if (!atoms.length) return [[]];
        const lines = [];
        let line = [];
        let width = 0;
        for (const atom of atoms) {
            const w = measure(atom.text, FigureRichText.resolveRun(atom.run, style));
            // A word that cannot fit an empty line is not broken: splitting a
            // gene name or an accession number mid-token is worse than a line
            // that overhangs, and an overhang is visible where a silent split
            // reads as the real name.
            if (!atom.space && line.length && width + w > widthMm) {
                lines.push(line);
                line = [];
                width = 0;
            }
            line.push(atom);
            width += w;
        }
        lines.push(line);
        return lines.map((onLine) => onLine.map((a) => ({ ...a.run, text: a.text })));
    }

    /** Runs split into words and the whitespace between them, each remembering
     *  which run it came from so a break never loses a mark. */
    static atoms(runs) {
        const out = [];
        for (const run of runs) {
            for (const piece of run.text.split(/(\s+)/)) {
                if (!piece) continue;
                out.push({ text: piece, run: run, space: /^\s+$/.test(piece) });
            }
        }
        return out;
    }

    // -- reading the editor's DOM back ------------------------------------

    /**
     * Lines of runs, read back out of a contenteditable.
     *
     * The editor renders its DOM FROM the model, so most of what is here was
     * put here by `elementFromRich`. What this has to survive is the part the
     * browser contributes: typing merges text into a span, Backspace at the
     * start of a line dissolves a line element, and some engines leave a stray
     * <br> or an unwrapped text node behind. Rather than trusting any of that
     * shape, this walks the tree and asks each text node what marks it has
     * INHERITED, which is true whatever produced the nesting.
     *
     * Works on a minimal node interface -- `nodeType`, `nodeName`, `childNodes`,
     * `nodeValue`, `dataset`, `style` -- so a probe can drive it with plain
     * objects and no browser.
     */
    static linesFromDom(root) {
        const lines = [];
        let current = [];
        const flush = () => { lines.push(current); current = []; };

        const walk = (node, inherited) => {
            // Text
            if (node.nodeType === 3) {
                const text = FigureRichText.cleanRunText(
                    node.nodeValue || "", FigureRichText.MAX_TEXT_LENGTH);
                if (text) current.push(FigureRichText.orderRun({ ...inherited, text: text }));
                return;
            }
            if (node.nodeType !== 1) return;
            const name = (node.nodeName || "").toUpperCase();
            if (name === "BR") { flush(); return; }

            const marks = FigureRichText.marksFromNode(node, inherited);
            const block = name === "DIV" || name === "P";
            // A block element starts a line unless it is the first thing here,
            // in which case it IS the line already open.
            if (block && current.length) flush();
            for (const child of Array.from(node.childNodes || [])) walk(child, marks);
            if (block) flush();
        };

        for (const child of Array.from(root.childNodes || [])) walk(child, {});
        if (current.length) flush();

        const out = lines.map((runs) => ({
            hard: true, runs: FigureRichText.coalesce(runs),
        }));
        while (out.length > 1 && !out[out.length - 1].runs.length) out.pop();
        return { lines: out.length ? out : [{ hard: true, runs: [] }] };
    }

    /** What one element adds to the marks its children inherit. */
    static marksFromNode(node, inherited) {
        const marks = { ...inherited };
        const name = (node.nodeName || "").toUpperCase();
        const style = node.style || {};
        const data = node.dataset || {};

        if (name === "B" || name === "STRONG") marks.bold = true;
        if (name === "I" || name === "EM") marks.italic = true;
        if (name === "U") marks.underline = true;
        if (name === "S" || name === "STRIKE" || name === "DEL") marks.strike = true;

        const weight = style.fontWeight;
        if (weight === "bold" || weight === "bolder" || parseInt(weight, 10) >= 600) {
            marks.bold = true;
        }
        if (style.fontStyle === "italic" || style.fontStyle === "oblique") marks.italic = true;

        // The editor stamps its own marks as data attributes rather than
        // reading them back out of CSS: `font-family` comes back from the
        // browser as the whole resolved stack, and matching a family name
        // against that is a parser nobody should have to own.
        if (data.bold === "1") marks.bold = true;
        if (data.italic === "1") marks.italic = true;
        if (data.underline === "1") marks.underline = true;
        if (data.strike === "1") marks.strike = true;
        if (FigureRichText.FAMILIES.includes(data.family)) marks.family = data.family;
        if (data.size) {
            const size = parseFloat(data.size);
            if (isFinite(size)) marks.size_pt = Math.max(1, Math.min(200, size));
        }
        if (data.color && /^#[0-9a-fA-F]{6}$/.test(data.color)) {
            marks.color = data.color.toLowerCase();
        }
        return marks;
    }

    /** The marks a run carries, as the editor's data attributes. */
    static runAttributes(run) {
        const parts = [];
        if (run.bold) parts.push('data-bold="1"');
        if (run.italic) parts.push('data-italic="1"');
        if (run.underline) parts.push('data-underline="1"');
        if (run.strike) parts.push('data-strike="1"');
        if (run.family) parts.push(`data-family="${run.family}"`);
        if (run.size_pt !== undefined) parts.push(`data-size="${run.size_pt}"`);
        if (run.color) parts.push(`data-color="${run.color}"`);
        return parts.join(" ");
    }

    // -- measuring ---------------------------------------------------------

    /**
     * Height and baseline offset of the line box holding these runs.
     *
     * The tallest run sets the box and the line is centred in it, half the
     * leading above and half below, so a line mixing an 8 pt caption with a 6 pt
     * superscript sits where a reader expects rather than riding the bottom of
     * its box. Mirrors `textmetrics.line_metrics`.
     */
    static lineMetrics(runs, style) {
        const leading = style.line_height || FigureRichText.LINE_HEIGHT;
        if (!runs.length) {
            const em = style.font_size_pt * FigureRichText.MM_PER_PT;
            const family = FigureRichText.family(style.font_family);
            // An empty line still occupies one: it is the blank line between
            // two paragraphs, and collapsing it to nothing would close the gap
            // the author put there.
            return { lead: em * leading,
                     ascent: em * FigureRichText.ASCENT[family],
                     descent: em * FigureRichText.DESCENT[family] };
        }
        let tallest = runs[0];
        for (const run of runs) {
            const size = run.size_pt === undefined ? style.font_size_pt : run.size_pt;
            const best = tallest.size_pt === undefined ? style.font_size_pt : tallest.size_pt;
            if (size > best) tallest = run;
        }
        const size = tallest.size_pt === undefined ? style.font_size_pt : tallest.size_pt;
        const family = FigureRichText.family(tallest.family || style.font_family);
        const em = size * FigureRichText.MM_PER_PT;
        return { lead: em * leading,
                 ascent: em * FigureRichText.ASCENT[family],
                 descent: em * FigureRichText.DESCENT[family] };
    }
}

/**
 * failureMessage.js -- why a connection failed, laid out the way it was written.
 *
 * A connection failure is not a sentence. The steps in `connect.py` build it
 * out of three things, in this order:
 *
 *     Installing Plexora on you@cluster failed (pip exited 1).
 *         [the last dozen lines the far machine printed]
 *     Run it by hand over there to see the whole of it:
 *         ssh you@cluster pip install --upgrade plexora
 *
 * -- what went wrong, what the machine said about it, and what to do. Three
 * surfaces show that string: the Settings page's card for a saved server, the
 * connection dialog, and the navbar globe's panel. All three used to assign it
 * to `textContent`, and HTML collapses newlines, so the three parts ran
 * together into one paragraph with a cluster's login banner in the middle of
 * it. What that looks like, on a card a third of a column wide, is a wall of
 * grey text with no beginning.
 *
 * So the layout is here, once, for all of them, and it is the message's own:
 *
 * **The prose stays prose.** Each paragraph is a paragraph. The first one is
 * the headline and reads as one; the last one is almost always the fix, and it
 * is never hidden, clipped or scrolled -- it is the reason the message exists.
 *
 * **The machine's words are quoted, and bounded.** An indented run is
 * something printed rather than written: remote output, or a command to type.
 * It is set in the log's own monospace so it is not mistaken for Plexora
 * talking, and it is capped in height with a scroll of its own. That cap is
 * the whole point. `pip` can fail in one line or in forty, and the card it
 * lands on lives in a grid of equal-height rows -- an uncapped forty-line
 * quote does not just make one card enormous, it makes the two healthy
 * machines beside it enormous too. Capping only the quote, and never the
 * prose, is what keeps a failure card the size of a card while leaving every
 * word Plexora wrote in plain sight.
 *
 * **It is repainted, never rebuilt.** The Settings card repaints once a
 * second for as long as a connection is up. Rebuilding this on every one of
 * those would drop a selection mid-drag and put the quote back at the top
 * each time somebody scrolled it -- the same mistake, in the same place, that
 * logTerminal.js exists to stop making.
 */
window.PlexoraFailureMessage = (function () {
    "use strict";

    //: What marks a line as printed rather than written. Four spaces is what
    //: every step in connect.py indents a quoted line by; a tab is what a
    //: remote shell's own output sometimes arrives with.
    const QUOTED = /^(?: {4}|\t)/;

    //: Where the last painted message is remembered, so an unchanged repaint
    //: costs nothing and disturbs nothing.
    const DRAWN = "__plexoraFailureText";

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    /**
     * Split a message into runs of prose and runs of quoted output.
     *
     * A blank line ends whatever is open, which is what keeps the paragraph
     * before a quote from swallowing the one after it. Nothing here parses
     * meaning -- it reads indentation, and a message with none comes back as
     * one prose block, which is the shape of most of them.
     *
     * @returns `[{quoted, lines}]` in the order they were written.
     */
    function blocks(message) {
        const out = [];
        String(message).split(/\r?\n/).forEach((line) => {
            if (!line.trim()) {
                // A gap between paragraphs, not a line of its own: close the
                // open block so the next line starts a new one.
                if (out.length) out[out.length - 1].closed = true;
                return;
            }
            const quoted = QUOTED.test(line);
            const open = out[out.length - 1];
            if (open && !open.closed && open.quoted === quoted) {
                open.lines.push(line);
                return;
            }
            out.push({ quoted, closed: false, lines: [line] });
        });
        return out;
    }

    /**
     * @function paint - draw `message` into `element`, structure and all.
     *
     * Everything it draws goes inside one wrapper, deliberately: the
     * Settings card puts the message in a flex row, and a handful of sibling
     * paragraphs in one of those would be laid out side by side.
     *
     * @param element the box the message lives in. Kept, not replaced -- the
     *   caller owns it, and owns whether it is hidden.
     * @param message the failure, as the server wrote it. Empty clears.
     * @returns the element, for a caller that is building one.
     */
    function paint(element, message) {
        if (!element) return element;
        const text = message ? String(message) : "";
        if (element[DRAWN] === text) return element;
        element[DRAWN] = text;

        if (!text) {
            element.replaceChildren();
            return element;
        }

        const wrap = el("div", "connect-failure");
        blocks(text).forEach((block) => {
            if (block.quoted) {
                // Dedented: the indent was how the block was MARKED as quoted,
                // and keeping it would waste four columns of a card that has
                // about forty, on every line.
                wrap.append(el("pre", "connect-failure-quote",
                               block.lines
                                   .map((line) => line.replace(QUOTED, ""))
                                   .join("\n")));
            } else {
                // Joined with a space rather than a newline: connect.py wraps
                // its prose at eighty columns for the terminal, and honouring
                // those breaks in a box of a different width is how a
                // paragraph ends up ragged down the middle.
                wrap.append(el("p", "connect-failure-text",
                               block.lines.map((l) => l.trim()).join(" ")));
            }
        });
        element.replaceChildren(wrap);
        return element;
    }

    return { paint, blocks };
})();

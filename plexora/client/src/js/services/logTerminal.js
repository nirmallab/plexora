/**
 * logTerminal.js -- the connection log, behaving like a terminal.
 *
 * Two surfaces show it: the connection modal while a connection is opening,
 * and the Settings page's card for a connection that is already up. They had
 * two implementations and the second one was wrong -- the card was rebuilt on
 * every poll, so the pane was a NEW element once a second and a reader was put
 * back at the top of it, every second, which is precisely when the log has
 * something in it worth reading.
 *
 * So the behaviour lives here, once:
 *
 * **It follows, until you stop it.** New output scrolls into view while the
 * reader is at the bottom. The moment they scroll up it stops -- a pane that
 * yanks itself back down is unreadable -- and scrolling back to the bottom
 * sets it following again. That is what every terminal does and it is the only
 * behaviour that is right in both directions.
 *
 * **It is repainted, never rebuilt.** `paint()` compares what it is about to
 * draw with what is on screen and returns without touching the DOM when they
 * are the same, so a poll that changes nothing costs nothing and cannot
 * disturb a selection.
 *
 * **It says which machine each line came from.** Lines that ssh relayed from
 * the far end arrive prefixed by the process that printed them -- `[ssh]`,
 * `[tunnel]` -- and everything else is Plexora narrating what it is doing.
 * Both belong in one stream, in order, because the interesting moments are
 * exactly where they interleave; the prefix is dimmed so the remote machine's
 * own words are the ones that read as output.
 *
 * The text arrives already redacted (`remote_sessions.redact`, server-side).
 * Nothing here adds a second redactor and nothing here needs one.
 */
window.PlexoraLogTerminal = (function () {
    "use strict";

    //: How close to the bottom still counts as "at the bottom", in pixels.
    //: Fractional scroll heights are normal, and an exact comparison is false
    //: on half the machines that run this.
    const PIN_SLACK = 6;

    //: A line ssh relayed from the far end, as `_Watched` writes it: two
    //: spaces, the process label in brackets, then whatever the remote machine
    //: printed on its own stdout or stderr (which ssh merges into one stream).
    const RELAYED = /^\s*\[([^\]]+)\]\s?(.*)$/;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    /**
     * @function create - a log pane that follows its own output.
     *
     * @param options `title` -- the heading above it; `empty` -- what to say
     *   before there is any output at all.
     * @returns `{element, body, paint, follow, pinned}`. `element` is what to
     *   insert; keep it and repaint it, rather than making a new one, or the
     *   scroll position goes with the old one.
     */
    function create(options = {}) {
        const wrap = el("div", "connect-log");
        if (options.title !== null) {
            wrap.append(el("div", "connect-log-head",
                           options.title || "Connection log"));
        }

        const body = el("pre", "connect-log-body");
        body.tabIndex = 0;
        body.setAttribute("role", "log");
        // polite, not assertive: this updates every second while a connection
        // is opening, and an assertive region would interrupt the screen
        // reader mid-sentence each time it did.
        body.setAttribute("aria-live", "polite");
        if (options.empty) body.setAttribute("data-empty", options.empty);
        wrap.append(body);

        //: Whether the pane is following its output. True until the reader
        //: scrolls up, true again when they come back to the bottom.
        let pinned = true;
        //: What is drawn, so an unchanged poll is a no-op.
        let drawn = null;

        body.addEventListener("scroll", () => {
            pinned = body.scrollTop + body.clientHeight
                >= body.scrollHeight - PIN_SLACK;
        });

        function follow() {
            pinned = true;
            body.scrollTop = body.scrollHeight;
        }

        function paint(lines) {
            const list = lines || [];
            const text = list.join("\n");
            if (text === drawn) return;
            drawn = text;
            body.replaceChildren();
            list.forEach((line) => {
                const match = RELAYED.exec(line);
                if (!match) {
                    body.append(el("span", "connect-log-line", line));
                    return;
                }
                const row = el("span", "connect-log-line is-relayed");
                row.append(el("span", "connect-log-from", match[1]));
                row.append(el("span", "connect-log-said", match[2]));
                body.append(row);
            });
            if (pinned) body.scrollTop = body.scrollHeight;
        }

        return {
            element: wrap,
            body: body,
            paint: paint,
            follow: follow,
            pinned: () => pinned,
        };
    }

    return { create, PIN_SLACK };
})();

/**
 * agentPanel.js -- what an agent is doing in this viewer, and the way to stop it.
 *
 * A small NON-modal card docked in the sidebar's footer (#plexora_ai_dock,
 * index.html: under the layers and the tool cards, above the Cells control,
 * never over the viewer's channel legend), raised by an agent session's
 * events (`plexora:agent-state-changed` whose `kind` ends in `.session`, with
 * `session_id` and `event` in the payload -- gating's automatic-gating
 * session is the one that sends them today; core names no plugin). It shows:
 *
 *   - an orb and the PHASE the server says the session is in, with the marker
 *     it is on ("Inspecting · CD45"). The server owns the phase vocabulary
 *     (autogate schemas.PHASES); `PHASES` below is the one place it is
 *     spelled in JS, and a Python test pins the two together;
 *   - the latest evidence the agent showed (the bridge's `show_evidence`
 *     lands here while the panel is up), enlarged on click;
 *   - a progress line ("4 of 9 markers · CD45 accepted, moderate"), or --
 *     while QC's own deterministic pass runs, well before the first packet --
 *     that pass's own stage ("Scanning channels · scanned CD3 (120/482)"),
 *     read from `progress.bulk` (`session.bulk`: kept beside `progress` the
 *     way `phase` and `subject` already are);
 *   - Continue in background / Watch in viewer, Pause agent / Resume
 *     agent and Stop agent on one row, and a chevron that minimizes the card to a
 *     one-line bar (orb, phase, Watch in viewer while detached, the chevron
 *     back). With the whole sidebar collapsed, a chip under its expand
 *     button says an agent is still at work.
 *
 * Three things are kept apart, each its own state (`session` below):
 *
 *   - the agent running, paused or done (`paused`, `done`; the server's
 *     control.json `paused` / `stopped`);
 *   - the viewer attached or not (`attached`; control.json
 *     `viewer_detached`): attached, the session mirrors what it looks at
 *     into this tab. "Continue in background" detaches -- the run goes on,
 *     this tab gets its own view back (PlexoraAgentBridge.restore) and the
 *     card minimizes; "Watch in viewer" attaches again, and the server
 *     replays the packet the agent is on, so the viewer catches up at once.
 *     A script already under way stops at its next command, so after a
 *     detach at most one command already fetched by the bridge can still
 *     land -- the restore follows the server's answer for that reason;
 *   - the card expanded or minimized (`collapsed`). Expanding never attaches.
 *
 * Take over is the workflow's own (gating's pill, gatingAgentBridge.js): a
 * pause plus a detach on the server, which this card follows from the
 * `control` event. Resume never re-attaches: a run taken over carries on in
 * the background until Watch in viewer. A `control` event carries `paused`,
 * `viewer_attached` and the mirrored tab's `view_id`; it reaches every tab
 * on the project, and only the tab it names counts itself attached.
 *
 * On `finished` it becomes a Done card: one summary line and a Close. It
 * also gives the viewer back (PlexoraAgentBridge.restore) -- the server's own
 * teardown sends `restore_viewer` too, and whichever comes second finds no
 * lease and does nothing.
 *
 * The controls POST `{action}` to the `control.url` every event carries, so
 * core never names a plugin route, and a reloaded tab (which missed
 * `started`) attaches on the first event it does see and can still act.
 *
 * A run of Plexora's own harness (`ai.run_session`, started from the
 * launcher below or over MCP) rides the same channel with its own events
 * (plexora/ai/harness/capabilities.py AI_EVENTS): `ai_run` (the quote),
 * `ai_usage` (a usage line: packets, credits charged, cache-read share),
 * `ai_paused` and a failed `ai_finished` -- a card saying credit ran out or
 * the gateway failed, with Resume (POST /ai/v1/runs {resume_session}) beside
 * Add credits / Not now, the limit card's two-button pattern.
 *
 * The launcher: "Gate with Plexora AI" and "QC with Plexora AI" for the open
 * project, each with the estimate the gateway's price list gives ("~125
 * credits · 5 markers", GET /ai/v1/balance) before anything starts. The
 * sparkle in the sidebar header (#plexora_ai_button, index.html) opens and
 * closes it when the page's licence hint carries `ai` and a project is open
 * -- hidden on Free (no nags); `openLauncher()` still opens it, and then
 * every button is disabled with the reason.
 *
 * A setup question (`needs_setup`: which values to gate on) opens the
 * existing requirements modal for this tab -- not for another tab's mirror.
 *
 * What the agent says (the phase, its subject, the caption, the progress
 * and summary lines) is TYPED in, a few characters at a time, the way a
 * model's tokens arrive -- `type()` below; instant under reduced motion. The
 * typed nodes are hidden from assistive technology, and one visually hidden
 * live region carries each line whole, so a screen reader hears a sentence,
 * not a stream of letters.
 *
 * DOM built with createElement/textContent only: nothing an agent sends is
 * ever parsed as markup. Classic script, `window.PlexoraAgentPanel`.
 */
window.PlexoraAgentPanel = (function () {
    "use strict";

    //: The server's phases (autogate schemas.PHASES), each with the words the
    //: panel says and the orb state it draws (thinking-orbs STATE_TO_MODE keys).
    const PHASES = {
        planning: { label: "Planning", orb: "weaving" },
        analyzing: { label: "Analyzing", orb: "solving" },
        inspecting: { label: "Inspecting", orb: "searching" },
        thinking: { label: "Thinking", orb: "breathing" },
        validating: { label: "Validating", orb: "connecting" },
        waiting: { label: "Waiting for you", orb: "breathing" },
        summarizing: { label: "Summarizing", orb: "composing" },
    };

    //: How a marker's outcome reads in the progress line (autogate
    //: schemas.TERMINAL_STATES).
    const OUTCOMES = {
        accepted: "accepted",
        accepted_low_confidence: "accepted, low confidence",
        manual_review_recommended: "needs review",
        technically_failed: "failed stain, gate at the maximum",
        no_positive_population: "no positive cells, gate at the maximum",
        not_binary: "not two populations, needs review",
        insufficient_information: "not enough to decide, needs review",
        skipped_locked: "skipped, gate locked",
        skipped_excluded: "skipped, excluded",
        skipped_manual: "skipped, gated by hand",
        skipped_no_marker: "skipped, no such marker",
    };

    //: How the bulk pass's own stage reads while it runs (QC bulk.py's
    //: stage names; gating never sends one -- `progress.bulk` is QC-only).
    const BULK_STAGES = {
        calibrating: "Calibrating",
        scanning: "Scanning channels",
        detectors: "Looking for artifacts",
        candidates: "Ranking candidates",
        checks: "Running checks",
        cells: "Checking cells",
    };

    //: How the Done card names the way a session ended (`finished.reason`).
    const ENDINGS = {
        closed: { label: "Done", note: "" },
        committed: { label: "Done", note: "" },
        cancelled: { label: "Cancelled", note: "Cancelled" },
        rolled_back: { label: "Rolled back", note: "Gates rolled back" },
        stopped: { label: "Stopped", note: "Stopped by you" },
    };

    //: Why a Plexora AI run stopped (`ai_paused.reason`), in the user's words.
    const CREDIT_CODES = ["insufficient_credits", "spend_cap_reached", "run_envelope_exceeded"];
    const AI_REASONS = {
        insufficient_credits: "Your Plexora AI credits ran out",
        spend_cap_reached: "Your Plexora AI spending limit was reached",
        run_envelope_exceeded: "This run used everything it was quoted for",
        usage_limit_reached: "Today's Plexora AI limit was reached; it resets at midnight UTC",
        run_closed: "The gateway closed this run",
        ai_disabled: "Plexora AI is switched off for this account",
        ai_not_entitled: "This licence does not include Plexora AI",
        no_license: "Plexora AI needs an activated Paid licence on this machine",
        cancelled: "The run was cancelled",
    };
    const NO_AI = "Plexora AI is part of a Paid licence that includes AI.";

    const MINIMIZE_TITLE = "Minimize. The agent keeps working; its status stays here.";
    const EXPAND_TITLE = "Show the agent's controls";
    const STOP_TITLE = "Stop the agent. Gates it has written so far stay until you or the agent undo them.";
    const CHIP_TITLE = "An agent is working here. Click to show its controls.";
    const DETACH_TITLE = "Give the viewer back to you. The agent keeps working in the background.";
    const ATTACH_TITLE = "Show what the agent is doing in this viewer again, from where it is now.";
    const ROLLBACK_HINT = "The agent can undo them: gating_session_finish(action=\"rollback\")";
    //: The orb is drawn from the engine's 32 px preset and shown at
    //: ORB_DISPLAY -- the phase line's height (agentPanel.css), so the orb
    //: and its words read as one thing; the bar's and the chip's are the
    //: 20 px preset.
    const ORB_SIZE = 32;
    const ORB_DISPLAY = 28;
    const CHIP_ORB_SIZE = 20;
    //: The typing effect: one "token" every TYPE_STEP_MS, and a line of any
    //: length done within TYPE_MAX_MS (a long caption types faster).
    const TYPE_STEP_MS = 26;
    const TYPE_MAX_MS = 1400;

    //: Whether text is typed (a probe turns it off to read lines at once).
    let typing = true;

    //: The one session this tab is showing, or null.
    let current = null;

    // -- small things -----------------------------------------------------------

    function url(path) {
        const clean = String(path || "").replace(/^\/+/, "");
        return (typeof plexoraUrl === "function") ? plexoraUrl(clean) : "/" + clean;
    }

    function datasource() {
        return (window.flaskVariables && window.flaskVariables.datasource) || "";
    }

    function bridge() {
        return window.PlexoraAgentBridge || null;
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text) node.textContent = text;
        return node;
    }

    function button(className, text, title) {
        const node = el("button", className, text);
        node.type = "button";
        if (title) {
            node.title = title;
            node.setAttribute("aria-label", title);
        }
        return node;
    }

    function plural(count, one, many) {
        return `${count} ${count === 1 ? one : (many || one + "s")}`;
    }

    function toast(title, error) {
        window.PlexoraToast?.show?.({
            title, note: error ? String((error && error.message) || error) : "", lines: [], tone: "warning",
        });
    }

    //: The orb's tint when the stylesheet's is unreadable: agentPanel.css
    //: --plx-agent-ink, the silver the panel's words are set in.
    const INK = "#b8c0cc";

    /** The panel's ink (--plx-agent-ink on the card), so the orb and the
     *  words beside it are one colour. */
    function ink(node) {
        try {
            return window.getComputedStyle(node || document.documentElement)
                .getPropertyValue("--plx-agent-ink").trim() || INK;
        } catch (error) {
            return INK;
        }
    }

    function reducedMotion() {
        try {
            return Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
        } catch (error) {
            return false;
        }
    }

    /**
     * Put `text` into `node` progressively. What is already on screen and
     * matches the start of the new text stays (so "3 of 9 markers" growing a
     * clause only types the clause); the rest arrives a token at a time.
     * Asking for the text a node already shows, or is already typing, is a
     * no-op, so render() may call this on every event.
     */
    function type(node, text) {
        const full = String(text || "");
        const running = node._plxTyper;
        if (running && running.full === full) return;
        if (!running && node.textContent === full) return;
        if (running) clearTimeout(running.timer);
        node._plxTyper = null;
        if (!typing || !full || reducedMotion() || typeof setTimeout !== "function") {
            node.classList.remove("is-typing");
            node.textContent = full;
            return;
        }
        const shown = node.textContent;
        let at = 0;
        while (at < shown.length && at < full.length && shown[at] === full[at]) at += 1;
        const step = Math.max(1, Math.ceil((full.length - at) * TYPE_STEP_MS / TYPE_MAX_MS));
        const typer = { full, timer: null };
        node._plxTyper = typer;
        node.classList.add("is-typing");
        node.textContent = full.slice(0, at);
        const tick = () => {
            if (node._plxTyper !== typer) return;
            // Tokens are uneven: now and then one runs a character longer, and
            // one never stops just short of a space.
            let next = Math.min(full.length, at + step + (at % 5 === 3 ? 1 : 0));
            if (full[next] === " ") next += 1;
            at = Math.min(full.length, next);
            node.textContent = full.slice(0, at);
            if (at >= full.length) {
                node._plxTyper = null;
                node.classList.remove("is-typing");
                return;
            }
            typer.timer = setTimeout(tick, TYPE_STEP_MS);
        };
        typer.timer = setTimeout(tick, TYPE_STEP_MS);
    }

    function stopTyping(node) {
        const running = node && node._plxTyper;
        if (!running) return;
        clearTimeout(running.timer);
        node._plxTyper = null;
        node.classList.remove("is-typing");
    }

    function mountOrb(canvas, state, size, display) {
        const orb = window.PlexoraOrb;
        if (!orb || typeof orb.mount !== "function") {
            canvas.setAttribute("data-orb", "static");
            return null;
        }
        try {
            return orb.mount(canvas, { state, size, display, tint: ink(canvas.parentNode), dark: true });
        } catch (error) {
            canvas.setAttribute("data-orb", "static");
            return null;
        }
    }

    function orbState(phase) {
        return (PHASES[phase] || PHASES.thinking).orb;
    }

    function phaseLabel(phase) {
        return (PHASES[phase] || { label: "Working" }).label;
    }

    // -- the card -------------------------------------------------------------------

    function build(id) {
        const root = el("section", "plx-agent-panel is-active");
        root.setAttribute("role", "region");
        root.setAttribute("aria-label", "Agent");
        // Whole lines for a screen reader; the typed ones are aria-hidden.
        const live = el("p", "plx-agent-live");
        live.setAttribute("role", "status");
        live.setAttribute("aria-live", "polite");
        root.dataset.phase = "planning";

        const head = el("header", "plx-agent-head");
        const orbCanvas = el("canvas", "plx-agent-orb");
        const phase = el("div", "plx-agent-phase");
        const phaseName = el("span", "plx-agent-phase-name", "Starting");
        const subject = el("span", "plx-agent-subject");
        subject.hidden = true;
        phase.setAttribute("aria-hidden", "true");
        phase.append(phaseName, subject);
        const toggle = button("plx-agent-toggle", "", MINIMIZE_TITLE);
        toggle.setAttribute("aria-expanded", "true");
        head.append(orbCanvas, phase, toggle);

        const evidence = el("figure", "plx-agent-evidence");
        evidence.hidden = true;
        const thumbButton = button("plx-agent-thumb-button", "", "Enlarge");
        const thumb = el("img", "plx-agent-thumb");
        thumb.alt = "";
        thumbButton.appendChild(thumb);
        const caption = el("figcaption", "plx-agent-caption");
        caption.setAttribute("aria-hidden", "true");
        evidence.append(thumbButton, caption);

        // What the agent is doing, in words for the user (the server's
        // `narration`) -- never the question put to the agent.
        const narration = el("p", "plx-agent-narration");
        narration.setAttribute("aria-hidden", "true");
        narration.hidden = true;
        const limit = el("div", "plx-agent-limit");
        limit.setAttribute("role", "alertdialog");
        limit.hidden = true;
        const limitText = el("p", "plx-agent-limit-text");
        const limitActions = el("div", "plx-agent-limit-actions");
        const limitGo = button("plx-button plx-button-primary plx-agent-button", "Keep going");
        const limitStop = button("plx-button plx-agent-button", "Stop, flag for review");
        limitActions.append(limitGo, limitStop);
        limit.append(limitText, limitActions);
        // A Plexora AI run paused (credit, the gateway): Resume, and a way to
        // top up or put it off -- the limit card's two buttons.
        const credit = el("div", "plx-agent-limit plx-agent-credit");
        credit.setAttribute("role", "alertdialog");
        credit.hidden = true;
        const creditText = el("p", "plx-agent-limit-text");
        const creditActions = el("div", "plx-agent-limit-actions");
        const creditResume = button("plx-button plx-button-primary plx-agent-button", "Resume");
        creditResume.dataset.action = "ai-resume";
        const creditOther = button("plx-button plx-agent-button", "Not now");
        creditOther.dataset.action = "ai-later";
        creditActions.append(creditResume, creditOther);
        credit.append(creditText, creditActions);
        const progress = el("p", "plx-agent-progress", "Getting ready");
        progress.setAttribute("aria-hidden", "true");
        // What a Plexora AI run has spent: packets, credits, cache share.
        const usage = el("p", "plx-agent-usage");
        usage.hidden = true;
        // How the user's note was read (`ai_context`): what the run stands on.
        const context = el("p", "plx-agent-context");
        context.hidden = true;
        const summary = el("p", "plx-agent-summary");
        summary.hidden = true;
        const report = el("p", "plx-agent-report");
        report.hidden = true;
        const hint = el("p", "plx-agent-hint");
        hint.hidden = true;

        const actions = el("div", "plx-agent-actions");
        // The viewer's own: quiet, so Pause and Stop keep their weight.
        const viewer = button("plx-button plx-agent-button plx-agent-button-quiet", "Continue in background");
        viewer.dataset.action = "detach";
        viewer.title = DETACH_TITLE;
        const pause = button("plx-button plx-agent-button", "Pause agent");
        pause.dataset.action = "pause";
        const stop = button("plx-button plx-button-danger plx-agent-button", "Stop agent");
        stop.dataset.action = "stop";
        stop.title = STOP_TITLE;
        const close = button("plx-button plx-button-primary plx-agent-button", "Close");
        close.dataset.action = "close";
        close.hidden = true;
        actions.append(viewer, pause, stop, close);

        // The status lines (progress, spend, how the note was read) sit
        // together, tighter than the panel's own gap.
        const meta = el("div", "plx-agent-meta");
        meta.append(progress, usage, context);
        root.append(live, head, narration, limit, credit, evidence, meta, summary, report, hint, actions);

        // Minimized: one line that still says what the agent is doing.
        const bar = el("div", "plx-agent-bar");
        bar.setAttribute("role", "region");
        bar.setAttribute("aria-label", "Agent");
        bar.hidden = true;
        const barCanvas = el("canvas", "plx-agent-orb plx-agent-orb-chip");
        const barText = el("span", "plx-agent-bar-text");
        const barWatch = button("plx-button plx-agent-button plx-agent-button-quiet plx-agent-bar-watch",
                                "Watch in viewer");
        barWatch.title = ATTACH_TITLE;
        barWatch.hidden = true;
        const barToggle = button("plx-agent-toggle", "", EXPAND_TITLE);
        barToggle.setAttribute("aria-expanded", "false");
        bar.append(barCanvas, barText, barWatch, barToggle);

        // With the whole sidebar collapsed (and the dock with it), a chip
        // under the sidebar's expand button.
        const chip = button("plx-agent-chip", "", CHIP_TITLE);
        chip.hidden = true;
        const chipCanvas = el("canvas", "plx-agent-orb plx-agent-orb-chip");
        const chipText = el("span", "plx-agent-chip-text", "Agent");
        chip.append(chipCanvas, chipText);

        const dock = document.getElementById("plexora_ai_dock");
        const wrapper = document.getElementById("openseadragon_wrapper");
        const host = dock || wrapper || document.body;
        if (dock) {
            root.classList.add("is-docked");
            bar.classList.add("is-docked");
            dock.hidden = false;
        }
        host.appendChild(root);
        host.appendChild(bar);
        (wrapper || document.body).appendChild(chip);

        const session = {
            id, root, bar, chip, host, dock,
            els: { live, orbCanvas, phaseName, subject, toggle, evidence, thumb, thumbButton, caption,
                   narration, limit, limitText, limitGo, limitStop,
                   credit, creditText, creditResume, creditOther, usage, context,
                   progress, summary, report, hint, viewer, pause, stop, close,
                   barCanvas, barText, barWatch, barToggle, chipCanvas, chipText },
            orb: null, barOrb: null, chipOrb: null, observer: null,
            control: null, phase: "planning", subject: "", progress: null, lastLine: "",
            paused: false, done: false, collapsed: false, stopping: false, switching: false,
            attached: true,
            evidence: null, viewId: null,
        };
        session.orb = mountOrb(orbCanvas, orbState("planning"), ORB_SIZE, ORB_DISPLAY);

        toggle.addEventListener("click", () => collapse(session));
        barToggle.addEventListener("click", () => expand(session));
        chip.addEventListener("click", () => openFromChip(session));
        viewer.addEventListener("click", () => (session.attached ? detachViewer(session) : attachViewer(session)));
        barWatch.addEventListener("click", () => attachViewer(session));
        pause.addEventListener("click", () => togglePause(session));
        stop.addEventListener("click", () => stopSession(session));
        limitGo.addEventListener("click", () => answerLimit(session, "continue"));
        limitStop.addEventListener("click", () => answerLimit(session, "stop"));
        creditResume.addEventListener("click", () => resumeRun(session));
        creditOther.addEventListener("click", () => creditLater(session));
        close.addEventListener("click", () => detach(session));
        thumbButton.addEventListener("click", () => enlarge(session));
        watchSidebar(session);
        return session;
    }

    function attach(id) {
        if (current) detach(current);
        current = build(id);
        closeLauncher();
        syncLaunchChip();
        return current;
    }

    function detach(session) {
        if (!session) return;
        session.orb?.destroy();
        session.barOrb?.destroy();
        session.chipOrb?.destroy();
        session.orb = null;
        session.barOrb = null;
        session.chipOrb = null;
        session.observer?.disconnect?.();
        session.observer = null;
        session.root.remove();
        session.bar.remove();
        session.chip.remove();
        if (session.dock && !session.dock.children.length) session.dock.hidden = true;
        Object.values(session.els).forEach(stopTyping);
        if (current === session) current = null;
        syncLaunchChip();
    }

    /** "Scanning channels · scanned CD3 (120/482)" -- the bulk pass's own
     *  stage, message and step, while `bulk.state` says it is still running
     *  (`progress.bulk`, QC's engine folding `bulk_progress` in). */
    function bulkStageLine(bulk) {
        const stage = BULK_STAGES[bulk.stage] || "Working";
        const message = bulk.message ? ` · ${bulk.message}` : "";
        const total = Number(bulk.total);
        const step = Number.isFinite(total) && total > 1
            ? ` (${Math.max(0, Math.min(total, Math.floor(Number(bulk.done) || 0)))}/${total})` : "";
        return `${stage}${message}${step}`;
    }

    /** The progress line's first clause, outside a bulk pass: "N of M
     *  channels" from `by_type.channel` (QC) when it is there, else the
     *  generic units count every workflow (gating included) always sends. */
    function unitsLine(session, progress) {
        const channels = progress.by_type && progress.by_type.channel;
        if (channels && Number(channels.total) > 0) {
            return `${Number(channels.done) || 0} of ${plural(Number(channels.total), "channel")}`;
        }
        if (Number.isFinite(Number(progress.units_total)) && Number(progress.units_total) > 0) {
            const noun = (session.labels && session.labels.unit_noun) || "marker";
            return `${Number(progress.units_done) || 0} of ${plural(Number(progress.units_total), noun)}`;
        }
        return "";
    }

    /** A short, secondary clause for the other unit types QC's `by_type`
     *  carries ("checks 2/6 · cell checks 1/9"), when there are any. */
    function otherCountsLine(progress) {
        const by = progress.by_type || {};
        const parts = [];
        if (by.check && Number(by.check.total) > 0) {
            parts.push(`checks ${Number(by.check.done) || 0}/${by.check.total}`);
        }
        if (by.cells && Number(by.cells.total) > 0) {
            parts.push(`cell checks ${Number(by.cells.done) || 0}/${by.cells.total}`);
        }
        return parts.join(" · ");
    }

    function render(session) {
        const { els } = session;
        const label = session.paused ? "Paused" : (session.pending ? "Starting" : phaseLabel(session.phase));
        const head = `AI agent ${label.toLowerCase()}`;
        type(els.phaseName, head);
        type(els.subject, session.subject ? ` · ${session.subject}` : "");
        els.subject.hidden = !session.subject;
        session.root.dataset.phase = session.phase;
        session.root.classList.toggle("is-paused", session.paused);
        els.pause.textContent = session.paused ? "Resume agent" : "Pause agent";
        els.pause.dataset.action = session.paused ? "resume" : "pause";
        // A card up before its session exists has nothing to pause or stop yet.
        els.pause.disabled = session.stopping || Boolean(session.pending);
        els.stop.disabled = session.stopping || Boolean(session.pending);
        const busy = session.stopping || Boolean(session.pending) || session.switching;
        els.viewer.textContent = session.attached ? "Continue in background" : "Watch in viewer";
        els.viewer.dataset.action = session.attached ? "detach" : "attach";
        els.viewer.title = session.attached ? DETACH_TITLE : ATTACH_TITLE;
        els.viewer.disabled = busy;
        els.barWatch.hidden = session.attached || session.done;
        els.barWatch.disabled = busy;
        session.root.classList.toggle("is-detached", !session.attached);
        type(els.barText, `${head}${session.subject ? ` · ${session.subject}` : ""}`);
        els.chipText.textContent = `Agent · ${label}`;
        const line = [];
        const progress = session.progress || {};
        const bulk = session.bulk || {};
        if (bulk.state === "bulk_running" && bulk.stage) {
            line.push(bulkStageLine(bulk));
        } else {
            const units = unitsLine(session, progress);
            if (units) line.push(units);
            const others = otherCountsLine(progress);
            if (others) line.push(others);
        }
        if (session.lastLine) line.push(session.lastLine);
        if (session.stopping) line.push("Stopping");
        if (line.length) type(els.progress, line.join(" · "));
        els.narration.hidden = !session.narration;
        if (session.narration) type(els.narration, session.narration);
        const spent = usageLine(session);
        els.usage.hidden = !spent;
        if (spent && els.usage.textContent !== spent) els.usage.textContent = spent;
        const read = contextLine(session.aiContext);
        els.context.hidden = !read.text;
        if (read.text && els.context.textContent !== read.text) els.context.textContent = read.text;
        els.context.title = read.title;
        const said = session.narration ? ` ${session.narration}` : "";
        const spoken = `${head}${session.subject ? ` · ${session.subject}` : ""}.${said} ${line.join(" · ")}`;
        if (els.live.textContent !== spoken) els.live.textContent = spoken;
        const state = orbState(session.phase);
        session.orb?.setState(state);
        session.barOrb?.setState(state);
        session.chipOrb?.setState(state);
        if (session.paused) {
            session.orb?.pause();
            session.barOrb?.pause();
            session.chipOrb?.pause();
        } else if (!session.done) {
            if (!session.collapsed) session.orb?.resume();
            session.barOrb?.resume();
            session.chipOrb?.resume();
        }
        syncChip(session);
    }

    function credits(value) {
        const n = Number(value) || 0;
        return n >= 100 ? String(Math.round(n)) : String(Math.round(n * 10) / 10);
    }

    /** "Plexora AI · 12 packets · 3.4 credits · 87% from cache", or the
     *  quote before the first packet; "" when no harness run drives this. */
    function usageLine(session) {
        const used = session.aiUsage;
        if (used && Number(used.packets) > 0) {
            const parts = ["Plexora AI", plural(Number(used.packets), "packet"),
                           `${credits(used.charged_credits)} credits`];
            const share = Number(used.cache_read_share);
            if (Number.isFinite(share) && share > 0) parts.push(`${Math.round(share * 100)}% from cache`);
            return parts.join(" · ");
        }
        if (session.aiRun && session.aiRun.quote_credits !== undefined && session.aiRun.quote_credits !== null) {
            return `Plexora AI · at most ${credits(session.aiRun.quote_credits)} credits`;
        }
        return session.aiRun ? "Plexora AI" : "";
    }

    /** How the run read the user's note, from `ai_context`: one line
     *  ("Context: melanoma · skin · every marker") and, on hover, the note as
     *  typed, its normalised form and anything left unclear. A note that
     *  could not be interpreted says so in the line itself ("Your note was
     *  passed on unread"), not only on hover. Strings only. */
    function contextLine(reading) {
        if (!reading || typeof reading !== "object") return { text: "", title: "" };
        const said = reading.interpretation && typeof reading.interpretation === "object" ? reading.interpretation : {};
        const ctx = said.context && typeof said.context === "object" ? said.context : {};
        const scope = said.gating_scope && typeof said.gating_scope === "object" ? said.gating_scope : {};
        const parts = ["disease", "tissue", "species"].map((k) => ctx[k]).filter((v) => typeof v === "string" && v);
        const units = Number(reading.units) || 0;
        const requested = Array.isArray(scope.requested_markers) ? scope.requested_markers.map(String) : [];
        if (scope.mode === "selected_markers" && requested.length) {
            parts.push(requested.length <= 4 ? `only ${requested.join(", ")}` : `only ${plural(requested.length, "marker")}`);
        } else if (Array.isArray(scope.excluded_markers) && scope.excluded_markers.length) {
            parts.push(`skipping ${scope.excluded_markers.map(String).join(", ")}`);
        } else {
            parts.push(units ? `all ${plural(units, "marker")}` : "every marker");
        }
        const unclear = Array.isArray(said.ambiguities) ? said.ambiguities.map(String) : [];
        const title = [
            said.original_text ? `You wrote: ${String(said.original_text)}` : "",
            said.normalized_text ? `Read as: ${String(said.normalized_text)}` : "",
            said.source === "unprocessed" ? "Passed on as written (it could not be interpreted)." : "",
            said.problem ? `Why: ${String(said.problem)}` : "",
            ...unclear.map((a) => `Unclear: ${a}`),
        ].filter(Boolean).join("\n");
        if (said.source === "unprocessed") {
            return { text: `Your note was passed on unread · ${parts[parts.length - 1]} · see note`, title };
        }
        return { text: `Context: ${parts.join(" · ")}${unclear.length ? " · see note" : ""}`, title };
    }

    /** What the paused-run card shows, from an `ai_paused` / failed
     *  `ai_finished` event: only strings, and `resume` only as an object. */
    function pauseOf(payload, reason, message) {
        return {
            reason: reason ? String(reason) : "",
            message: message ? String(message) : "",
            top_up_url: typeof payload.top_up_url === "string" ? payload.top_up_url : "",
            resume: payload.resume && typeof payload.resume === "object" ? payload.resume : null,
        };
    }

    /** The paused-run card: why, Resume, and Add credits (when the gateway
     *  gave a top-up page) or Not now. */
    function showCredit(session) {
        const { els } = session;
        const pause = session.aiPause;
        els.credit.hidden = !pause;
        if (!pause) return;
        const reason = String(pause.reason || "");
        const said = AI_REASONS[reason]
            || (pause.message ? `Plexora AI could not continue: ${pause.message}` : "Plexora AI could not continue");
        const tail = CREDIT_CODES.includes(reason)
            ? ". Everything decided so far is kept; add credits, then resume where it stopped."
            : ". Everything decided so far is kept; resume when you are ready.";
        els.creditText.textContent = said + tail;
        els.creditResume.disabled = Boolean(session.resuming) || !pause.resume;
        els.creditOther.textContent = pause.top_up_url ? "Add credits" : "Not now";
        els.creditOther.dataset.action = pause.top_up_url ? "ai-top-up" : "ai-later";
    }

    async function resumeRun(session) {
        const pause = session.aiPause;
        if (!pause || !pause.resume || session.resuming) return;
        session.resuming = true;
        showCredit(session);
        try {
            await startRun(pause.resume);
            session.aiPause = null;
            session.lastLine = "Resuming";
        } catch (error) {
            toast("Plexora AI did not resume", error);
        } finally {
            session.resuming = false;
            showCredit(session);
            if (!session.done) render(session);
        }
    }

    function creditLater(session) {
        const pause = session.aiPause;
        if (pause && pause.top_up_url) {
            openExternal(String(pause.top_up_url));
            return;
        }
        session.aiPause = null;
        showCredit(session);
    }

    function openExternal(target) {
        if (!/^https:\/\//.test(target)) return;
        if (window.PlexoraDesktop && typeof window.PlexoraDesktop.openUrl === "function") {
            Promise.resolve(window.PlexoraDesktop.openUrl(target)).catch(() => {});
            return;
        }
        if (typeof window.open === "function") window.open(target, "_blank", "noopener");
    }

    // -- Plexora AI runs ------------------------------------------------------------

    async function aiFetch(path, options = {}) {
        const response = await fetch(url(path), Object.assign({
            credentials: "same-origin", headers: { "Accept": "application/json", "Content-Type": "application/json" },
        }, options));
        const body = await response.json().catch(() => ({}));
        if (!response.ok || body.success === false) {
            const error = new Error(body.error || `HTTP ${response.status}`);
            error.status = response.status;
            error.body = body;
            throw error;
        }
        return body;
    }

    /** POST /ai/v1/runs; resolves the run (`{run_id, job_id}`). */
    function startRun(args) {
        return aiFetch("ai/v1/runs", { method: "POST", body: JSON.stringify(args) });
    }

    function aiAllowed() {
        const paid = window.PlexoraPaid;
        return Boolean(paid && typeof paid.allows === "function" && paid.allows("ai"));
    }

    //: The launcher's card, or null.
    let launcher = null;

    //: Plexora AI's tools: the name and line the launcher shows, its action,
    //: and the unit its estimate counts. Which of them a project is offered
    //: is MODALITIES' call, and then the estimate's (a tool its data cannot
    //: run is left out, not shown disabled).
    const KINDS = [
        { kind: "gating", name: "AI Gating", label: "Gate with Plexora AI", noun: "marker",
          about: "Automatically phenotype cells using marker expression and image context.",
          context: { start: "Start gating",
                     help: "Every marker is gated unless you ask for fewer, e.g. \u201conly gate CD3 and CD8\u201d." } },
        { kind: "qc", name: "AI Quality Control", label: "QC with Plexora AI", noun: "channel",
          about: "Detect focus, registration, segmentation and other image-quality issues." },
    ];

    function launchHost() {
        return document.getElementById("openseadragon_wrapper") || document.body;
    }

    /** Without an `ai` licence the AI button says this, and nothing else:
     *  no trial, just the way to enter a licence. */
    async function explainLocked() {
        const answer = await window.PlexoraConfirm?.choose?.({
            title: "Plexora AI",
            body: ["Plexora AI is under development and needs a licence to access it."],
            choices: [{ value: null, label: "Close" },
                      { value: "license", label: "Enter License…", kind: "primary", focus: true }],
        });
        if (answer === "license") window.PlexoraPaid?.goToLicense?.();
        return answer;
    }

    /** The sidebar header's AI button (#plexora_ai_button), always shown: with
     *  an `ai` licence hint it opens the launcher (or closes it when it is
     *  up); without one it explains that Plexora AI needs a licence. */
    function syncLaunchChip() {
        const spark = document.getElementById("plexora_ai_button");
        if (!spark) return null;
        if (!spark.dataset.bound) {
            spark.dataset.bound = "1";
            spark.addEventListener("click", () => {
                if (launcher) closeLauncher();
                else if (aiAllowed()) openLauncher();
                else explainLocked();
            });
        }
        spark.hidden = false;
        spark.setAttribute("aria-expanded", launcher ? "true" : "false");
        return spark;
    }

    function closeLauncher() {
        if (!launcher) return;
        launcher.root.remove();
        launcher.backdrop?.remove();
        if (typeof document.removeEventListener === "function") {
            document.removeEventListener("keydown", launcher.onKey);
        }
        launcher = null;
        syncLaunchChip();
        document.getElementById("plexora_ai_button")?.focus?.();
    }

    /** "2,000": whole credits from 100 up, one decimal under it. */
    function creditCount(value) {
        const n = Number(value) || 0;
        const rounded = n >= 100 ? Math.round(n) : Math.round(n * 10) / 10;
        return rounded.toLocaleString("en-US");
    }

    /** One tool's state from the balance answer: `{disabled, credits, detail}`,
     *  or `{omit: true}` when the open project's data cannot run it. */
    function kindState(kind, answer) {
        if (!aiAllowed()) return { disabled: true, detail: NO_AI };
        if (!datasource()) return { disabled: true, detail: "Open a project first." };
        if (current && !current.done) return { disabled: true, detail: "An agent session is already running here." };
        if (!answer) return { disabled: true, detail: "Estimating" };
        if (answer.error) return { disabled: true, detail: String(answer.error) };
        const estimate = (answer.estimates || {})[kind.kind] || {};
        const units = Number(estimate.units) || 0;
        if (estimate.unavailable || units <= 0) return { omit: true };
        const cost = `~${creditCount(estimate.credits)} credits`;
        const detail = ` · ${plural(units, estimate.unit || kind.noun)}`;
        if (estimate.affordable === false) {
            return { disabled: true, credits: cost,
                     detail: `${detail} · you have ${creditCount(answer.available_credits)}` };
        }
        return { disabled: false, credits: cost, detail };
    }

    function paintLauncher() {
        if (!launcher) return;
        const answer = launcher.answer;
        if (launcher.step) {
            paintContext(launcher.step, answer);
            return;
        }
        KINDS.forEach((kind) => {
            const row = launcher.rows[kind.kind];
            if (!row) return;     // not a tool for this project's data
            if (launcher.starting) {
                row.button.disabled = true;
                return;
            }
            const state = kindState(kind, answer);
            row.card.hidden = Boolean(state.omit);
            row.button.disabled = Boolean(state.omit || state.disabled);
            row.cost.textContent = state.credits || "";
            row.cost.hidden = !state.credits;
            row.detail.textContent = state.credits ? state.detail : (state.detail || "");
            row.button.title = row.note.textContent;
        });
        launcher.sections.forEach(({ section, kinds }) => {
            section.hidden = !kinds.some((k) => !launcher.rows[k].card.hidden);
        });
        launcher.none.hidden = launcher.sections.some(({ section }) => !section.hidden);
        const known = Boolean(answer && !answer.error && answer.available_credits !== undefined);
        launcher.balance.hidden = !known;
        launcher.balanceText.textContent = known ? `${creditCount(answer.available_credits)} credits available` : "";
        launcher.more.hidden = aiAllowed();
    }

    async function launch(kind, note = null) {
        if (!launcher || launcher.starting) return;
        if (kind.context && note === null) {
            askContext(kind);
            return;
        }
        launcher.starting = true;
        const row = launcher.rows[kind.kind];
        row.cost.hidden = true;
        row.detail.textContent = "Starting";
        paintLauncher();
        try {
            // A run launched here is watched here: the session mirrors into
            // this tab (named, since another tab may show the same project).
            const live = bridge();
            const viewId = live && typeof live.sessionId === "function" ? live.sessionId() : null;
            // An empty note is no note: the server makes no interpreter call.
            const text = String(note || "").trim();
            const run = await startRun({
                kind: kind.kind, project: datasource(),
                start_options: viewId ? { mirror: true, view_id: viewId } : { mirror: true },
                ...(text ? { context: text } : {}),
            });
            closeLauncher();
            showPending(run.run_id, text);
            watchStart(run.run_id);
        } catch (error) {
            if (launcher) launcher.starting = false;
            paintLauncher();
            toast(`${kind.label} did not start`, error);
        }
    }

    //: The longest note the server takes (ai.run_session `context`).
    const CONTEXT_MAX = 1000;

    /** A tool that takes a note about the sample (`kind.context`) asks for it
     *  first: the launcher's tools give way to one optional field, in the
     *  same card. Whatever is typed goes to the server as written -- a cheap
     *  model normalises it there (plexora/ai/context.py) -- and empty skips
     *  that call. Back returns to the tools; Enter starts, Shift+Enter is a
     *  new line. */
    function askContext(kind) {
        const mine = launcher;
        if (!mine || mine.step) return;
        const step = el("div", "plx-ai-context");
        step.dataset.kind = kind.kind;
        const input = el("textarea", "plx-ai-context-input");
        input.id = "plx_ai_context_input";
        input.rows = 3;
        input.maxLength = CONTEXT_MAX;
        input.placeholder = "e.g. \u201cThis is a melanoma skin sample.\u201d";
        input.setAttribute("aria-describedby", "plx_ai_context_help");
        const label = el("label", "plx-ai-context-label", "Add context");
        label.setAttribute("for", input.id);
        label.appendChild(el("span", "plx-ai-context-optional", "optional"));
        const help = el("p", "plx-ai-context-help", kind.context.help);
        help.id = "plx_ai_context_help";
        const actions = el("div", "plx-ai-context-actions");
        const back = button("plx-button plx-agent-button plx-ai-back", "Back");
        back.dataset.action = "ai-context-back";
        const note = el("p", "plx-ai-note plx-ai-context-note");
        const cost = el("span", "plx-ai-cost");
        const detail = el("span", "plx-ai-detail");
        note.append(cost, detail);
        const start = button("plx-button plx-agent-button plx-ai-go", kind.context.start);
        start.dataset.action = "ai-context-start";
        actions.append(back, note, start);
        step.append(label, input, help, actions);
        const go = () => launch(kind, input.value || "");
        start.addEventListener("click", go);
        back.addEventListener("click", () => leaveContext());
        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
                event.preventDefault?.();
                go();
            }
        });
        mine.toolsView.forEach((node) => { node.dataset.wasHidden = node.hidden ? "1" : ""; node.hidden = true; });
        mine.title.textContent = kind.name;
        mine.root.appendChild(step);
        mine.step = { kind, root: step, input, start, back, cost, detail };
        paintLauncher();
        input.focus?.();
    }

    function leaveContext() {
        const mine = launcher;
        if (!mine || !mine.step || mine.starting) return;
        const kind = mine.step.kind;
        mine.step.root.remove();
        mine.step = null;
        mine.title.textContent = "Plexora AI";
        mine.toolsView.forEach((node) => { node.hidden = node.dataset.wasHidden === "1"; });
        paintLauncher();
        mine.rows[kind.kind]?.button.focus?.();
    }

    /** The context step's estimate and Start button: the tool's own state. */
    function paintContext(step, answer) {
        const state = launcher.starting ? { disabled: true } : kindState(step.kind, answer);
        step.start.disabled = Boolean(state.omit || state.disabled);
        step.start.textContent = launcher.starting ? "Starting\u2026" : step.kind.context.start;
        step.input.disabled = Boolean(launcher.starting);
        step.back.disabled = Boolean(launcher.starting);
        step.cost.textContent = state.credits || "";
        step.cost.hidden = !state.credits;
        step.detail.textContent = state.credits ? state.detail : (launcher.starting ? "" : state.detail || "");
    }

    /** The corner card at once, before the session's first event (reading
     *  the note and loading the project take a few seconds): "AI agent
     *  starting", no Pause or Stop yet. The first event adopts it. */
    function showPending(runId, note) {
        if (!runId || (current && !current.done)) return;
        const session = attach(null);
        session.pending = String(runId);
        // The launcher asks for this tab to be mirrored into.
        session.attached = true;
        session.lastLine = note ? "Reading your note" : "Getting ready";
        const wait = "Available once the session has started.";
        session.els.pause.title = wait;
        session.els.stop.title = wait;
        render(session);
    }

    function dropPending(runId) {
        if (current && current.pending === runId && !current.done) detach(current);
    }

    /** Until the session's first event arrives: a run that ends before it
     *  has one (no licence on this machine, the gateway unreachable) says why. */
    function watchStart(runId, tries = 30) {
        if (!runId || typeof setTimeout !== "function") return;
        setTimeout(async () => {
            if (current && !current.done && !current.pending) return;
            let run = null;
            try {
                run = await aiFetch(`ai/v1/runs/${encodeURIComponent(runId)}`);
            } catch (error) {
                if (tries > 1) watchStart(runId, tries - 1);
                else dropPending(runId);
                return;
            }
            if (["failed", "paused", "stopped"].includes(run.status)) {
                dropPending(runId);
                toast("Plexora AI stopped", AI_REASONS[run.reason] || run.reason || run.status);
                return;
            }
            if (tries > 1 && run.status !== "done") watchStart(runId, tries - 1);
            else dropPending(runId);
        }, 2000);
    }

    /** The launcher card, for the open project. Resolves once its estimate
     *  has been asked for (a probe waits on it). */
    //: The data modalities Plexora AI knows, and the tools each can be given
    //: to. A modality is detected from the open project (its reference
    //: image's modality, or a layer of it); only one with a tool its data can
    //: run is shown. A new tool is a KINDS entry plus its id here.
    const MODALITIES = [
        { id: "multiplex", label: "Multiplexed imaging", image: ["multiplex"], layers: [], kinds: ["gating", "qc"] },
        { id: "xenium", label: "Spatial transcriptomics", image: ["xenium_morphology"],
          layers: ["transcripts"], kinds: [] },
        { id: "visium", label: "Visium HD", image: [], layers: ["visium_bins", "visium_spots"], kinds: [] },
        { id: "he", label: "H&E and brightfield", image: ["he"], layers: [], kinds: [] },
    ];

    /** The open project's modalities: {image, layers:Set}, or null when the
     *  layer stack is not there to ask (then multiplexed imaging is assumed,
     *  and the gateway's estimate still leaves out what cannot run). */
    function projectModalities() {
        try {
            const list = window.__plexora?.layers?.layers?.() || [];
            if (!list.length) return null;
            const of = (layer) => layer?.spec?.modality || layer?.modality || "";
            const reference = list.find((layer) => layer.id === "__image__");
            return { image: of(reference), layers: new Set(list.map(of).filter(Boolean)) };
        } catch (error) {
            return null;
        }
    }

    /** The modalities detected here that have at least one tool. */
    function modalitiesHere() {
        const found = projectModalities();
        if (!found) return [MODALITIES[0]];
        return MODALITIES.filter((m) => m.kinds.length && (m.image.includes(found.image)
            || m.layers.some((name) => found.layers.has(name))));
    }

    function openLauncher() {
        if (launcher) return Promise.resolve(launcher);
        // A centred modal over the whole page, on a backdrop that closes it.
        const backdrop = el("div", "plx-ai-backdrop");
        const root = el("section", "plx-agent-panel plx-ai-launcher");
        root.setAttribute("role", "dialog");
        root.setAttribute("aria-modal", "true");
        root.setAttribute("aria-label", "Plexora AI");
        const head = el("header", "plx-agent-head plx-ai-head");
        const title = el("h2", "plx-ai-title", "Plexora AI");
        const hide = button("plx-ai-close", "", "Close");
        hide.dataset.action = "ai-close";
        head.append(title, hide);
        const intro = el("p", "plx-ai-intro",
            "AI-powered analysis tailored to your current data. "
            + "Review the estimated credit usage before starting any run.");
        root.append(head, intro);
        const rows = {};
        const sections = [];
        const groups = modalitiesHere();
        groups.forEach((group) => {
            const section = el("section", "plx-ai-modality");
            section.dataset.modality = group.id;
            section.appendChild(el("h3", "plx-ai-modality-name", group.label));
            const kinds = KINDS.filter((kind) => group.kinds.includes(kind.kind));
            kinds.forEach((kind) => {
                const card = el("div", "plx-ai-tool");
                card.dataset.kind = kind.kind;
                const icon = el("span", "plx-ai-tool-icon");
                icon.dataset.icon = kind.kind;
                icon.setAttribute("aria-hidden", "true");
                const body = el("div", "plx-ai-tool-body");
                const go = button("plx-button plx-agent-button plx-ai-go", kind.label);
                go.dataset.action = `ai-${kind.kind}`;
                const note = el("p", "plx-ai-note");
                const cost = el("span", "plx-ai-cost");
                const detail = el("span", "plx-ai-detail");
                note.append(cost, detail);
                const row = el("div", "plx-ai-row");
                row.append(go, note);
                body.append(el("h4", "plx-ai-tool-name", kind.name), el("p", "plx-ai-tool-about", kind.about), row);
                card.append(icon, body);
                section.appendChild(card);
                go.addEventListener("click", () => launch(kind));
                rows[kind.kind] = { card, button: go, note, cost, detail };
            });
            root.appendChild(section);
            sections.push({ section, kinds: kinds.map((kind) => kind.kind) });
        });
        const none = el("p", "plx-ai-none", "No Plexora AI tools can run on the data loaded here yet.");
        const balance = el("p", "plx-ai-balance");
        const coin = el("span", "plx-ai-coin");
        coin.setAttribute("aria-hidden", "true");
        const balanceText = el("span", "plx-ai-balance-text");
        balance.append(coin, balanceText);
        balance.hidden = true;
        const more = button("plx-button plx-agent-button", "About Plexora AI");
        more.dataset.action = "ai-explain";
        more.hidden = true;
        more.addEventListener("click", () => {
            window.PlexoraPaid?.explain?.({ entitlement: "ai", label: "Plexora AI" });
        });
        const chat = button("plx-button plx-agent-button", "Chat with Plexora AI");
        chat.dataset.action = "ai-chat";
        chat.hidden = !window.PlexoraChatPanel || !document.getElementById("openseadragon_wrapper");
        chat.addEventListener("click", () => {
            closeLauncher();
            window.PlexoraChatPanel?.open?.();
        });
        root.append(none, balance, chat, more);
        // What the context step hides while it is up (and puts back).
        const toolsView = [intro, ...sections.map(({ section }) => section), none, balance, chat, more];
        hide.addEventListener("click", () => closeLauncher());
        backdrop.addEventListener("click", () => closeLauncher());
        const onKey = (event) => {
            if (event.key === "Escape") closeLauncher();
        };
        if (typeof document.addEventListener === "function") document.addEventListener("keydown", onKey);
        document.body.append(backdrop, root);
        launcher = { root, backdrop, onKey, rows, sections, groups, none, balance, balanceText, more,
                     title, toolsView, step: null, answer: null, starting: false };
        syncLaunchChip();
        paintLauncher();
        const first = Object.values(rows).map((row) => row.button).find((b) => !b.disabled) || hide;
        first.focus?.();
        if (!aiAllowed() || !datasource() || !Object.keys(rows).length) return Promise.resolve(launcher);
        const mine = launcher;
        return aiFetch(`ai/v1/balance?project=${encodeURIComponent(datasource())}`)
            .then((answer) => { mine.answer = answer; })
            .catch((error) => { mine.answer = { error: error.message || String(error) }; })
            .then(() => {
                paintLauncher();
                if (mine === launcher) {
                    const ready = Object.values(mine.rows).find((row) => !row.card.hidden && !row.button.disabled);
                    ready?.button.focus?.();
                }
                return mine;
            });
    }

    /** Minimize to the bar. Only the card's size: the agent and the
     *  viewer stay as they are. */
    function collapse(session) {
        if (session.collapsed) return;
        session.collapsed = true;
        session.root.hidden = true;
        session.bar.hidden = false;
        session.orb?.pause();
        if (!session.barOrb) {
            session.barOrb = mountOrb(session.els.barCanvas, orbState(session.phase), CHIP_ORB_SIZE);
        }
        if (session.paused) session.barOrb?.pause();
        else session.barOrb?.resume();
        session.els.barToggle.focus?.();
        render(session);
    }

    /** Back to the whole card -- never re-attaching the viewer. */
    function expand(session) {
        if (!session.collapsed) return;
        session.collapsed = false;
        session.root.hidden = false;
        session.bar.hidden = true;
        session.barOrb?.destroy();
        session.barOrb = null;
        if (!session.paused && !session.done) session.orb?.resume();
        if (!session.done) render(session);
    }

    function sidebarCollapsed() {
        const shell = document.getElementById("bodyDiv");
        return Boolean(shell && shell.classList && shell.classList.contains("sidebar-collapsed"));
    }

    /** The chip shows while the sidebar (and so the dock) is collapsed and
     *  an agent is still at work; it is the dock's stand-in, so a card
     *  mounted elsewhere never needs one. */
    function syncChip(session) {
        if (!session || !session.chip) return;
        const show = Boolean(session.dock) && !session.done && sidebarCollapsed();
        if (session.chip.hidden !== show) return;
        session.chip.hidden = !show;
        if (show) {
            session.chipOrb = session.chipOrb || mountOrb(session.els.chipCanvas, orbState(session.phase), CHIP_ORB_SIZE);
            if (session.paused) session.chipOrb?.pause();
            else session.chipOrb?.resume();
        } else {
            session.chipOrb?.destroy();
            session.chipOrb = null;
        }
    }

    function watchSidebar(session) {
        const shell = document.getElementById("bodyDiv");
        if (!shell || !session.dock || typeof MutationObserver !== "function") return;
        try {
            session.observer = new MutationObserver(() => syncChip(session));
            session.observer.observe(shell, { attributes: true, attributeFilter: ["class"] });
        } catch (error) {
            session.observer = null;
        }
    }

    /** The chip opens the sidebar (the same toggle as its expand button)
     *  and the card in it. */
    function openFromChip(session) {
        if (sidebarCollapsed()) document.getElementById("sidebar_expand_button")?.click?.();
        expand(session);
        syncChip(session);
    }

    // -- talking to the session ------------------------------------------------------

    async function control(session, action, extra = {}) {
        const target = session.control && session.control.url;
        if (!target) throw new Error("this session did not say where to send that");
        const response = await fetch(url(target), {
            method: "POST",
            credentials: "same-origin",
            headers: { "Accept": "application/json", "Content-Type": "application/json" },
            body: JSON.stringify({ action, datasource: datasource(), ...extra }),
        });
        if (!response.ok) throw new Error(`the session did not accept "${action}" (HTTP ${response.status})`);
        return response.json().catch(() => ({}));
    }

    async function togglePause(session) {
        if (session.done || session.stopping) return;
        const action = session.paused ? "resume" : "pause";
        const was = session.paused;
        // Optimistic: the click is the answer the user needs; the `control`
        // event that follows confirms it (and a second tab follows too).
        session.paused = !was;
        render(session);
        try {
            await control(session, action);
        } catch (error) {
            session.paused = was;
            render(session);
            toast(was ? "The agent did not resume" : "The agent did not pause", error);
        }
    }

    async function stopSession(session) {
        if (session.done || session.stopping) return;
        session.stopping = true;
        render(session);
        try {
            await control(session, "stop");
        } catch (error) {
            session.stopping = false;
            render(session);
            toast("The agent did not stop", error);
            return;
        }
        // At once, not after the route's `finished`: the agent's next call is
        // when it hears about the stop, and the viewer is the user's again now.
        restoreViewer("stopped");
    }

    /** "Continue in background": the run goes on, the viewer is the user's
     *  again and the card minimizes. The view is put back once the server
     *  has switched the mirror off, so no later command takes it again. */
    async function detachViewer(session) {
        if (session.done || session.stopping || session.switching || !session.attached) return;
        session.switching = true;
        session.attached = false;
        collapse(session);
        try {
            await control(session, "detach_viewer");
        } catch (error) {
            session.attached = true;
            session.switching = false;
            expand(session);
            render(session);
            toast("The viewer could not be given back", error);
            return;
        }
        session.switching = false;
        render(session);
        restoreViewer("detached");
    }

    /** "Watch in viewer": mirror into this tab again. The server replays
     *  the packet the agent is on; the card stays the size it is. */
    async function attachViewer(session) {
        if (session.done || session.stopping || session.switching || session.attached) return;
        const live = bridge();
        const viewId = live && typeof live.sessionId === "function" ? live.sessionId() : null;
        if (!viewId) {
            toast("This tab cannot show the agent yet", "the viewer is still connecting; try again in a moment");
            return;
        }
        session.switching = true;
        session.attached = true;
        session.viewId = viewId;
        render(session);
        try {
            await control(session, "attach_viewer", { view_id: viewId });
        } catch (error) {
            session.attached = false;
            session.switching = false;
            render(session);
            toast("The agent could not be shown here", error);
            return;
        }
        session.switching = false;
        render(session);
    }

    /** The first open limit question, or nothing. Answered here or by the
     *  agent (`limit_answered`); both close it. */
    function showLimit(session) {
        const { els } = session;
        const item = (session.pendingLimits || [])[0];
        els.limit.hidden = !item || session.done;
        if (!item || session.done) return;
        els.limitText.textContent = `${item.label || item.marker} has used ${item.words}, and the evidence still `
            + "says to keep looking. Keep going, or stop and flag it for manual review?";
    }

    async function answerLimit(session, decision) {
        const item = (session.pendingLimits || [])[0];
        if (!item || session.answeringLimit) return;
        const { els } = session;
        session.answeringLimit = true;
        els.limitGo.disabled = true;
        els.limitStop.disabled = true;
        try {
            await control(session, "limit", { marker: item.marker, decision });
            session.pendingLimits = (session.pendingLimits || []).filter((p) => p.marker !== item.marker);
        } catch (error) {
            toast("The agent did not take the answer", error);
        } finally {
            session.answeringLimit = false;
            els.limitGo.disabled = false;
            els.limitStop.disabled = false;
            showLimit(session);
        }
    }

    function restoreViewer(reason) {
        const live = bridge();
        if (!live || typeof live.restore !== "function") return null;
        return Promise.resolve().then(() => live.restore({ reason })).catch((error) => {
            console.error("agentPanel: the viewer could not be given back", error);
        });
    }

    function enlarge(session) {
        const shown = session.evidence;
        const live = bridge();
        if (!shown || !live || typeof live.showEvidence !== "function") return;
        try {
            live.showEvidence({ src: shown.src, caption: shown.caption, title: shown.title || "From your agent" });
        } catch (error) {
            toast("That image could not be opened", error);
        }
    }

    // -- the setup question --------------------------------------------------------

    function thisTab(viewId) {
        if (!viewId) return true;
        const live = bridge();
        const mine = live && typeof live.sessionId === "function" ? live.sessionId() : null;
        return !mine || mine === viewId;
    }

    /**
     * Ask the user what the session is waiting on, in the requirements modal
     * every tool uses. Through `collect`, not `require`: `require` resolves
     * without asking for `features`, which the project always counts as
     * answered -- the question here is "are these the right values", and it
     * has to be put.
     */
    async function openSetup(needs) {
        const requirements = window.PlexoraRequirements;
        if (!requirements || typeof requirements.collect !== "function") return false;
        const ds = datasource();
        const tool = needs.tool || "gating";
        const keys = Array.isArray(needs.keys) && needs.keys.length ? needs.keys : ["features"];
        let payload = null;
        try {
            const response = await fetch(url(`${encodeURIComponent(ds)}/tools/${encodeURIComponent(tool)}/requirements`),
                { credentials: "same-origin" });
            payload = await response.json();
        } catch (error) {
            payload = null;
        }
        const names = (list) => (Array.isArray(list) ? list : []).some((row) => row && keys.includes(row.key));
        let form;
        if (payload && payload.success !== false && (names(payload.missing) || names(payload.confirm))) {
            form = payload;
        } else {
            form = Object.assign({}, payload || {}, {
                missing: (payload && Array.isArray(payload.missing)) ? payload.missing : [],
                confirm: (Array.isArray(needs.requirements) ? needs.requirements : [])
                    .map((row) => Object.assign({}, row, { optional: false })),
                optional: [],
            });
            delete form.success;
        }
        return requirements.collect(ds, form);
    }

    // -- the events --------------------------------------------------------------

    function adoptProgress(session, payload) {
        if (!payload.progress || typeof payload.progress !== "object") return;
        session.progress = payload.progress;
        // QC's own shortcut, kept beside `progress` the way `phase` and
        // `subject` already are: the bulk pass's stage, while it runs
        // (`progress.bulk`, QC's engine folding `bulk_progress` in).
        if (payload.progress.bulk && typeof payload.progress.bulk === "object") {
            session.bulk = payload.progress.bulk;
        }
    }

    function adoptPhase(session, payload) {
        if (payload.phase && PHASES[payload.phase]) session.phase = payload.phase;
    }

    function summaryLine(summary, reason) {
        const s = summary || {};
        // A workflow that words its own summary (QC) says it; gating's
        // buckets are read below.
        if (typeof s.text === "string" && s.text) {
            const ended = (ENDINGS[reason] || {}).note;
            return ended ? `${s.text} · ${ended}` : s.text;
        }
        const n = (key) => Number(s[key]) || 0;
        const parts = [];
        if (n("units_total")) parts.push(plural(n("units_total"), "marker"));
        // The server's buckets overlap on purpose (engine.summary_of):
        // `accepted` includes the low-confidence ones, and `empty` (a gate
        // written at the maximum) includes the `failed` stains.
        if (n("accepted")) {
            parts.push(n("accepted_low_confidence")
                ? `${n("accepted")} accepted (${n("accepted_low_confidence")} low confidence)`
                : `${n("accepted")} accepted`);
        }
        if (n("review")) parts.push(`${n("review")} ${n("review") === 1 ? "needs" : "need"} review`);
        const noPositives = Math.max(0, n("empty") - n("failed"));
        if (noPositives) parts.push(`${noPositives} no positive cells`);
        if (n("failed")) parts.push(plural(n("failed"), "failed stain"));
        if (n("skipped")) parts.push(`${n("skipped")} skipped`);
        if (n("written")) parts.push(`${plural(n("written"), "gate")} written`);
        if (n("replayed")) parts.push(`${plural(n("replayed"), "earlier answer")} reused`);
        const note = (ENDINGS[reason] || {}).note;
        if (note) parts.push(note);
        return parts.join(" · ") || "Finished";
    }

    /** `{src, caption, title}` for the first usable entry of an event's
     *  `evidence` list, or null. Only an artifact id is taken (the captures
     *  route on this server), never a URL an event names. */
    function firstEvidence(list) {
        if (!Array.isArray(list)) return null;
        const usable = list.filter((item) => item && item.artifact_id !== undefined
            && item.artifact_id !== null && /^[\w.-]+$/.test(String(item.artifact_id)));
        if (!usable.length) return null;
        const item = usable[0];
        const more = usable.length > 1 ? ` (+${usable.length - 1} more)` : "";
        return {
            src: url(`agent/v1/captures/${encodeURIComponent(String(item.artifact_id))}`),
            caption: `${item.caption ? String(item.caption) : ""}${more}`,
            title: item.title ? String(item.title) : "",
        };
    }

    function setEvidence(session, args) {
        session.evidence = { src: String(args.src), caption: args.caption ? String(args.caption) : "",
                             title: args.title ? String(args.title) : "" };
        const { els } = session;
        if (els.thumb.getAttribute("src") !== session.evidence.src) els.thumb.src = session.evidence.src;
        els.thumb.alt = session.evidence.caption || "The agent's latest evidence";
        type(els.caption, session.evidence.caption || session.evidence.title);
        els.caption.title = session.evidence.caption || "";
        els.evidence.hidden = false;
    }

    //: One per server event (autogate schemas.SESSION_EVENTS).
    const HANDLERS = {
        started(session, payload) {
            adoptPhase(session, payload);
            adoptProgress(session, payload);
            session.viewId = payload.view_id || session.viewId;
            // Attached only when the session mirrors into THIS tab (another
            // tab may show the same project).
            session.attached = (payload.viewer_attached === undefined || Boolean(payload.viewer_attached))
                && Boolean(payload.view_id) && thisTab(payload.view_id);
            session.lastLine = "";
            // QC's bulk pass (the scan, the detectors, the checks) runs as
            // this job, well before the first packet; kept for a probe, and
            // so a reloaded tab's first "started" has it too.
            if (payload.job_id) session.job = String(payload.job_id);
            // The workflow's own words (unit noun, outcomes, finish tool),
            // when it sends them; gating's constants otherwise.
            if (payload.labels && typeof payload.labels === "object") session.labels = payload.labels;
        },
        control(session, payload) {
            if (payload.paused !== undefined) session.paused = Boolean(payload.paused);
            if (payload.view_id) session.viewId = String(payload.view_id);
            if (payload.viewer_attached !== undefined) {
                // Every tab on the project hears this; only the one it names
                // is being mirrored into.
                const named = payload.view_id || session.viewId;
                session.attached = Boolean(payload.viewer_attached) && Boolean(named) && thisTab(named);
            }
        },
        issued(session, payload) {
            adoptPhase(session, payload);
            adoptProgress(session, payload);
            session.narration = payload.narration ? String(payload.narration) : "";
            const subject = payload.subject || payload.marker
                || (Array.isArray(payload.markers) ? payload.markers.join(", ") : "");
            session.subject = subject ? String(subject) : "";
            // The exact pictures the model was sent (`evidence`, a list of
            // `{artifact_id, caption, title, width, height}`), served by the
            // captures route -- shown whether or not a viewer is mirrored.
            const shown = firstEvidence(payload.evidence);
            if (shown) setEvidence(session, shown);
        },
        phase(session, payload) {
            adoptPhase(session, payload);
            // The bulk pass's own throttled progress rides a `phase` event
            // too (bulk.py's `_progress_announcer`); a plain phase change
            // (gating's, or QC's between packets) carries no `progress` and
            // `adoptProgress` is a no-op for it.
            adoptProgress(session, payload);
        },
        answered(session, payload) {
            adoptPhase(session, payload);
            adoptProgress(session, payload);
            if (payload.narration) session.narration = String(payload.narration);
        },
        unit_closed(session, payload) {
            const own = session.labels && session.labels.outcomes;
            const words = (typeof payload.outcome_text === "string" && payload.outcome_text)
                || (own && own[payload.state]) || OUTCOMES[payload.state]
                || String(payload.state || "closed").replace(/_/g, " ");
            const confidence = typeof payload.confidence === "string" && payload.confidence
                && payload.state !== "accepted_low_confidence" ? `, ${payload.confidence}` : "";
            session.lastLine = `${payload.marker || "A marker"} ${words}${confidence}`;
            session.els.progress.title = payload.reason ? String(payload.reason) : "";
            // Counted now; `answered`, which follows, carries the server's count.
            if (session.progress && Number.isFinite(Number(session.progress.units_done))) {
                session.progress = Object.assign({}, session.progress, {
                    units_done: Math.min(Number(session.progress.units_total) || Infinity,
                                         Number(session.progress.units_done) + 1),
                });
            }
        },
        limit_reached(session, payload) {
            adoptPhase(session, payload);
            // `unit` is what the control route is told (a workflow's unit
            // key); `label` what the user reads. Gating sends the marker.
            const marker = payload.unit ? String(payload.unit)
                : (payload.marker ? String(payload.marker) : "A marker");
            const label = payload.label ? String(payload.label) : marker;
            session.pendingLimits = session.pendingLimits || [];
            if (!session.pendingLimits.some((item) => item.marker === marker)) {
                session.pendingLimits.push({ marker, label, words: String(payload.words || "its allowance") });
            }
            session.narration = payload.label
                ? `${label} needs more looks before it can be decided.`
                : `${marker} needs more looks before its gate can be trusted.`;
            showLimit(session);
        },
        limit_answered(session, payload) {
            const answers = payload.answers && typeof payload.answers === "object" ? payload.answers : {};
            session.pendingLimits = (session.pendingLimits || []).filter((item) => !(item.marker in answers));
            showLimit(session);
        },
        needs_setup(session, payload) {
            session.phase = "planning";
            session.lastLine = "Waiting for you: which values to gate on";
            if (!thisTab(payload.view_id)) return;
            if (session.setupOpen) return;
            session.setupOpen = true;
            Promise.resolve().then(() => openSetup(payload.needs || {})).catch((error) => {
                toast("The setup question could not be opened", error);
            }).finally(() => { session.setupOpen = false; });
        },
        finished(session, payload) {
            if (session.done) return;
            const reason = payload.reason || "closed";
            const ending = ENDINGS[reason] || ENDINGS.closed;
            session.done = true;
            session.paused = false;
            session.stopping = false;
            session.subject = "";
            if (session.collapsed) expand(session);
            const { els } = session;
            session.root.classList.remove("is-active", "is-paused");
            session.root.dataset.phase = "done";
            type(els.phaseName, ending.label);
            stopTyping(els.subject);
            stopTyping(els.progress);
            els.subject.hidden = true;
            const line = summaryLine(payload.summary, reason);
            type(els.summary, line);
            els.live.textContent = `${ending.label}. ${line}`;
            els.summary.hidden = false;
            els.progress.hidden = true;
            const written = Number(payload.summary && payload.summary.written) || 0;
            const finishTool = session.labels && session.labels.finish_tool;
            els.hint.textContent = finishTool
                ? `The agent can undo them: ${finishTool}(action="rollback")` : ROLLBACK_HINT;
            els.hint.hidden = !(written > 0 && reason !== "rolled_back");
            els.pause.hidden = true;
            els.stop.hidden = true;
            els.viewer.hidden = true;
            els.toggle.hidden = true;
            els.barWatch.hidden = true;
            els.close.hidden = false;
            session.orb?.pause();
            syncChip(session);
            restoreViewer(reason);
        },
        ai_run(session, payload) {
            session.aiRun = { run_id: payload.run_id, quote_credits: payload.quote_credits, kind: payload.kind };
            if (payload.resumed) {
                session.aiPause = null;
                showCredit(session);
            }
        },
        ai_context(session, payload) {
            session.aiContext = { interpretation: payload.interpretation, units: payload.units };
        },
        ai_usage(session, payload) {
            if (!session.aiRun) session.aiRun = { run_id: payload.run_id, kind: payload.kind };
            if (payload.usage && typeof payload.usage === "object") session.aiUsage = payload.usage;
        },
        ai_paused(session, payload) {
            if (payload.usage && typeof payload.usage === "object") session.aiUsage = payload.usage;
            session.aiPause = pauseOf(payload, payload.reason, payload.message);
            showCredit(session);
        },
        ai_finished(session, payload) {
            if (payload.usage && typeof payload.usage === "object") session.aiUsage = payload.usage;
            // A run that failed with its session still open (a gateway error
            // that is not about credit): the same card, worded for it.
            if (payload.status === "failed" && !session.done) {
                session.aiPause = pauseOf(payload, payload.reason || "gateway_error", payload.reason);
                showCredit(session);
            }
        },
        report(session, payload) {
            const paths = Array.isArray(payload.paths) ? payload.paths.map(String) : [];
            const line = session.els.report;
            line.textContent = "Report written";
            line.title = paths.join("\n");
            line.hidden = false;
        },
    };

    function handle(payload) {
        const handler = HANDLERS[payload.event];
        if (!handler) return;
        let session = current;
        const ending = payload.event === "finished" || payload.event === "report" || payload.event === "ai_finished";
        if (session && session.pending && !session.done && !ending) {
            // The card put up at launch becomes this session's.
            session.id = payload.session_id;
            session.pending = null;
            session.els.pause.title = "";
            session.els.stop.title = STOP_TITLE;
        }
        if (!session || session.id !== payload.session_id) {
            // A reloaded tab missed `started`: the first live event attaches.
            // An ending for a session this tab never showed is not news.
            if (payload.event === "finished" || payload.event === "report" || payload.event === "ai_finished") return;
            session = attach(payload.session_id);
        } else if (payload.event === "started" && session.done) {
            session = attach(payload.session_id);
        }
        if (payload.control && typeof payload.control === "object") session.control = payload.control;
        if (session.done && payload.event !== "finished" && payload.event !== "report") return;
        handler(session, payload);
        if (!session.done) render(session);
    }

    /** A reloaded tab missed `started`, and the session may say nothing for
     *  a while (a model call): a run already going on this project has its
     *  card put back now, from the run's `session` snapshot. The next live
     *  event carries on from it. */
    async function reattachRunning() {
        if (current || !aiAllowed() || !datasource()) return null;
        try {
            const list = await aiFetch("ai/v1/runs?limit=10");
            const row = (list.runs || []).find((r) => r.project === datasource()
                && r.status === "running" && r.session_id);
            if (!row || current) return null;
            const run = await aiFetch(`ai/v1/runs/${encodeURIComponent(row.run_id)}`);
            const live = run.session;
            if (!live || live.stopped || current) return null;
            const id = String(live.session_id);
            handle({ session_id: id, event: "started", control: live.control,
                     phase: live.phase, progress: live.progress });
            handle({ session_id: id, event: "control", paused: Boolean(live.paused),
                     ...(live.viewer_attached !== undefined ? { viewer_attached: Boolean(live.viewer_attached) } : {}),
                     ...(live.view_id ? { view_id: String(live.view_id) } : {}) });
            handle({ session_id: id, event: "ai_run", run_id: row.run_id, kind: row.kind });
            return id;
        } catch (error) {
            return null;    // nothing to put back; the next event still attaches
        }
    }

    function onEvent(event) {
        const detail = (event && event.detail) || {};
        if (!String(detail.kind || "").endsWith(".session")) return;
        const payload = detail.payload || {};
        if (!payload.session_id || !payload.event) return;
        try {
            handle(payload);
        } catch (error) {
            console.error("agentPanel: could not take on", payload.event, error);
        }
    }

    if (typeof window.addEventListener === "function") {
        window.addEventListener("plexora:agent-state-changed", onEvent);
    }
    // The header sparkle, once the page (and its licence hint) is in place,
    // and the card of a run this tab was showing before a reload.
    const boot = () => {
        syncLaunchChip();
        reattachRunning();
    };
    try {
        if (document.readyState === "loading" && typeof document.addEventListener === "function") {
            document.addEventListener("DOMContentLoaded", boot);
        } else {
            boot();
        }
    } catch (error) {
        console.error("agentPanel: the Plexora AI button could not be set up", error);
    }

    return {
        /** Whether a live (not finished) session card is up -- the bridge's
         *  question before it routes evidence here. */
        isAttached: () => Boolean(current && !current.done),
        /** The latest evidence, in place: `{src, caption, title, subject, kind}`. */
        showEvidence(args = {}) {
            const session = current;
            if (!session || session.done || !args.src) return false;
            setEvidence(session, args);
            if (args.subject && !session.subject) {
                session.subject = String(args.subject);
                render(session);
            }
            return true;
        },
        /** The session on screen (for a probe): `{id, phase, paused, done,
         *  collapsed, attached, job, bulk}` -- `job` and `bulk` are QC's (the
         *  bulk pass's job id, and its latest stage while it runs). */
        current: () => (current ? { id: current.id, phase: current.phase, paused: current.paused,
                                    done: current.done, collapsed: current.collapsed,
                                    attached: current.attached, job: current.job, bulk: current.bulk } : null),
        PHASES,
        OUTCOMES,
        /** The Plexora AI launcher for the open project (resolves once its
         *  estimate has been asked for). */
        openLauncher,
        closeLauncher,
        /** The launcher on screen (for a probe): `{buttons: {gating, qc}}`
         *  as `{disabled, note, label}`, the balance line; or null. */
        launcher: () => (launcher ? {
            buttons: Object.fromEntries(Object.entries(launcher.rows).filter(([, r]) => !r.card.hidden)
                .map(([k, r]) => [k, { disabled: r.button.disabled, note: r.note.textContent,
                                       label: r.button.textContent }])),
            balance: launcher.balance.hidden ? "" : launcher.balance.textContent,
            modalities: launcher.sections.filter(({ section }) => !section.hidden).map(({ section }) => section.dataset.modality),
            empty: !launcher.none.hidden,
            modal: launcher.root.parentNode === document.body && launcher.backdrop.parentNode === document.body,
            title: launcher.title.textContent,
            context: launcher.step ? {
                kind: launcher.step.kind.kind, start: launcher.step.start.textContent,
                disabled: launcher.step.start.disabled, note: launcher.step.cost.textContent + launcher.step.detail.textContent,
                placeholder: launcher.step.input.placeholder, help: launcher.step.root.children[2].textContent,
                toolsHidden: launcher.toolsView.every((node) => node.hidden),
            } : null,
        } : null),
        syncLaunchChip,
        /** `{typing: false}` shows every line at once (a probe reads them). */
        configure(options = {}) {
            if (options.typing !== undefined) typing = Boolean(options.typing);
        },
        //: Test seams.
        _handle: handle,
        _reattach: reattachRunning,
        _openSetup: openSetup,
    };
})();

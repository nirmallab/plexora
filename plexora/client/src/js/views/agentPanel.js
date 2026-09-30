/**
 * agentPanel.js -- what an agent is doing in this viewer, and the way to stop it.
 *
 * A small NON-modal card in the viewer's corner, raised by an agent session's
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
 *   - a progress line ("4 of 9 markers · CD45 accepted, moderate");
 *   - Pause agent / Resume agent, Stop agent, and Hide -- which only hides:
 *     the agent keeps working, and a chip stays in the corner.
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

    //: How the Done card names the way a session ended (`finished.reason`).
    const ENDINGS = {
        closed: { label: "Done", note: "" },
        committed: { label: "Done", note: "" },
        cancelled: { label: "Cancelled", note: "Cancelled" },
        rolled_back: { label: "Rolled back", note: "Gates rolled back" },
        stopped: { label: "Stopped", note: "Stopped by you" },
    };

    const HIDE_TITLE = "Hide this panel. The agent keeps working; a small chip stays in this corner.";
    const CHIP_TITLE = "The agent keeps working. Click to show its panel.";
    const ROLLBACK_HINT = "The agent can undo them: gating_session_finish(action=\"rollback\")";
    //: The orb is drawn from the engine's 32 px preset and shown at
    //: ORB_DISPLAY -- the phase line's height (agentPanel.css), so the orb
    //: and its words read as one thing; the chip's is the 20 px preset.
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
        const hide = button("plx-agent-hide", "Hide", HIDE_TITLE);
        head.append(orbCanvas, phase, hide);

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
        const progress = el("p", "plx-agent-progress", "Getting ready");
        progress.setAttribute("aria-hidden", "true");
        const summary = el("p", "plx-agent-summary");
        summary.hidden = true;
        const report = el("p", "plx-agent-report");
        report.hidden = true;
        const hint = el("p", "plx-agent-hint");
        hint.hidden = true;

        const actions = el("div", "plx-agent-actions");
        const pause = button("plx-button plx-agent-button", "Pause agent");
        pause.dataset.action = "pause";
        const stop = button("plx-button plx-button-danger plx-agent-button", "Stop agent");
        stop.dataset.action = "stop";
        stop.title = "Stop the agent. Gates it has written so far stay until you or the agent undo them.";
        const close = button("plx-button plx-button-primary plx-agent-button", "Close");
        close.dataset.action = "close";
        close.hidden = true;
        actions.append(pause, stop, close);

        root.append(live, head, narration, limit, evidence, progress, summary, report, hint, actions);

        const chip = button("plx-agent-chip", "", CHIP_TITLE);
        chip.hidden = true;
        const chipCanvas = el("canvas", "plx-agent-orb plx-agent-orb-chip");
        const chipText = el("span", "plx-agent-chip-text", "Agent");
        chip.append(chipCanvas, chipText);

        const host = document.getElementById("openseadragon_wrapper") || document.body;
        host.appendChild(root);
        host.appendChild(chip);

        const session = {
            id, root, chip, host,
            els: { live, orbCanvas, phaseName, subject, hide, evidence, thumb, thumbButton, caption,
                   narration, limit, limitText, limitGo, limitStop,
                   progress, summary, report, hint, pause, stop, close, chipCanvas, chipText },
            orb: null, chipOrb: null,
            control: null, phase: "planning", subject: "", progress: null, lastLine: "",
            paused: false, done: false, collapsed: false, stopping: false,
            evidence: null, viewId: null,
        };
        session.orb = mountOrb(orbCanvas, orbState("planning"), ORB_SIZE, ORB_DISPLAY);

        hide.addEventListener("click", () => collapse(session));
        chip.addEventListener("click", () => expand(session));
        pause.addEventListener("click", () => togglePause(session));
        stop.addEventListener("click", () => stopSession(session));
        limitGo.addEventListener("click", () => answerLimit(session, "continue"));
        limitStop.addEventListener("click", () => answerLimit(session, "stop"));
        close.addEventListener("click", () => detach(session));
        thumbButton.addEventListener("click", () => enlarge(session));
        return session;
    }

    function attach(id) {
        if (current) detach(current);
        current = build(id);
        return current;
    }

    function detach(session) {
        if (!session) return;
        session.orb?.destroy();
        session.chipOrb?.destroy();
        session.orb = null;
        session.chipOrb = null;
        session.root.remove();
        session.chip.remove();
        Object.values(session.els).forEach(stopTyping);
        if (current === session) current = null;
    }

    function render(session) {
        const { els } = session;
        const label = session.paused ? "Paused" : phaseLabel(session.phase);
        const head = `AI agent ${label.toLowerCase()}`;
        type(els.phaseName, head);
        type(els.subject, session.subject ? ` · ${session.subject}` : "");
        els.subject.hidden = !session.subject;
        session.root.dataset.phase = session.phase;
        session.root.classList.toggle("is-paused", session.paused);
        els.pause.textContent = session.paused ? "Resume agent" : "Pause agent";
        els.pause.dataset.action = session.paused ? "resume" : "pause";
        els.pause.disabled = session.stopping;
        els.stop.disabled = session.stopping;
        els.chipText.textContent = `Agent · ${label}`;
        const line = [];
        const progress = session.progress || {};
        if (Number.isFinite(Number(progress.units_total)) && Number(progress.units_total) > 0) {
            const noun = (session.labels && session.labels.unit_noun) || "marker";
            line.push(`${Number(progress.units_done) || 0} of ${plural(Number(progress.units_total), noun)}`);
        }
        if (session.lastLine) line.push(session.lastLine);
        if (session.stopping) line.push("Stopping");
        if (line.length) type(els.progress, line.join(" · "));
        els.narration.hidden = !session.narration;
        if (session.narration) type(els.narration, session.narration);
        const said = session.narration ? ` ${session.narration}` : "";
        const spoken = `${head}${session.subject ? ` · ${session.subject}` : ""}.${said} ${line.join(" · ")}`;
        if (els.live.textContent !== spoken) els.live.textContent = spoken;
        const state = orbState(session.phase);
        session.orb?.setState(state);
        session.chipOrb?.setState(state);
        if (session.paused) {
            session.orb?.pause();
            session.chipOrb?.pause();
        } else if (!session.done) {
            if (!session.collapsed) session.orb?.resume();
            session.chipOrb?.resume();
        }
    }

    function collapse(session) {
        if (session.collapsed) return;
        session.collapsed = true;
        session.root.hidden = true;
        session.chip.hidden = false;
        session.orb?.pause();
        if (!session.chipOrb) {
            session.chipOrb = mountOrb(session.els.chipCanvas, orbState(session.phase), CHIP_ORB_SIZE);
        }
        if (session.paused) session.chipOrb?.pause();
        else session.chipOrb?.resume();
        render(session);
    }

    function expand(session) {
        if (!session.collapsed) return;
        session.collapsed = false;
        session.root.hidden = false;
        session.chip.hidden = true;
        session.chipOrb?.destroy();
        session.chipOrb = null;
        if (!session.paused && !session.done) session.orb?.resume();
        render(session);
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
        if (payload.progress && typeof payload.progress === "object") session.progress = payload.progress;
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
            session.lastLine = "";
            // The workflow's own words (unit noun, outcomes, finish tool),
            // when it sends them; gating's constants otherwise.
            if (payload.labels && typeof payload.labels === "object") session.labels = payload.labels;
        },
        control(session, payload) {
            if (payload.paused !== undefined) session.paused = Boolean(payload.paused);
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
            els.hide.hidden = true;
            els.close.hidden = false;
            session.orb?.pause();
            restoreViewer(reason);
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
        if (!session || session.id !== payload.session_id) {
            // A reloaded tab missed `started`: the first live event attaches.
            // An ending for a session this tab never showed is not news.
            if (payload.event === "finished" || payload.event === "report") return;
            session = attach(payload.session_id);
        } else if (payload.event === "started" && session.done) {
            session = attach(payload.session_id);
        }
        if (payload.control && typeof payload.control === "object") session.control = payload.control;
        if (session.done && payload.event !== "finished" && payload.event !== "report") return;
        handler(session, payload);
        if (!session.done) render(session);
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
        /** The session on screen (for a probe): `{id, phase, paused, done, collapsed}`. */
        current: () => (current ? { id: current.id, phase: current.phase, paused: current.paused,
                                    done: current.done, collapsed: current.collapsed } : null),
        PHASES,
        OUTCOMES,
        /** `{typing: false}` shows every line at once (a probe reads them). */
        configure(options = {}) {
            if (options.typing !== undefined) typing = Boolean(options.typing);
        },
        //: Test seams.
        _handle: handle,
        _openSetup: openSetup,
    };
})();

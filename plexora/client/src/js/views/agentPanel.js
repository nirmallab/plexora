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
 *   - a progress line ("4 of 9 markers · CD45 accepted, moderate"), or --
 *     while QC's own deterministic pass runs, well before the first packet --
 *     that pass's own stage ("Scanning channels · scanned CD3 (120/482)"),
 *     read from `progress.bulk` (`session.bulk`: kept beside `progress` the
 *     way `phase` and `subject` already are);
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
 * A run of Plexora's own harness (`ai.run_session`, started from the
 * launcher below or over MCP) rides the same channel with its own events
 * (plexora/ai/harness/capabilities.py AI_EVENTS): `ai_run` (the quote),
 * `ai_usage` (a usage line: packets, credits charged, cache-read share),
 * `ai_paused` and a failed `ai_finished` -- a card saying credit ran out or
 * the gateway failed, with Resume (POST /ai/v1/runs {resume_session}) beside
 * Add credits / Not now, the limit card's two-button pattern.
 *
 * The launcher: "Gate with Plexora AI" and "QC with Plexora AI" for the open
 * project, each with the estimate the gateway's price list gives ("About 125
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

        root.append(live, head, narration, limit, credit, evidence, progress, usage, summary, report, hint,
                    actions);

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
                   credit, creditText, creditResume, creditOther, usage,
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
        creditResume.addEventListener("click", () => resumeRun(session));
        creditOther.addEventListener("click", () => creditLater(session));
        close.addEventListener("click", () => detach(session));
        thumbButton.addEventListener("click", () => enlarge(session));
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
        session.chipOrb?.destroy();
        session.orb = null;
        session.chipOrb = null;
        session.root.remove();
        session.chip.remove();
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

    const KINDS = [
        { kind: "gating", label: "Gate with Plexora AI", noun: "marker" },
        { kind: "qc", label: "QC with Plexora AI", noun: "channel" },
    ];

    function launchHost() {
        return document.getElementById("openseadragon_wrapper") || document.body;
    }

    /** The sidebar header's sparkle (#plexora_ai_button): shown only with an
     *  `ai` licence hint and a project open; it opens the launcher, or closes
     *  it when it is up. */
    function syncLaunchChip() {
        const spark = document.getElementById("plexora_ai_button");
        if (!spark) return null;
        if (!spark.dataset.bound) {
            spark.dataset.bound = "1";
            spark.addEventListener("click", () => (launcher ? closeLauncher() : openLauncher()));
        }
        spark.hidden = !(aiAllowed() && Boolean(datasource()));
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

    /** One kind's button state from the balance answer: `{disabled, note}`. */
    function kindState(kind, answer) {
        if (!aiAllowed()) return { disabled: true, note: NO_AI };
        if (!datasource()) return { disabled: true, note: "Open a project first." };
        if (current && !current.done) return { disabled: true, note: "An agent session is already running here." };
        if (!answer) return { disabled: true, note: "Asking Plexora AI for an estimate" };
        if (answer.error) return { disabled: true, note: String(answer.error) };
        const estimate = (answer.estimates || {})[kind.kind] || {};
        if (estimate.unavailable) return { disabled: true, note: String(estimate.unavailable) };
        const units = Number(estimate.units) || 0;
        const about = `About ${credits(estimate.credits)} credits · ${plural(units, estimate.unit || kind.noun)}`;
        if (estimate.affordable === false) {
            return { disabled: true, note: `${about}. You have ${credits(answer.available_credits)} credits.` };
        }
        return { disabled: false, note: about };
    }

    function paintLauncher() {
        if (!launcher) return;
        const answer = launcher.answer;
        KINDS.forEach((kind) => {
            const row = launcher.rows[kind.kind];
            if (!row) return;     // not a workflow for this project's data
            const state = launcher.starting ? { disabled: true, note: row.note.textContent } : kindState(kind, answer);
            row.button.disabled = state.disabled;
            row.note.textContent = state.note;
            row.button.title = state.note;
        });
        const known = Boolean(answer && !answer.error && answer.available_credits !== undefined);
        launcher.balance.hidden = !known;
        launcher.balance.textContent = known ? `${credits(answer.available_credits)} credits available` : "";
        launcher.more.hidden = aiAllowed();
    }

    async function launch(kind) {
        if (!launcher || launcher.starting) return;
        launcher.starting = true;
        launcher.rows[kind.kind].note.textContent = "Starting";
        paintLauncher();
        try {
            const run = await startRun({ kind: kind.kind, project: datasource() });
            closeLauncher();
            watchStart(run.run_id);
        } catch (error) {
            if (launcher) launcher.starting = false;
            paintLauncher();
            toast(`${kind.label} did not start`, error);
        }
    }

    /** Until the session's first event arrives: a run that ends before it
     *  has one (no licence on this machine, the gateway unreachable) says why. */
    function watchStart(runId, tries = 30) {
        if (!runId || typeof setTimeout !== "function") return;
        setTimeout(async () => {
            if (current && !current.done) return;
            let run = null;
            try {
                run = await aiFetch(`ai/v1/runs/${encodeURIComponent(runId)}`);
            } catch (error) {
                return;
            }
            if (["failed", "paused", "stopped"].includes(run.status)) {
                toast("Plexora AI stopped", AI_REASONS[run.reason] || run.reason || run.status);
                return;
            }
            if (tries > 1 && run.status !== "done") watchStart(runId, tries - 1);
        }, 2000);
    }

    /** The launcher card, for the open project. Resolves once its estimate
     *  has been asked for (a probe waits on it). */
    //: Plexora AI's workflows by data modality: what each kind of data can
    //: be given to. A group shows when the open project holds that data (its
    //: reference image's modality, or a layer of it); a group with no
    //: workflows yet says so rather than vanishing.
    const MODALITIES = [
        { id: "multiplex", label: "Multiplexed imaging", image: ["multiplex"], layers: [],
          kinds: ["gating", "qc"],
          about: "Phenotype cells by gating each marker, and check the image itself: focus, "
              + "registration and segmentation." },
        { id: "xenium", label: "Spatial transcriptomics (Xenium)", image: ["xenium_morphology"],
          layers: ["transcripts"], kinds: [] },
        { id: "visium", label: "Visium HD", image: [], layers: ["visium_bins", "visium_spots"], kinds: [] },
        { id: "he", label: "H&E and brightfield", image: ["he"], layers: [], kinds: [] },
    ];

    /** The open project's modalities: {image, layers:Set}, or null when the
     *  layer stack is not there to ask (then multiplexed imaging is assumed,
     *  and the gateway's estimate still says if a run cannot happen). */
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

    function modalitiesHere() {
        const found = projectModalities();
        if (!found) return [MODALITIES[0]];
        const here = MODALITIES.filter((m) => m.image.includes(found.image)
            || m.layers.some((name) => found.layers.has(name)));
        here.sort((x, y) => Number(y.kinds.length > 0) - Number(x.kinds.length > 0));
        return here.length ? here : [MODALITIES[0]];
    }

    function openLauncher() {
        if (launcher) return Promise.resolve(launcher);
        // A centred modal over the whole page, on a backdrop that closes it.
        const backdrop = el("div", "plx-ai-backdrop");
        const root = el("section", "plx-agent-panel plx-ai-launcher");
        root.setAttribute("role", "dialog");
        root.setAttribute("aria-modal", "true");
        root.setAttribute("aria-label", "Plexora AI");
        const head = el("header", "plx-agent-head");
        const title = el("div", "plx-agent-phase", "Plexora AI");
        const hide = button("plx-agent-hide", "Close", "Close");
        hide.dataset.action = "ai-close";
        head.append(title, hide);
        const intro = el("p", "plx-agent-narration",
            "Plexora answers every decision itself and bills it in Plexora AI credits. "
            + "Each run is quoted before anything is spent.");
        root.append(head, intro);
        const rows = {};
        const groups = modalitiesHere();
        groups.forEach((group) => {
            const section = el("section", "plx-ai-modality");
            section.dataset.modality = group.id;
            section.appendChild(el("h3", "plx-ai-modality-name", group.label));
            if (group.about) section.appendChild(el("p", "plx-ai-modality-about", group.about));
            const kinds = KINDS.filter((kind) => group.kinds.includes(kind.kind));
            if (!kinds.length) {
                section.appendChild(el("p", "plx-ai-note plx-ai-none",
                    "No Plexora AI workflows for this data yet."));
            }
            kinds.forEach((kind) => {
                const row = el("div", "plx-ai-row");
                const go = button("plx-button plx-button-primary plx-agent-button", kind.label);
                go.dataset.action = `ai-${kind.kind}`;
                const note = el("p", "plx-ai-note");
                row.append(go, note);
                section.appendChild(row);
                go.addEventListener("click", () => launch(kind));
                rows[kind.kind] = { button: go, note };
            });
            root.appendChild(section);
        });
        const balance = el("p", "plx-agent-progress plx-ai-balance");
        balance.hidden = true;
        const more = button("plx-button plx-agent-button", "About Plexora AI");
        more.dataset.action = "ai-explain";
        more.hidden = true;
        more.addEventListener("click", () => {
            window.PlexoraPaid?.explain?.({ entitlement: "ai", label: "Plexora AI" });
        });
        root.append(balance, more);
        hide.addEventListener("click", () => closeLauncher());
        backdrop.addEventListener("click", () => closeLauncher());
        const onKey = (event) => {
            if (event.key === "Escape") closeLauncher();
        };
        if (typeof document.addEventListener === "function") document.addEventListener("keydown", onKey);
        document.body.append(backdrop, root);
        launcher = { root, backdrop, onKey, rows, groups, balance, more, answer: null, starting: false };
        syncLaunchChip();
        paintLauncher();
        const first = Object.values(rows).map((row) => row.button).find((b) => !b.disabled) || hide;
        first.focus?.();
        if (!aiAllowed() || !datasource() || !Object.keys(rows).length) return Promise.resolve(launcher);
        const mine = launcher;
        return aiFetch(`ai/v1/balance?project=${encodeURIComponent(datasource())}`)
            .then((answer) => { mine.answer = answer; })
            .catch((error) => { mine.answer = { error: error.message || String(error) }; })
            .then(() => { paintLauncher(); return mine; });
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
            els.hide.hidden = true;
            els.close.hidden = false;
            session.orb?.pause();
            restoreViewer(reason);
        },
        ai_run(session, payload) {
            session.aiRun = { run_id: payload.run_id, quote_credits: payload.quote_credits, kind: payload.kind };
            if (payload.resumed) {
                session.aiPause = null;
                showCredit(session);
            }
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
    // The header sparkle, once the page (and its licence hint) is in place.
    try {
        if (document.readyState === "loading" && typeof document.addEventListener === "function") {
            document.addEventListener("DOMContentLoaded", () => syncLaunchChip());
        } else {
            syncLaunchChip();
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
         *  collapsed, job, bulk}` -- `job` and `bulk` are QC's (the bulk
         *  pass's job id, and its latest stage while it runs). */
        current: () => (current ? { id: current.id, phase: current.phase, paused: current.paused,
                                    done: current.done, collapsed: current.collapsed,
                                    job: current.job, bulk: current.bulk } : null),
        PHASES,
        OUTCOMES,
        /** The Plexora AI launcher for the open project (resolves once its
         *  estimate has been asked for). */
        openLauncher,
        closeLauncher,
        /** The launcher on screen (for a probe): `{buttons: {gating, qc}}`
         *  as `{disabled, note, label}`, the balance line; or null. */
        launcher: () => (launcher ? {
            buttons: Object.fromEntries(Object.entries(launcher.rows).map(([k, r]) => [k, {
                disabled: r.button.disabled, note: r.note.textContent, label: r.button.textContent }])),
            balance: launcher.balance.hidden ? "" : launcher.balance.textContent,
            modalities: launcher.groups.map((g) => g.id),
            modal: launcher.root.parentNode === document.body && launcher.backdrop.parentNode === document.body,
        } : null),
        syncLaunchChip,
        /** `{typing: false}` shows every line at once (a probe reads them). */
        configure(options = {}) {
            if (options.typing !== undefined) typing = Boolean(options.typing);
        },
        //: Test seams.
        _handle: handle,
        _openSetup: openSetup,
    };
})();

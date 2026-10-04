/**
 * chatPanel.js -- talking to Plexora AI from the viewer.
 *
 * The sidebar header's AI button opens the launcher (views/agentPanel.js), and
 * its "Chat with Plexora AI" opens this NON-modal panel over the tissue. It has
 * no box of its own: a glass composer floats at the bottom centre of the
 * viewer (text and images, attached or pasted; the credit meter, Stop, Send
 * then Minimize and Close, on its one row) and the transcript
 * rises above it, fading out toward the top (chatPanel.css) so the image
 * stays visible behind it. Minimize folds all of it into a two-button pill
 * at the bottom of the viewer (Restore, Close); Restore brings back the same
 * transcript, draft and attachments, since nothing was taken down. It
 * speaks the `/ai/v1/conversations` wire (server/routes/ai_chat_routes.py):
 *
 *   - POST /conversations starts one (with the viewer tools: this tab is the
 *     viewer); a Free licence answers 403 with a `license` block, which opens
 *     the paid-feature modal (PlexoraPaid.explain) instead of the panel;
 *   - POST /<id>/messages sends the user's turn;
 *   - GET /<id>/events streams the turn's events as server-sent events, and
 *     falls back to a held poll (?after=&wait=20) when the stream errors -- a
 *     proxy that buffers it, say -- the agent bridge's idiom;
 *   - POST /<id>/control (Stop), /<id>/approve (Approve / Deny), /<id>/undo
 *     (an Undo chip on a reversible write).
 *
 * What it shows, from the events (the server's `disclosure` event is not
 * drawn: the launcher already says this is AI); the user's words with
 * thumbnails of what they
 * attached; the answer as it streams (`text_delta`, then the whole `text`);
 * one chip per tool call (tool, ok or error, "cached" when the tool-result
 * cache answered, Undo when it was a reversible write with an operation id,
 * the images a tool returned); an approval card for a call that writes a
 * source file or cannot be undone; sub-agents' starts and summaries as muted
 * lines; a pause for credit as a notice; and a running credit meter.
 *
 * A reload keeps it: the conversation id and whether the panel was open or
 * minimized are kept in this tab's sessionStorage, per project, and on load
 * the panel reopens the same conversation and replays its events from the
 * start, which redraws the transcript (the user's lines come back from their
 * `user_message` events; a decided approval from `approval_decided`). A
 * conversation the server no longer has is forgotten and the panel stays
 * shut.
 *
 * DOM built with createElement/textContent only: nothing the model says is
 * ever parsed as markup. Classic script, `window.PlexoraChatPanel`.
 */
window.PlexoraChatPanel = (function () {
    "use strict";

    const POLL_WAIT_S = 20;
    const MAX_IMAGES = 4;
    // The composer grows with what is typed, up to this, then scrolls.
    const INPUT_MAX_PX = 168;
    // Within this of the bottom, new lines keep the transcript pinned there;
    // further up, the reader scrolled back on purpose and is left alone.
    const STICK_PX = 48;

    const state = {
        root: null, log: null, dock: null, input: null, send: null, stop: null, meter: null,
        thumbs: null, restore: null, conversation: null, cursor: 0, source: null, polling: false,
        streamingNode: null, attached: [], busy: false, open: false, minimized: false, credits: 0,
        // User lines drawn on Send whose `user_message` echo has not come back yet.
        echoes: 0, approvals: {},
    };

    // -- surviving a reload ------------------------------------------------------------

    function storageKey() {
        const project = (window.flaskVariables && window.flaskVariables.datasource) || "";
        return "plexora.chat." + project;
    }

    function recall() {
        try {
            const raw = window.sessionStorage && window.sessionStorage.getItem(storageKey());
            const saved = raw ? JSON.parse(raw) : null;
            return saved && typeof saved === "object" ? saved : {};
        } catch (_) {
            return {};
        }
    }

    function remember() {
        try {
            if (!window.sessionStorage) return;
            if (!state.conversation) {
                window.sessionStorage.removeItem(storageKey());
                return;
            }
            window.sessionStorage.setItem(storageKey(), JSON.stringify({
                conversation: state.conversation, open: state.open, minimized: state.minimized,
            }));
        } catch (_) { /* private window, blocked storage: the panel just won't survive a reload */ }
    }

    function url(path) {
        return (typeof plexoraUrl === "function") ? plexoraUrl(path) : "/" + path;
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function button(label, className, onClick) {
        const node = el("button", className, label);
        node.type = "button";
        node.addEventListener("click", onClick);
        return node;
    }

    /** A composer control drawn as an icon (a CSS mask, chatPanel.css), named
     *  by its aria-label and tooltip. */
    function iconButton(label, className, onClick) {
        const node = button("", "plx-chat-icon-btn " + className, onClick);
        node.setAttribute("aria-label", label);
        node.title = label;
        node.appendChild(el("span", "plx-chat-icon"));
        return node;
    }

    async function post(path, body) {
        const response = await fetch(url(path), {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body || {}),
        });
        let payload = {};
        try { payload = await response.json(); } catch (_) { payload = {}; }
        return { status: response.status, body: payload };
    }

    function scroll(force) {
        const log = state.log;
        if (!log) return;
        const away = (log.scrollHeight || 0) - (log.scrollTop || 0) - (log.clientHeight || 0);
        if (force || away < STICK_PX) log.scrollTop = log.scrollHeight;
    }

    function line(className, text) {
        const pinned = state.log && ((state.log.scrollHeight || 0) - (state.log.scrollTop || 0)
            - (state.log.clientHeight || 0)) < STICK_PX;
        const node = el("div", "plx-chat-line " + className, text);
        state.log.appendChild(node);
        scroll(pinned);
        return node;
    }

    function grow() {
        const input = state.input;
        if (!input) return;
        input.style.height = "auto";
        input.style.height = Math.min(input.scrollHeight || 0, INPUT_MAX_PX) + "px";
        ready();
    }

    /** Send stays glass until there is something to send. */
    function ready() {
        if (!state.dock) return;
        const text = state.input && (state.input.value || "").trim();
        state.dock.classList.toggle("has-draft", Boolean(text) || state.attached.length > 0);
    }

    function credits(micro) {
        return (Number(micro || 0) / 10000).toFixed(1);
    }

    function setBusy(busy) {
        state.busy = busy;
        if (state.send) state.send.disabled = busy;
        if (state.stop) state.stop.hidden = !busy;
        if (state.root) state.root.classList.toggle("is-busy", busy);
    }

    // -- the panel --------------------------------------------------------------------

    function mount() {
        if (state.root) return;
        const host = document.getElementById("openseadragon_wrapper") || document.body;

        const root = el("section", "plx-chat-panel");
        root.hidden = true;
        root.setAttribute("aria-label", "Plexora AI");

        state.log = el("div", "plx-chat-log");
        state.log.setAttribute("role", "log");
        state.log.setAttribute("aria-live", "polite");
        root.appendChild(state.log);

        const dock = el("div", "plx-chat-dock");
        state.dock = dock;
        state.thumbs = el("div", "plx-chat-thumbs");
        dock.appendChild(state.thumbs);
        const form = el("div", "plx-chat-form");
        const picker = el("input", "plx-chat-file");
        picker.type = "file";
        picker.accept = "image/png,image/jpeg,image/webp";
        picker.multiple = true;
        picker.hidden = true;
        picker.addEventListener("change", () => attach(picker.files || []));
        form.appendChild(iconButton("Attach an image", "plx-chat-attach", () => picker.click()));
        state.input = el("textarea", "plx-chat-input");
        state.input.placeholder = "Ask Plexora AI about this image...";
        state.input.rows = 1;
        state.input.setAttribute("aria-label", "Message Plexora AI");
        state.input.addEventListener("input", grow);
        state.input.addEventListener("keydown", (event) => {
            if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                submit();
            } else if (event.key === "Escape") {
                event.preventDefault();
                toggle(false);
            }
        });
        state.input.addEventListener("paste", (event) => {
            const items = (event.clipboardData && event.clipboardData.files) || [];
            if (items.length) attach(items);
        });
        form.appendChild(state.input);
        state.meter = el("span", "plx-chat-meter", "0.0 credits");
        state.meter.title = "Plexora AI credits this conversation has used";
        form.appendChild(state.meter);
        state.stop = iconButton("Stop", "plx-chat-stop", () => control("stop"));
        state.stop.hidden = true;
        form.appendChild(state.stop);
        state.send = iconButton("Send", "plx-chat-send", () => submit());
        form.appendChild(state.send);
        const controls = el("div", "plx-chat-window");
        controls.appendChild(iconButton("Minimize Plexora AI", "plx-chat-minimize", () => minimize(true)));
        controls.appendChild(iconButton("Close Plexora AI", "plx-chat-close", () => toggle(false)));
        form.appendChild(controls);
        form.appendChild(picker);
        dock.appendChild(form);
        root.appendChild(dock);

        // Minimized: only these two, tethered to the bottom of the viewer.
        const pill = el("div", "plx-chat-pill");
        state.restore = iconButton("Restore Plexora AI", "plx-chat-restore", () => minimize(false));
        pill.appendChild(state.restore);
        pill.appendChild(iconButton("Close Plexora AI", "plx-chat-close", () => toggle(false)));
        root.appendChild(pill);
        host.appendChild(root);
        state.root = root;
    }

    function minimize(on) {
        if (!state.root) return;
        state.minimized = Boolean(on);
        state.root.classList.toggle("is-minimized", state.minimized);
        remember();
        if (state.minimized) {
            if (state.restore) state.restore.focus();
        } else {
            if (state.input) state.input.focus();
            scroll(true);
        }
    }

    async function toggle(open) {
        mount();
        state.open = open === undefined ? !state.open : Boolean(open);
        // Opened again from the launcher, it comes back full size.
        if (state.minimized) minimize(false);
        state.root.hidden = !state.open;
        if (state.open && !state.conversation) {
            const started = (await resume()) || (await start());
            if (!started) {
                state.open = false;
                state.root.hidden = true;
            }
        }
        remember();
        if (state.open && state.input) state.input.focus();
    }

    /** Pick up the conversation this tab had before a reload, if the server
     *  still has it; its events replay from the start. */
    async function resume() {
        const saved = recall().conversation;
        if (!saved) return false;
        let ok = false;
        try {
            const response = await fetch(url(`ai/v1/conversations/${encodeURIComponent(saved)}`));
            const body = await response.json();
            ok = response.ok && Boolean(body.ok);
        } catch (_) {
            ok = false;
        }
        if (!ok) return false;
        state.conversation = saved;
        state.cursor = 0;
        listen();
        return true;
    }

    async function start() {
        const title = (window.flaskVariables && window.flaskVariables.datasource) || "";
        const answer = await post("ai/v1/conversations", { title: String(title), viewer: true });
        if (answer.status === 403 && answer.body.license && window.PlexoraPaid) {
            window.PlexoraPaid.explain(answer.body.license);
            return false;
        }
        if (!answer.body.ok) {
            line("plx-chat-error", (answer.body.error && answer.body.error.message) || answer.body.error ||
                 "Plexora AI is not available.");
            return true;
        }
        state.conversation = answer.body.conversation_id;
        state.cursor = 0;
        listen();
        return true;
    }

    // -- sending ------------------------------------------------------------------------

    function attach(files) {
        for (const file of Array.from(files)) {
            if (state.attached.length >= MAX_IMAGES || !/^image\//.test(file.type || "")) continue;
            const reader = new FileReader();
            reader.onload = () => {
                state.attached.push({ data: String(reader.result), format: (file.type.split("/")[1] || "png") });
                drawThumbs();
                ready();
            };
            reader.readAsDataURL(file);
        }
    }

    function drawThumbs() {
        state.thumbs.textContent = "";
        state.attached.forEach((image, index) => {
            const img = el("img", "plx-chat-thumb");
            img.src = image.data;
            img.alt = "attached image " + (index + 1);
            img.title = "Click to remove";
            img.addEventListener("click", () => {
                state.attached.splice(index, 1);
                drawThumbs();
                ready();
            });
            state.thumbs.appendChild(img);
        });
    }

    async function submit() {
        if (state.busy || !state.conversation) return;
        const text = (state.input.value || "").trim();
        const images = state.attached.slice();
        if (!text && !images.length) return;
        state.input.value = "";
        grow();
        state.attached = [];
        drawThumbs();
        ready();
        state.echoes += 1;
        const mine = line("plx-chat-user", text);
        for (const image of images) {
            const img = el("img", "plx-chat-thumb");
            img.src = image.data;
            img.alt = "your image";
            mine.appendChild(img);
        }
        scroll(true);
        setBusy(true);
        const answer = await post(`ai/v1/conversations/${state.conversation}/messages`, { text, images });
        if (!answer.body.ok) {
            state.echoes = Math.max(0, state.echoes - 1);
            setBusy(false);
            line("plx-chat-error", (answer.body.error && answer.body.error.message) || "The message was not sent.");
        }
    }

    function control(action) {
        if (!state.conversation) return Promise.resolve();
        return post(`ai/v1/conversations/${state.conversation}/control`, { action });
    }

    // -- events -------------------------------------------------------------------------

    function listen() {
        if (typeof EventSource === "function" && !state.polling) {
            const source = new EventSource(url(`ai/v1/conversations/${state.conversation}/events?after=${state.cursor}`));
            source.onmessage = (message) => receive(message);
            source.onerror = () => {
                source.close();
                state.source = null;
                state.polling = true;
                poll();
            };
            // Named events (`event: tool_result`) do not reach onmessage.
            for (const name of KNOWN) source.addEventListener(name, receive);
            state.source = source;
            return;
        }
        state.polling = true;
        poll();
    }

    async function poll() {
        while (state.polling && state.conversation) {
            try {
                const response = await fetch(url(
                    `ai/v1/conversations/${state.conversation}/events?after=${state.cursor}&wait=${POLL_WAIT_S}`));
                const body = await response.json();
                for (const event of body.events || []) handle(event);
                if (!response.ok) await new Promise((resolve) => setTimeout(resolve, 2000));
            } catch (_) {
                await new Promise((resolve) => setTimeout(resolve, 2000));
            }
        }
    }

    function receive(message) {
        let event;
        try { event = JSON.parse(message.data); } catch (_) { return; }
        handle(event);
    }

    const KNOWN = ["disclosure", "user_message", "turn_started", "text_delta", "text", "tool_call", "tool_result",
                   "approval_requested", "approval_decided", "usage", "done", "error", "paused", "stopped",
                   "compacted", "agent_started", "agent_finished", "control", "undone", "turn_finished"];

    function handle(event) {
        if (!event || typeof event.seq !== "number" || event.seq <= state.cursor) return;
        state.cursor = event.seq;
        const sub = event.agent ? "[" + event.agent + "] " : "";
        switch (event.event) {
        case "user_message":
            // Drawn already when it was sent from this page; replayed after a
            // reload, it is drawn here (the images were not kept, only how many).
            if (state.echoes > 0) {
                state.echoes -= 1;
            } else {
                const mine = line("plx-chat-user", event.text || "");
                if (event.images) mine.appendChild(el("div", "plx-chat-user-note",
                    event.images + (event.images === 1 ? " image" : " images")));
            }
            break;
        case "approval_decided":
            decided(event);
            break;
        case "turn_started":
            setBusy(true);
            break;
        case "text_delta":
            if (event.agent) break;
            if (!state.streamingNode) state.streamingNode = line("plx-chat-assistant", "");
            state.streamingNode.textContent += event.text;
            scroll();
            break;
        case "text":
            if (event.agent) {
                line("plx-chat-sub", sub + event.text);
            } else {
                const node = state.streamingNode || line("plx-chat-assistant", "");
                node.textContent = event.text;
                state.streamingNode = null;
            }
            break;
        case "tool_result":
            toolChip(event, sub);
            break;
        case "approval_requested":
            approvalCard(event, sub);
            break;
        case "usage":
            if (typeof event.total_charged_micro === "number") {
                state.credits = event.total_charged_micro;
                state.meter.textContent = credits(state.credits) + " credits";
            }
            break;
        case "paused":
            line("plx-chat-notice", sub + "Paused: " + (event.message || event.reason) +
                 (event.resume ? " (" + event.resume + ")" : ""));
            break;
        case "error":
            line("plx-chat-error", sub + (event.message || event.code));
            break;
        case "stopped":
            line("plx-chat-notice", "Stopped.");
            break;
        case "agent_started":
            line("plx-chat-sub", sub + "started: " + (event.brief || event.role || ""));
            break;
        case "agent_finished":
            line("plx-chat-sub", sub + (event.state === "done" ? "done: " : event.state + ": ") + (event.summary || ""));
            break;
        case "undone":
            line("plx-chat-notice", "Undone: " + event.operation_id);
            break;
        case "turn_finished":
            state.streamingNode = null;
            setBusy(false);
            break;
        default:
            break;
        }
    }

    function toolChip(event, sub) {
        const chip = el("div", "plx-chat-tool" + (event.ok ? "" : " is-error"));
        chip.appendChild(el("span", "plx-chat-tool-name", sub + event.tool));
        const status = event.ok ? (event.source === "cache" ? "ok, cached" : "ok") :
            (event.source === "declined" ? "declined" : "error");
        chip.appendChild(el("span", "plx-chat-tool-status", status));
        if (event.summary && !event.ok) chip.title = event.summary;
        if (event.undo && event.operation_id) {
            const undo = button("Undo", "plx-chat-undo", async () => {
                undo.disabled = true;
                const answer = await post(`ai/v1/conversations/${state.conversation}/undo`,
                                          { operation_id: event.operation_id });
                undo.textContent = answer.body.ok ? "Undone" : "Undo failed";
            });
            undo.title = "Undo " + event.operation_id;
            chip.appendChild(undo);
        }
        for (const image of event.images || []) {
            const img = el("img", "plx-chat-thumb");
            img.src = "data:image/" + (image.format || "png") + ";base64," + image.data;
            img.alt = event.tool + " image";
            chip.appendChild(img);
        }
        state.log.appendChild(chip);
        scroll();
    }

    function approvalCard(event, sub) {
        const card = el("div", "plx-chat-approval");
        const what = event.permission === "destructive" ? "cannot be undone" : "writes into your source file";
        card.appendChild(el("div", "plx-chat-approval-title", sub + event.tool + " " + what));
        if (event.purpose) card.appendChild(el("div", "plx-chat-approval-purpose", event.purpose));
        card.appendChild(el("pre", "plx-chat-approval-args", JSON.stringify(event.arguments || {}, null, 1)));
        const row = el("div", "plx-chat-approval-actions");
        const decide = (decision) => async () => {
            approve.disabled = true;
            deny.disabled = true;
            const answer = await post(`ai/v1/conversations/${state.conversation}/approve`,
                                      { approval_id: event.approval_id, decision });
            row.textContent = answer.body.ok ? (decision === "approve" ? "Approved" : "Declined") :
                ((answer.body.error && answer.body.error.message) || "Not recorded");
        };
        const approve = button("Approve", "plx-chat-approve", decide("approve"));
        const deny = button("Deny", "plx-chat-deny", decide("deny"));
        state.approvals[event.approval_id] = { row, approve, deny };
        row.appendChild(approve);
        row.appendChild(deny);
        card.appendChild(row);
        state.log.appendChild(card);
        scroll();
    }

    /** An approval answered (here, from another page, or before a reload):
     *  its card stops offering the buttons. */
    function decided(event) {
        const card = state.approvals[event.approval_id];
        if (!card) return;
        card.approve.disabled = true;
        card.deny.disabled = true;
        const words = { approved: "Approved", denied: "Declined", stopped: "Stopped", expired: "Expired" };
        card.row.textContent = words[event.status] || String(event.status || "Decided");
    }

    function boot() {
        if (!document.getElementById("openseadragon_wrapper")) return;
        mount();
        const saved = recall();
        if (saved.conversation && saved.open) {
            toggle(true).then(() => { if (state.open && saved.minimized) minimize(true); });
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }

    return { open: () => toggle(true), close: () => toggle(false), minimize: () => minimize(true),
             restore: () => minimize(false), handle, mount, _state: state };
})();

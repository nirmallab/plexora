/**
 * feedbackDialog.js -- the form behind the navbar's feedback menu
 * (services/feedbackMenu.js) and Help > Report a Bug.
 *
 *     PlexoraFeedback.open("bug" | "feature")
 *
 * One <dialog>, "Send Feedback", with a Bug | Feature switch in its head that
 * changes the description's prompt and the button and keeps what was typed.
 * Each field is a row, its label in a left column:
 *
 *   - Reply to: the account's address when this Plexora is signed in (the
 *     platform replies there; from GET /settings/license/account, held in
 *     memory only), else an email field;
 *   - the description, 10 to 5000 characters, in a box that grows with it;
 *   - attachments: up to three files, 5 MB each, PNG, JPEG or WebP screenshots
 *     or .txt and .log files (a log gets a one-line warning: it can hold file
 *     paths and dataset names). By the bar, by pasting, or by dropping.
 *
 * The footer's "Includes diagnostics · Review" opens exactly what is sent
 * besides (services/feedbackDiagnostics.js).
 *
 * Sending: on a desktop, a local server or a data node, to this server
 * (POST /feedback/submit), which relays it with the device's certificate; when
 * the server cannot (no internet, an older BioCognia client), straight to the
 * platform from the page. On the website the page sends it itself, through
 * its licence client (services/licenseClient.js `feedback`). One send at a
 * time; a failure says why and keeps every field; a file that did not go is
 * named and can be sent again. What was typed is kept until a send
 * succeeds -- in memory only, never stored.
 */
window.PlexoraFeedback = (function () {
    "use strict";

    const PRODUCT = "plexora";
    const DEFAULT_API = "https://api.biocognia.com/v1";
    const DESCRIPTION_MIN = 10;
    const DESCRIPTION_MAX = 5000;
    const MAX_FILES = 3;
    const MAX_BYTES = 5 * 1024 * 1024;
    const IMAGE_TYPES = { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", webp: "image/webp" };
    const TEXT_TYPES = { txt: "text/plain", log: "text/plain" };
    const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    //: How long the thank-you shows before the dialog closes itself.
    const DONE_MS = 1800;

    const KINDS = {
        bug: {
            label: "What happened?",
            placeholder: "What did you do, what happened, and what did you expect instead? "
                + "The steps that led to it help most.",
            submit: "Send Report",
            done: "Thanks — your report is in",
            doneNote: "We will email you when it is fixed.",
        },
        feature: {
            label: "What would you like?",
            placeholder: "What would you like Plexora to do, and what are you trying to achieve with it?",
            submit: "Send Request",
            done: "Thanks — your request is in",
            doneNote: "We will email you when it is ready.",
        },
    };

    //: What was typed, until a send succeeds: one draft whichever kind is
    //: chosen, so switching Bug/Feature never loses it. Memory only.
    let draft = blankDraft();
    let dialog = null;
    let current = null;

    function blankDraft() {
        return { description: "", email: "", files: [] };
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function icon(name, extra) {
        const node = el("span", `fas fa-${name}${extra ? " " + extra : ""}`);
        node.setAttribute("aria-hidden", "true");
        return node;
    }

    function extensionOf(name) {
        const match = /\.([A-Za-z0-9]+)$/.exec(String(name || ""));
        return match ? match[1].toLowerCase() : "";
    }

    function sizeText(bytes) {
        if (bytes >= 1024 * 1024) return `${(bytes / 1048576).toFixed(1)} MB`;
        return `${Math.max(1, Math.round(bytes / 1024))} KB`;
    }

    /** Up to two letters for the account's address, for the reply-to line. */
    function initialsOf(email) {
        const parts = String(email || "").split("@")[0].split(/[._-]+/).filter(Boolean);
        return (parts.length > 1 ? parts[0][0] + parts[1][0] : (parts[0] || "?").slice(0, 2)).toUpperCase();
    }

    // -- where this Plexora is, and who it is signed in as ---------------------------

    function onWebsite() {
        return window.PlexoraBackend?.kind === "browser";
    }

    /** {signed_in, version, build, platform, python, api, relay, website}. Never throws. */
    async function readContext() {
        if (onWebsite()) {
            const licence = window.PlexoraLicense;
            let site = null;
            try { site = window.parent?.PlexoraSite || window.PlexoraSite || null; } catch (error) { site = null; }
            let signedIn = false;
            try { signedIn = Boolean(licence?.certificate?.()); } catch (error) { signedIn = false; }
            return { website: true, signed_in: signedIn, version: site?.version || "", build: site?.build || "",
                     api: licence?.api || DEFAULT_API, relay: false };
        }
        try {
            const response = await fetch(plexoraUrl("feedback/context"), { cache: "no-store" });
            if (response.ok) {
                const data = await response.json();
                return { ...data, api: data.api || DEFAULT_API };
            }
        } catch (error) { /* an older server, or none: the page sends it itself */ }
        return { signed_in: false, api: DEFAULT_API, relay: false };
    }

    /** The account's address, or "" -- asked each time, kept in this dialog only. */
    async function readAccountEmail() {
        try {
            const response = await fetch(plexoraUrl("settings/license/account"), { cache: "no-store" });
            if (!response.ok) return "";
            const data = await response.json();
            return String(data?.account?.email || "");
        } catch (error) {
            return "";
        }
    }

    // -- the dialog -----------------------------------------------------------------

    /** A labelled row: the label in the left column, the field beside it. */
    function row(label, cell, { top = false } = {}) {
        const node = el("div", `plx-feedback-row${top ? " is-top" : ""}`);
        node.append(label, cell);
        return node;
    }

    function build() {
        const node = el("dialog", "plx-dialog plx-feedback");
        node.setAttribute("aria-labelledby", "plx_feedback_title");

        // The head: the title, Bug | Feature, and the close button.
        const head = el("div", "plx-feedback-head");
        const title = el("h2", "plx-feedback-title", "Send Feedback");
        title.id = "plx_feedback_title";
        const kinds = el("div", "plx-feedback-kinds");
        kinds.setAttribute("role", "radiogroup");
        kinds.setAttribute("aria-label", "Kind of feedback");
        const kindButtons = {};
        for (const [kind, label, glyph] of [["bug", "Bug", "bug"], ["feature", "Feature", "lightbulb"]]) {
            const button = el("button", `plx-feedback-kind is-${kind}`);
            button.type = "button";
            button.setAttribute("role", "radio");
            button.dataset.kind = kind;
            button.append(icon(glyph), el("span", null, label));
            kinds.appendChild(button);
            kindButtons[kind] = button;
        }
        const closer = el("button", "plx-feedback-close");
        closer.type = "button";
        closer.title = "Close";
        closer.setAttribute("aria-label", "Close");
        closer.appendChild(icon("xmark"));
        closer.addEventListener("click", () => close());
        head.append(title, kinds, el("span", "plx-feedback-spacer"), closer);
        node.appendChild(head);

        const form = el("form", "plx-feedback-form");
        form.noValidate = true;
        const body = el("div", "plx-feedback-body");

        // Reply to: the account's address when signed in, else a field.
        const replyLabel = el("label", "plx-feedback-label", "Reply to");
        replyLabel.htmlFor = "plx_feedback_email";
        const replyCell = el("div", "plx-feedback-cell");
        const whoLine = el("div", "plx-feedback-who");
        whoLine.hidden = true;
        const email = el("input", "plx-feedback-input");
        email.type = "email";
        email.id = "plx_feedback_email";
        email.name = "email";
        email.autocomplete = "email";
        email.required = true;
        email.maxLength = 254;
        email.placeholder = "you@institution.edu";
        email.value = draft.email;
        email.hidden = true;
        email.setAttribute("aria-describedby", "plx_feedback_email_error");
        const emailError = el("div", "plx-feedback-error");
        emailError.id = "plx_feedback_email_error";
        replyCell.append(whoLine, email, emailError);
        body.appendChild(row(replyLabel, replyCell));

        // The description.
        const descLabel = el("label", "plx-feedback-label");
        descLabel.htmlFor = "plx_feedback_description";
        const descCell = el("div", "plx-feedback-cell");
        const text = el("textarea", "plx-textarea");
        text.id = "plx_feedback_description";
        text.name = "description";
        text.rows = 5;
        text.required = true;
        text.maxLength = DESCRIPTION_MAX;
        text.value = draft.description;
        text.setAttribute("aria-describedby", "plx_feedback_description_error plx_feedback_count");
        const meta = el("div", "plx-feedback-meta");
        const textError = el("div", "plx-feedback-error");
        textError.id = "plx_feedback_description_error";
        const count = el("div", "plx-feedback-count");
        count.id = "plx_feedback_count";
        meta.append(textError, count);
        descCell.append(text, meta);
        body.appendChild(row(descLabel, descCell, { top: true }));

        // Attachments: one dashed bar while there are none, then chips and Add.
        const filesLabel = el("span", "plx-feedback-label", "Attachments");
        filesLabel.id = "plx_feedback_files_label";
        const files = el("div", "plx-feedback-cell plx-feedback-files-field");
        const attach = el("button", "plx-feedback-dropbar");
        attach.type = "button";
        const strong = el("span", "plx-feedback-dropbar-lead", "Add files");
        const rest = el("span", null, ", paste or drop · screenshots or .txt / .log · up to 3, 5 MB each");
        const words = el("span");
        words.append(strong, rest);
        attach.append(icon("paperclip"), words);
        const chips = el("div", "plx-feedback-chips");
        const list = el("ul", "plx-feedback-files");
        list.setAttribute("aria-labelledby", "plx_feedback_files_label");
        const addMore = el("button", "plx-feedback-add");
        addMore.type = "button";
        addMore.append(icon("plus"), el("span", null, "Add"));
        chips.append(list, addMore);
        const picker = el("input");
        picker.type = "file";
        picker.multiple = true;
        picker.hidden = true;
        picker.accept = ".png,.jpg,.jpeg,.webp,.txt,.log,image/png,image/jpeg,image/webp,text/plain";
        const filesError = el("div", "plx-feedback-error");
        filesError.setAttribute("role", "status");
        const logWarning = el("p", "plx-feedback-warning");
        logWarning.append(icon("triangle-exclamation"),
                          el("span", null, "Logs can contain file paths and dataset names. Check them before you send."));
        logWarning.hidden = true;
        files.append(attach, chips, picker, filesError, logWarning);
        body.appendChild(row(filesLabel, files, { top: true }));

        // What goes with it, opened from the footer's link.
        const diag = el("div", "plx-feedback-diag");
        diag.id = "plx_feedback_diag";
        diag.hidden = true;
        body.appendChild(diag);

        const alert = el("div", "plx-feedback-alert");
        alert.setAttribute("role", "alert");
        alert.hidden = true;
        body.appendChild(alert);
        form.appendChild(body);

        const foot = el("div", "plx-feedback-foot");
        const review = el("button", "plx-feedback-review");
        review.type = "button";
        review.setAttribute("aria-expanded", "false");
        review.setAttribute("aria-controls", diag.id);
        review.append(icon("circle-info"), el("span", null, "Includes diagnostics · "),
                      el("span", "plx-feedback-review-link", "Review"));
        const cancel = el("button", "plx-button", "Cancel");
        cancel.type = "button";
        cancel.addEventListener("click", () => close());
        const submit = el("button", "plx-button plx-button-primary plx-feedback-submit");
        submit.type = "submit";
        const submitLabel = el("span", "plx-feedback-submit-label");
        const spinner = el("span", "plx-feedback-spinner");
        spinner.setAttribute("aria-hidden", "true");
        submit.append(spinner, submitLabel);
        foot.append(review, el("span", "plx-feedback-spacer"), cancel, submit);
        form.appendChild(foot);
        node.appendChild(form);

        // The thank-you.
        const done = el("div", "plx-feedback-done");
        done.setAttribute("role", "status");
        done.hidden = true;
        const mark = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        mark.setAttribute("viewBox", "0 0 52 52");
        mark.setAttribute("class", "plx-feedback-check");
        mark.setAttribute("aria-hidden", "true");
        mark.innerHTML = '<circle class="plx-feedback-check-ring" cx="26" cy="26" r="24" fill="none"/>'
            + '<path class="plx-feedback-check-tick" fill="none" d="M15 27 l7 7 l15 -16"/>';
        const doneTitle = el("p", "plx-feedback-done-title");
        const doneText = el("p", "plx-feedback-done-text");
        const doneNote = el("p", "plx-feedback-done-note");
        done.append(mark, doneTitle, doneText, doneNote);
        node.appendChild(done);

        return { node, form, kindButtons, whoLine, email, emailError, descLabel, text, textError, count, attach,
                 addMore, chips, picker, list, filesError, logWarning, diag, review, alert, cancel, submit,
                 submitLabel, done, doneTitle, doneText, doneNote, files };
    }

    /** Bug or Feature: the description's label and prompt, the button, the switch. What was typed stays. */
    function setKind(state, kind) {
        const { parts } = state;
        state.kind = kind;
        parts.node.dataset.kind = kind;
        for (const [name, button] of Object.entries(parts.kindButtons)) {
            const on = name === kind;
            button.setAttribute("aria-checked", on ? "true" : "false");
            button.tabIndex = on ? 0 : -1;
        }
        parts.descLabel.textContent = KINDS[kind].label;
        parts.text.placeholder = KINDS[kind].placeholder;
        if (!state.sending && !state.sent) parts.submitLabel.textContent = submitText(state);
        if (parts.text.getAttribute("aria-invalid")) validateDescription(state);
    }

    // -- one open dialog ------------------------------------------------------------

    function open(kind = "bug") {
        if (!KINDS[kind]) kind = "bug";
        if (dialog && current) {
            if (!current.sending && !current.sent) setKind(current, kind);
            if (!dialog.open) dialog.showModal();
            return;
        }
        const parts = build();
        const state = {
            kind, parts, draft, sending: false, context: null, accountEmail: "",
            signedIn: false, sent: null, failedFiles: [], previews: new Map(),
        };
        current = state;
        dialog = parts.node;
        setKind(state, kind);
        wire(state);
        document.body.appendChild(dialog);
        if (typeof dialog.showModal === "function") dialog.showModal();
        else dialog.setAttribute("open", "");
        drawFiles(state);
        grow(parts.text);
        drawCount(state);
        parts.text.focus();
        state.ready = prepare(state).catch(() => drawWho(state));
    }

    /** Who is asking: read once per opening, behind the form. */
    async function prepare(state) {
        state.context = await readContext();
        if (state !== current) return;
        state.signedIn = Boolean(state.context.signed_in);
        if (state.signedIn) state.accountEmail = await readAccountEmail();
        if (state !== current) return;
        drawWho(state);
    }

    function drawWho(state, { needEmail = false } = {}) {
        const { parts } = state;
        const named = state.signedIn && state.accountEmail;
        if (state.signedIn && !needEmail) {
            const badge = el("span", "plx-feedback-avatar", named ? initialsOf(state.accountEmail) : "");
            badge.setAttribute("aria-hidden", "true");
            if (!named) badge.appendChild(icon("user"));
            const words = el("span", "plx-feedback-who-email", named ? state.accountEmail : "The email on your account");
            parts.whoLine.replaceChildren(badge, words, el("span", "plx-feedback-who-note", "· your BioCognia account"));
            parts.whoLine.hidden = false;
            parts.email.hidden = true;
        } else {
            parts.whoLine.hidden = true;
            parts.email.hidden = false;
            if (named && !parts.email.value) parts.email.value = state.accountEmail;
        }
    }

    /** The diagnostics panel: exactly what is sent besides what was typed. */
    async function toggleDiagnostics(state) {
        const { diag, review } = state.parts;
        if (!diag.hidden) {
            diag.hidden = true;
            review.setAttribute("aria-expanded", "false");
            return;
        }
        if (state.ready) await state.ready;
        if (state !== current) return;
        const diagnostics = await window.PlexoraFeedbackDiagnostics.collect(state.context || {});
        const table = el("table", "plx-feedback-diag-table");
        for (const [label, value] of window.PlexoraFeedbackDiagnostics.rows(diagnostics)) {
            const tr = el("tr");
            tr.append(el("th", null, label), el("td", null, value));
            table.appendChild(tr);
        }
        const nodes = [el("p", "plx-feedback-diag-head", "Sent with this, to help us reproduce it. "
            + "Never your files, data or project names."), table];
        const errors = diagnostics.errors || [];
        if (errors.length) {
            nodes.push(el("p", "plx-feedback-diag-head", `Recent errors (${errors.length})`));
            const ol = el("ol", "plx-feedback-diag-errors");
            for (const error of errors.slice().reverse()) {
                const li = el("li", null, `${error.kind || "Error"}: ${error.message || ""}`);
                if (error.source) li.appendChild(el("span", "plx-feedback-diag-source", ` · ${error.source}`));
                ol.appendChild(li);
            }
            nodes.push(ol);
        } else {
            nodes.push(el("p", "plx-feedback-diag-head", "No recent errors."));
        }
        diag.replaceChildren(...nodes);
        diag.hidden = false;
        review.setAttribute("aria-expanded", "true");
    }

    function wire(state) {
        const { parts } = state;
        parts.text.addEventListener("input", () => {
            state.draft.description = parts.text.value;
            grow(parts.text);
            drawCount(state);
            if (parts.text.getAttribute("aria-invalid")) validateDescription(state, { quiet: true });
        });
        parts.email.addEventListener("input", () => {
            state.draft.email = parts.email.value;
            if (parts.email.getAttribute("aria-invalid")) validateEmail(state, { quiet: true });
        });
        parts.email.addEventListener("blur", () => {
            if (parts.email.value.trim()) validateEmail(state);
        });
        parts.attach.addEventListener("click", () => parts.picker.click());
        parts.addMore.addEventListener("click", () => parts.picker.click());
        parts.review.addEventListener("click", () => { toggleDiagnostics(state); });
        for (const button of Object.values(parts.kindButtons)) {
            button.addEventListener("click", () => {
                if (!state.sending && !state.sent) setKind(state, button.dataset.kind);
            });
            button.addEventListener("keydown", (event) => {
                if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
                event.preventDefault();
                if (state.sending || state.sent) return;
                const next = state.kind === "bug" ? "feature" : "bug";
                setKind(state, next);
                parts.kindButtons[next].focus();
            });
        }
        parts.picker.addEventListener("change", () => {
            addFiles(state, Array.from(parts.picker.files || []));
            parts.picker.value = "";
        });
        parts.node.addEventListener("paste", (event) => {
            const pasted = Array.from(event.clipboardData?.files || []);
            if (!pasted.length) return;
            event.preventDefault();
            addFiles(state, pasted.map((file, i) => (file.name && file.name !== "image.png" ? file
                : new File([file], `pasted-${stamp()}${i ? "-" + (i + 1) : ""}.${extensionOf(file.name) || "png"}`,
                           { type: file.type }))));
        });
        parts.form.addEventListener("dragover", (event) => {
            if (!Array.from(event.dataTransfer?.types || []).includes("Files")) return;
            event.preventDefault();
            parts.files.classList.add("is-drop");
        });
        parts.form.addEventListener("dragleave", (event) => {
            if (!parts.form.contains(event.relatedTarget)) parts.files.classList.remove("is-drop");
        });
        parts.form.addEventListener("drop", (event) => {
            const dropped = Array.from(event.dataTransfer?.files || []);
            parts.files.classList.remove("is-drop");
            if (!dropped.length) return;
            event.preventDefault();
            addFiles(state, dropped);
        });
        parts.form.addEventListener("submit", (event) => {
            event.preventDefault();
            submit(state);
        });
        parts.node.addEventListener("cancel", (event) => {
            // Escape while sending would hide a send that is still going.
            if (state.sending) event.preventDefault();
        });
        parts.node.addEventListener("close", () => {
            for (const url of state.previews.values()) URL.revokeObjectURL(url);
            parts.node.remove();
            if (dialog === parts.node) {
                dialog = null;
                current = null;
            }
        });
    }

    function stamp() {
        const now = new Date();
        const pad = (n) => String(n).padStart(2, "0");
        return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
    }

    /** The textarea as tall as what is in it, up to its CSS max-height. */
    function grow(text) {
        text.style.height = "auto";
        const max = parseFloat(getComputedStyle(text).maxHeight) || 320;
        text.style.height = `${Math.min(text.scrollHeight + 2, max)}px`;
        text.style.overflowY = text.scrollHeight + 2 > max ? "auto" : "hidden";
    }

    function drawCount(state) {
        const length = state.parts.text.value.trim().length;
        state.parts.count.textContent = `${length.toLocaleString()} / ${DESCRIPTION_MAX.toLocaleString()}`;
        state.parts.count.classList.toggle("is-near", length > DESCRIPTION_MAX * 0.9);
    }

    // -- files ----------------------------------------------------------------------

    /**
     * Take files, checked, each copied into memory as it is added: a File on
     * disk can no longer be read once the disk copy changes (Chrome refuses
     * it), and a log still being written changes all the time. At most 3 x 5 MB.
     */
    async function addFiles(state, incoming) {
        const copies = [];
        const unreadable = [];
        for (const file of incoming) {
            const ext = extensionOf(file.name);
            // What will be refused anyway is passed on unread (takeFiles says why).
            if ((!IMAGE_TYPES[ext] && !TEXT_TYPES[ext]) || !file.size || file.size > MAX_BYTES) {
                copies.push(file);
                continue;
            }
            try {
                copies.push(new File([await file.arrayBuffer()], file.name,
                                     { type: file.type, lastModified: file.lastModified }));
            } catch (error) {
                unreadable.push(`${file.name} could not be read.`);
            }
        }
        if (state !== current) return;
        takeFiles(state, copies, unreadable);
    }

    function takeFiles(state, incoming, refused) {
        const { draft, parts } = state;
        for (const file of incoming) {
            const ext = extensionOf(file.name);
            if (!IMAGE_TYPES[ext] && !TEXT_TYPES[ext]) {
                refused.push(`${file.name}: only PNG, JPEG and WebP images and .txt or .log files can be attached.`);
            } else if (file.size > MAX_BYTES) {
                refused.push(`${file.name} is ${sizeText(file.size)}; each file must be 5 MB or smaller.`);
            } else if (!file.size) {
                refused.push(`${file.name} is empty.`);
            } else if (draft.files.length >= MAX_FILES) {
                refused.push(`At most ${MAX_FILES} files: ${file.name} was not added.`);
            } else if (draft.files.some((f) => f.name === file.name && f.size === file.size)) {
                continue;
            } else {
                draft.files.push(file);
            }
        }
        parts.filesError.textContent = refused.join(" ");
        drawFiles(state);
    }

    function removeFile(state, file) {
        const { draft } = state;
        draft.files = draft.files.filter((f) => f !== file);
        const url = state.previews.get(file);
        if (url) URL.revokeObjectURL(url);
        state.previews.delete(file);
        state.parts.filesError.textContent = "";
        drawFiles(state);
        (state.parts.attach.hidden ? state.parts.addMore : state.parts.attach).focus();
    }

    function drawFiles(state) {
        const { draft, parts } = state;
        const locked = state.sending || Boolean(state.sent);
        parts.list.replaceChildren(...draft.files.map((file) => {
            const ext = extensionOf(file.name);
            const li = el("li", `plx-feedback-file${TEXT_TYPES[ext] ? " is-text" : ""}`);
            if (IMAGE_TYPES[ext]) {
                let url = state.previews.get(file);
                if (!url) {
                    url = URL.createObjectURL(file);
                    state.previews.set(file, url);
                }
                const thumb = el("img", "plx-feedback-thumb");
                thumb.src = url;
                thumb.alt = "";
                li.appendChild(thumb);
            } else {
                const glyph = el("span", "plx-feedback-file-icon");
                glyph.appendChild(icon("file-lines"));
                li.appendChild(glyph);
            }
            const words = el("span", "plx-feedback-file-words");
            words.append(el("span", "plx-feedback-file-name", file.name),
                         el("span", "plx-feedback-file-size", sizeText(file.size)));
            li.appendChild(words);
            if (state.failedFiles.includes(file)) li.classList.add("is-failed");
            const remove = el("button", "plx-feedback-file-remove");
            remove.type = "button";
            remove.setAttribute("aria-label", `Remove ${file.name}`);
            remove.title = "Remove";
            remove.appendChild(icon("xmark"));
            remove.addEventListener("click", () => removeFile(state, file));
            remove.disabled = locked;
            li.appendChild(remove);
            return li;
        }));
        const any = draft.files.length > 0;
        parts.attach.hidden = any;
        parts.chips.hidden = !any;
        parts.addMore.hidden = draft.files.length >= MAX_FILES;
        parts.attach.disabled = locked;
        parts.addMore.disabled = locked;
        parts.logWarning.hidden = !draft.files.some((file) => TEXT_TYPES[extensionOf(file.name)]);
    }

    // -- checking ---------------------------------------------------------------------

    function setError(input, holder, message) {
        holder.textContent = message || "";
        if (message) input.setAttribute("aria-invalid", "true");
        else input.removeAttribute("aria-invalid");
    }

    function emailNeeded(state) {
        return !state.parts.email.hidden;
    }

    function validateEmail(state, { quiet = false } = {}) {
        const { email, emailError } = state.parts;
        if (!emailNeeded(state)) return true;
        const value = email.value.trim();
        const message = !value ? "Enter your email address so we can reply."
            : value.length > 254 || !EMAIL.test(value) ? "That does not look like an email address." : "";
        if (!quiet || !message) setError(email, emailError, message);
        return !message;
    }

    function validateDescription(state, { quiet = false } = {}) {
        const { text, textError } = state.parts;
        const length = text.value.trim().length;
        const message = !length ? (state.kind === "bug" ? "Describe what happened." : "Describe what you would like.")
            : length < DESCRIPTION_MIN ? `A little more, please: at least ${DESCRIPTION_MIN} characters.`
            : length > DESCRIPTION_MAX ? `Keep it under ${DESCRIPTION_MAX.toLocaleString()} characters.` : "";
        if (!quiet || !message) setError(text, textError, message);
        return !message;
    }

    // -- sending ----------------------------------------------------------------------

    /** The send button's words: the kind's, Send Again after a failure, Send Files Again for files only. */
    function submitText(state) {
        if (state.sent) return "Send Files Again";
        return state.failed ? "Send Again" : KINDS[state.kind].submit;
    }

    function busy(state, on) {
        const { parts } = state;
        state.sending = on;
        parts.submit.disabled = on;
        parts.submit.classList.toggle("is-busy", on);
        parts.submit.setAttribute("aria-busy", on ? "true" : "false");
        parts.submitLabel.textContent = on ? "Sending…" : submitText(state);
        parts.cancel.disabled = on;
        for (const button of Object.values(parts.kindButtons)) button.disabled = on || Boolean(state.sent);
        parts.text.readOnly = on;
        parts.email.readOnly = on;
        drawFiles(state);
    }

    function showAlert(state, message, extra) {
        const { alert } = state.parts;
        alert.replaceChildren(icon("circle-exclamation"), el("span", null, message));
        if (extra) alert.appendChild(extra);
        alert.hidden = false;
    }

    async function submit(state) {
        // One send at a time: a second click, Enter held down, or a click while
        // the context is still being read all land here and go no further.
        if (state.sending) return;
        const { parts } = state;
        parts.alert.hidden = true;
        // A report already sent whose files did not all go: only those are sent again.
        if (state.sent) return retryFiles(state);
        // Whether an address is needed is known once the context is read.
        if (state.checking) return;
        state.checking = true;
        try {
            if (state.ready) await state.ready;
        } finally {
            state.checking = false;
        }
        if (state !== current || state.sending) return;
        const okEmail = validateEmail(state);
        const okText = validateDescription(state);
        if (!okText) { parts.text.focus(); return; }
        if (!okEmail) { parts.email.focus(); return; }
        busy(state, true);
        try {
            if (!state.context) state.context = await readContext();
            const diagnostics = await window.PlexoraFeedbackDiagnostics.collect(state.context);
            const report = { kind: state.kind, description: parts.text.value.trim(), diagnostics };
            if (emailNeeded(state)) report.email = parts.email.value.trim();
            const result = await send(state, report, state.draft.files.slice());
            finish(state, result);
        } catch (error) {
            state.failed = true;
            busy(state, false);
            if (error && error.needEmail) {
                drawWho(state, { needEmail: true });
                validateEmail(state);
                parts.email.focus();
            }
            showAlert(state, (error && error.message) || "The report was not sent. Try again.");
        }
    }

    /** {id, files: [{name, ok, error}], upload, api}: through this server, or straight to the platform. */
    async function send(state, report, files) {
        const context = state.context || {};
        if (context.website) {
            const licence = window.PlexoraLicense;
            if (!licence?.feedback) throw new Error("The website's licence client is not running. Reload the page and try again.");
            if (!state.signedIn && !report.email) throw Object.assign(new Error("Enter your email address so we can reply."), { needEmail: true });
            let created;
            try {
                created = await licence.feedback(report);
            } catch (error) {
                throw new Error(friendly(error));
            }
            return { ...created, api: licence.api || context.api, files: await uploadAll(licence.api || context.api, created, files) };
        }
        if (context.relay) {
            const relayed = await viaServer(report, files);
            if (relayed.ok) return { ...relayed.body, api: context.api };
            if (!relayed.fallback) throw new Error(relayed.message);
        }
        // Straight to the platform, which needs an address to reply to.
        const email = report.email || state.accountEmail;
        if (!email) {
            throw Object.assign(new Error("Plexora could not send this through its server. Enter your email address "
                + "and send it again."), { needEmail: true });
        }
        return direct(context.api || DEFAULT_API, { ...report, email }, files, context.version);
    }

    /** POST /feedback/submit. `fallback` when the page should send it itself. */
    async function viaServer(report, files) {
        const body = new FormData();
        body.append("report", JSON.stringify(report));
        // Each file is the in-memory copy taken when it was attached (addFiles).
        for (const file of files) body.append("files", file, file.name);
        let response;
        try {
            response = await fetch(plexoraUrl("feedback/submit"), { method: "POST", body, cache: "no-store" });
        } catch (error) {
            return { ok: false, fallback: true };
        }
        let data = null;
        try { data = await response.json(); } catch (error) { data = null; }
        if (response.ok && data?.id) return { ok: true, body: data };
        const code = data?.code || "";
        if (["invalid_request", "rate_limited", "too_large", "forged_certificate"].includes(code)) {
            return { ok: false, fallback: false, message: code === "rate_limited"
                ? "Too many reports from here today. Try again tomorrow, or write to support@biocognia.com."
                : String(data?.error || "The report was refused.") };
        }
        return { ok: false, fallback: true };
    }

    /** To the platform from this page: the report, then each file with its upload token. */
    async function direct(api, report, files, version) {
        const headers = { "Content-Type": "application/json", "BioCognia-Client": `${PRODUCT}/${version || "0"}` };
        let response;
        try {
            response = await fetch(`${api}/feedback`, { method: "POST", mode: "cors", credentials: "omit", cache: "no-store",
                                                     headers, body: JSON.stringify({ product: PRODUCT, ...report }) });
        } catch (error) {
            throw new Error("Could not reach the BioCognia platform. Check your internet connection and try again; "
                + "what you wrote is kept.");
        }
        let data = null;
        try { data = await response.json(); } catch (error) { data = null; }
        if (!response.ok || !data?.id) {
            const code = data?.error?.code;
            throw new Error(code === "rate_limited"
                ? "Too many reports from here today. Try again tomorrow, or write to support@biocognia.com."
                : data?.error?.message || `The platform refused the report (HTTP ${response.status}).`);
        }
        return { ...data, api, files: await uploadAll(api, data, files) };
    }

    async function uploadAll(api, created, files) {
        const results = [];
        for (const file of files) results.push(await uploadOne(api, created, file));
        return results;
    }

    async function uploadOne(api, created, file) {
        const token = created?.upload?.token;
        if (!token) return { name: file.name, ok: false, error: "No upload link came back." };
        let bytes;
        try {
            bytes = await file.arrayBuffer();
        } catch (error) {
            return { name: file.name, ok: false, error: "the file could not be read; attach it again" };
        }
        try {
            const response = await fetch(`${api}/feedback/${encodeURIComponent(created.id)}/attachments?name=${encodeURIComponent(file.name)}`, {
                method: "PUT", mode: "cors", credentials: "omit", cache: "no-store",
                headers: { "Content-Type": "application/octet-stream", Authorization: `Bearer ${token}` },
                body: bytes });
            if (response.ok) return { name: file.name, ok: true };
            let data = null;
            try { data = await response.json(); } catch (error) { data = null; }
            return { name: file.name, ok: false, error: data?.error?.message || `HTTP ${response.status}` };
        } catch (error) {
            return { name: file.name, ok: false, error: "the platform could not be reached" };
        }
    }

    function friendly(error) {
        const message = String(error?.message || error || "");
        if (error?.code === "unreachable") {
            return "Could not reach the BioCognia platform. Check your internet connection and try again; what you wrote is kept.";
        }
        if (error?.code === "rate_limited") {
            return "Too many reports from here today. Try again tomorrow, or write to support@biocognia.com.";
        }
        return message.replace(/^Sending feedback was refused: /, "") || "The report was not sent. Try again.";
    }

    function finish(state, result) {
        state.sent = { id: result.id, upload: result.upload || {}, api: result.api };
        const failed = (result.files || []).filter((f) => !f.ok);
        state.failedFiles = state.draft.files.filter((file) => failed.some((f) => f.name === file.name));
        busy(state, false);
        if (!failed.length) return celebrate(state);
        const list = el("ul", "plx-feedback-alert-list");
        for (const f of failed) list.appendChild(el("li", null, `${f.name}: ${f.error || "not sent"}`));
        const skip = el("button", "plx-button plx-feedback-skip", "Close Without Them");
        skip.type = "button";
        skip.addEventListener("click", () => celebrate(state));
        list.appendChild(skip);
        showAlert(state, `Your ${state.kind === "bug" ? "report" : "request"} was sent (${result.id}), `
            + "but these files were not:", list);
        drawFiles(state);
    }

    async function retryFiles(state) {
        busy(state, true);
        const results = [];
        for (const file of state.failedFiles) results.push(await uploadOne(state.sent.api, state.sent, file));
        const failed = results.filter((r) => !r.ok);
        state.failedFiles = state.failedFiles.filter((file) => failed.some((f) => f.name === file.name));
        busy(state, false);
        if (!failed.length) return celebrate(state);
        finish(state, { id: state.sent.id, upload: state.sent.upload, api: state.sent.api, files: results });
    }

    function celebrate(state) {
        const { parts } = state;
        parts.doneTitle.textContent = KINDS[state.kind].done;
        parts.doneText.textContent = KINDS[state.kind].doneNote;
        parts.doneNote.textContent = state.sent?.id ? `Reference ${state.sent.id}` : "";
        parts.node.classList.add("is-done");
        parts.form.hidden = true;
        parts.done.hidden = false;
        // Starts on the next frame, so the ring draws from empty.
        requestAnimationFrame(() => parts.done.classList.add("is-shown"));
        draft = blankDraft();
        state.draft = draft;
        setTimeout(() => {
            if (current === state) close();
        }, DONE_MS);
    }

    function close(immediate = false) {
        if (!dialog) return;
        if (current?.sending && !immediate) return;
        if (dialog.open) dialog.close();
        else dialog.dispatchEvent(new Event("close"));
    }

    return { open, close, isOpen: () => Boolean(dialog && dialog.open) };
})();

/**
 * connectionModal.js -- one place where connecting to another machine happens.
 *
 * Four entry points lead here and they used to lead four different places: the
 * Settings page's Connect button, the machine picker a data field opens, the
 * globe in the navbar, and a field flipped to Remote with nothing saved yet.
 * Each had its own idea of what to show while an ssh was starting, and two of
 * them showed nothing at all beyond a state word.
 *
 * What this shows, in one dialog:
 *
 * **Steps, from the server's own states.** Nothing is invented here --
 * `connecting`, `authenticating`, `waiting_for_job`, `tunneling`,
 * `waiting_for_app` are the five things a connection actually does, and the
 * scheduler step is drawn only for a profile that runs inside a job. A
 * spinner would say the same thing about a two-second login and a
 * quarter-hour queue, and it was the queue that made the old flow feel broken
 * when it was merely slow.
 *
 * **The log, as a terminal.** Whatever ssh said, monospace, pinned to the
 * bottom -- and it stays on screen when the connection fails, because the
 * actionable line is almost always in it. Already redacted server-side; this
 * adds no second redactor and needs none.
 *
 * **The question, verbatim.** A password, a Duo push, or the host-key
 * paragraph that wants "yes". Only the user can tell which, and rewriting it
 * into something friendlier would paraphrase the one thing they must read
 * exactly. It is masked only when it is a secret -- `PlexoraRemotes.isSecret`
 * decides, once, for every surface.
 *
 * **Closing is not cancelling.** A queued job is a real fifteen minutes and
 * the ssh belongs to the server, not to this dialog. "Continue in background"
 * leaves it running and it goes on showing up in Settings and in the globe;
 * "Stop connecting" is the button that actually ends it, and says so.
 *
 * No secret is stored. What the user types goes straight to
 * `PlexoraRemotes.answer` and the box is cleared in the same breath.
 */
window.PlexoraConnectionModal = (function () {
    "use strict";

    const Remotes = () => window.PlexoraRemotes;

    //: The things a connection does, in order, with what each is called while
    //: it is happening. Four of them are conditional, and each is drawn only
    //: for a profile that actually does it: `queued` for one whose Plexora
    //: runs inside a scheduler job, `install` for one with "install or update
    //: Plexora" switched on, and `gcloud` for one whose machine does not exist
    //: until Plexora asks for it. Drawing any of them unconditionally would
    //: promise a wait that is never coming.
    //:
    //: Installing sits where it does because that is where it runs -- after
    //: the login and before anything is launched, so that what starts
    //: afterwards is the version it just installed. The two Google Cloud steps
    //: sit where they do for the same reason: a VM is created BEFORE there is
    //: anything to ssh to, and the bucket is mounted after signing in and
    //: before the environment on it is touched.
    const STEPS = [
        { state: "preparing_compute", label: "Starting the Compute Engine VM",
          gcloud: true },
        { state: "connecting", label: "Reaching the machine" },
        { state: "authenticating", label: "Signing in" },
        { state: "mounting_data", label: "Mounting your Cloud Storage bucket",
          gcloud: true },
        { state: "installing", label: "Installing Plexora", install: true },
        { state: "waiting_for_job", label: "Waiting for the scheduler",
          queued: true },
        { state: "tunneling", label: "Opening the tunnel" },
        { state: "waiting_for_app", label: "Starting Plexora" },
    ];

    //: What the install step is called when the launch command names an
    //: environment. The name comes from the server -- one reading of one field
    //: (`connect.environment_label`), the same one `pip` will be run through
    //: -- so a step cannot promise an environment the install does not write
    //: to. Without a name it stays the plain label: "in undefined" would be
    //: worse than saying nothing, and a `module load` line genuinely has no
    //: name to give.
    function installLabel(environment) {
        return environment ? "Installing Plexora in " + environment
                           : "Installing Plexora";
    }

    //: What the last step is called for a data node, which does not start a
    //: viewer at all. The step list is otherwise identical because the login
    //: is identical -- same profile, same ssh, same password.
    const NODE_LAST_STEP = "Starting the data node";

    let openDialog = null;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function button(className, text, onClick) {
        const node = el("button", className, text);
        node.type = "button";
        if (onClick) node.addEventListener("click", onClick);
        return node;
    }

    // -- the catalogue of presets ------------------------------------------
    //
    // What a preset answers is a property of somebody else's cluster --
    // partition, walltime, whether ssh into a compute node works -- and those
    // answers are the same for everybody who works there. So they are answered
    // in advance, only what genuinely differs is asked, and the server
    // composes and saves: there is one implementation of what a preset means.
    //
    // At module scope rather than inside the dialog because Settings draws the
    // same cards on the page itself. Two grids of one catalogue would be two
    // requests for a static list and two card layouts to keep in step, and the
    // second one would drift.

    //: The catalogue, fetched once per page. Not shipped in the markup: this
    //: file is loaded on every page including the viewer, and the list is used
    //: on one page in a hundred.
    let recipes = null;
    //: What the walltime, cores and memory boxes start out saying. Comes down
    //: with the catalogue rather than being written here, so that the numbers
    //: on screen are the same constants the server splices into the srun line.
    //: The literals below are a fallback for an old server, not a second
    //: opinion.
    let recipeDefaults = { walltime: "4:00:00", cores: "16",
                           memory: "128G" };

    async function loadRecipes() {
        if (recipes) return recipes;
        try {
            const answer = await fetch(plexoraUrl("settings/recipes"));
            const payload = await answer.json();
            recipes = payload.recipes || [];
            if (payload.defaults) recipeDefaults = payload.defaults;
        } catch (e) {
            recipes = [];
        }
        return recipes;
    }

    function recipeCard(recipe, onPick) {
        const card = button("connect-recipe", null, () => onPick(recipe));
        const head = el("div", "connect-recipe-head");
        head.append(el("span", "connect-recipe-label", recipe.label));
        if (recipe.unverified) {
            // Said plainly, on the card, before it is chosen. Presenting a
            // guess with the same confidence as a verified fact is how
            // somebody spends an afternoon on a partition that never existed.
            // Only on a preset that names a real cluster: "any Slurm cluster"
            // asserts nothing to have got wrong, and a badge there would
            // devalue the ones that need it.
            head.append(el("span", "connect-recipe-badge", "untested"));
        }
        card.append(head);
        card.append(el("span", "connect-recipe-blurb", recipe.blurb));
        return card;
    }

    /**
     * The catalogue, for whoever is drawing it: the shapes, then the sites.
     *
     * Two grids and a disclosure, not one grid. A preset that describes a KIND
     * of machine -- any ssh host, any Slurm cluster, a workstation, a cloud
     * account -- fits everybody who will ever open this. A preset that names
     * one organisation's cluster fits the people who have an account on it and
     * nobody else, and at seven cards in one grid the five that fit everybody
     * were competing for attention with two that fit almost no one. So the
     * first screen is the five, and the named sites are one click away, where
     * the people who recognise the name will go looking for them.
     *
     * The split is the recipe's own `institution` flag rather than anything
     * inferred here. `site` is the neighbouring question and NOT this one: AWS
     * and Google Cloud are sites in the sense that matters to the untested
     * badge -- Plexora asserts things about them -- and are shapes in the
     * sense that matters here, because anyone can open an account.
     *
     * `onPick` is handed the whole recipe rather than its id: the caller in
     * this file branches on `extra.flow` to choose which form to draw, and the
     * one in Settings only needs the id -- neither should have to look the
     * recipe up again.
     */
    async function recipeGrid(onPick) {
        function grid(recipes) {
            const list = el("div", "connect-recipes");
            recipes.forEach((recipe) => list.append(recipeCard(recipe, onPick)));
            return list;
        }

        const all = await loadRecipes();
        const named = all.filter((recipe) => recipe.institution);
        const shapes = all.filter((recipe) => !recipe.institution);
        const wrap = el("div", "connect-catalogue");
        // Everything in one grid when the split would leave a screen empty --
        // including against a server old enough not to send the flag at all,
        // where every recipe reads as a shape and `named` comes back empty.
        if (!named.length || !shapes.length) {
            wrap.append(grid(all));
            return wrap;
        }

        wrap.append(grid(shapes));
        const more = grid(named);
        more.hidden = true;
        const caret = el("span", "connect-recipes-caret", "▾");
        const toggle = button("connect-recipes-more", null, () => {
            more.hidden = !more.hidden;
            toggle.setAttribute("aria-expanded", String(!more.hidden));
            caret.textContent = more.hidden ? "▾" : "▴";
        });
        toggle.setAttribute("aria-expanded", "false");
        toggle.append(el("span", null, "Additional presets"), caret);
        wrap.append(toggle, more);
        return wrap;
    }

    //: So two dropdowns on one form never share an id, which is what
    //: `aria-controls` and `aria-activedescendant` are read through.
    let selectCount = 0;

    //: How far down the list the keyboard moves on Page Up/Down. Not the
    //: visible row count -- that changes with the menu's height -- just a
    //: jump big enough to be worth having on the region list.
    const PAGE_JUMP = 8;

    /**
     * A dropdown Plexora draws, in place of the browser's own.
     *
     * A native `<select>` cannot be styled where it matters. The closed
     * control takes a stylesheet; the menu it opens does not -- that is drawn
     * by the operating system, in the system's colours, at the system's size.
     * On a dark dialog it is a white rectangle in the middle of the screen and
     * no rule reaches it.
     *
     * Two things about where the menu lives, both of them load-bearing:
     *
     *   1. **It stays inside the `<dialog>`.** The top layer contains the
     *      dialog's whole subtree, so a child of it paints above a fullscreen
     *      viewer. Anything portalled to `document.body` lands behind -- which
     *      is why the app's own SearchableSelect cannot be used here.
     *   2. **It is positioned `fixed`, from the trigger's rectangle.** Both
     *      `.connect-modal` and `.connect-modal-body` clip their overflow, so
     *      an absolutely-placed menu would be cut off the first time one was
     *      opened near the bottom of a scrolled form.
     *
     * Focus stays on the trigger the whole time it is open, and the active row
     * is announced through `aria-activedescendant` -- the listbox pattern.
     * That is also what makes the menu safe to close on blur: nothing inside
     * it ever takes focus, and its `mousedown` is prevented so that clicking a
     * row cannot either.
     */
    function menuSelect() {
        selectCount += 1;
        const listId = "plexora-select-" + selectCount;
        const root = el("div", "connect-select");
        const trigger = el("button", "connect-select-button");
        trigger.type = "button";
        const shown = el("span", "connect-select-value");
        const caret = el("span", "connect-select-caret");
        caret.setAttribute("aria-hidden", "true");
        trigger.append(shown, caret);
        trigger.setAttribute("aria-haspopup", "listbox");
        trigger.setAttribute("aria-expanded", "false");
        trigger.setAttribute("aria-controls", listId);
        const menu = el("div", "connect-select-menu");
        menu.setAttribute("role", "listbox");
        menu.id = listId;
        menu.hidden = true;
        root.append(trigger, menu);

        let items = [];
        let rows = [];
        let value = "";
        let active = -1;
        let open = false;

        function indexOf(wanted) {
            for (let i = 0; i < items.length; i += 1) {
                if (items[i].value === wanted) return i;
            }
            return -1;
        }

        function paint() {
            const at = indexOf(value);
            // The raw value when it is not on the list, rather than a blank
            // control: a zone taken off an instance, or a region that came
            // from a bucket, is a real answer even when the catalogue has
            // never heard of it.
            shown.textContent = at >= 0 ? items[at].label : (value || "");
            shown.classList.toggle("is-placeholder", !value);
            rows.forEach((row, index) => {
                row.setAttribute("aria-selected", index === at ? "true" : "false");
                row.classList.toggle("is-chosen", index === at);
                row.classList.toggle("is-active", open && index === active);
            });
            if (open && active >= 0 && rows[active]) {
                trigger.setAttribute("aria-activedescendant", rows[active].id);
            } else {
                trigger.removeAttribute("aria-activedescendant");
            }
        }

        /** Put the menu where the trigger is, and on whichever side fits. */
        function place() {
            const box = trigger.getBoundingClientRect
                ? trigger.getBoundingClientRect() : null;
            if (!box) return;
            const tall = window.innerHeight || 800;
            const below = tall - box.bottom;
            menu.style.left = Math.round(box.left) + "px";
            // At least as wide as the control, and free to be wider. The form
            // is a two-column grid of 14rem cells and a machine type carries
            // three facts on one line -- sized to the trigger, every row in
            // that menu would end in an ellipsis.
            menu.style.minWidth = Math.round(box.width) + "px";
            // Upwards only when there is genuinely more room up there. A menu
            // that flips on a form somebody is still filling in is worse than
            // a short one.
            if (below < 180 && box.top > below) {
                menu.style.top = "auto";
                menu.style.bottom = Math.round(tall - box.top + 4) + "px";
                menu.style.maxHeight = Math.round(box.top - 16) + "px";
            } else {
                menu.style.bottom = "auto";
                menu.style.top = Math.round(box.bottom + 4) + "px";
                menu.style.maxHeight = Math.round(below - 16) + "px";
            }
            // Measured once it is visible, because "wider than the trigger"
            // means it can now run off the right edge of a narrow window.
            const wide = menu.getBoundingClientRect
                ? menu.getBoundingClientRect().width : 0;
            const room = (window.innerWidth || 1200) - 12;
            if (wide && box.left + wide > room) {
                menu.style.left = Math.max(12, Math.round(room - wide)) + "px";
            }
        }

        function show() {
            if (open || trigger.disabled || !items.length) return;
            open = true;
            active = Math.max(indexOf(value), 0);
            menu.hidden = false;
            trigger.setAttribute("aria-expanded", "true");
            place();
            paint();
            if (rows[active] && rows[active].scrollIntoView) {
                rows[active].scrollIntoView({ block: "nearest" });
            }
        }

        function shut() {
            if (!open) return;
            open = false;
            menu.hidden = true;
            trigger.setAttribute("aria-expanded", "false");
            paint();
        }

        function choose(next, tell) {
            const before = value;
            value = String(next == null ? "" : next);
            paint();
            if (tell && value !== before && api.onchange) api.onchange();
        }

        function move(by) {
            if (!items.length) return;
            if (!open) { show(); return; }
            const last = items.length - 1;
            active = Math.min(last, Math.max(0, (active < 0 ? 0 : active) + by));
            paint();
            if (rows[active] && rows[active].scrollIntoView) {
                rows[active].scrollIntoView({ block: "nearest" });
            }
        }

        /** Jump to the next option starting with `letter`, cycling. */
        function jump(letter) {
            const from = (active < 0 ? indexOf(value) : active) + 1;
            for (let step = 0; step < items.length; step += 1) {
                const at = (from + step) % items.length;
                const label = String(items[at].label || "").toLowerCase();
                if (label.indexOf(letter) === 0) {
                    active = at;
                    if (!open) choose(items[at].value, true);
                    paint();
                    return;
                }
            }
        }

        trigger.addEventListener("click", () => (open ? shut() : show()));
        trigger.addEventListener("blur", shut);
        trigger.addEventListener("keydown", (event) => {
            const key = event.key;
            if (key === "ArrowDown") { move(1); event.preventDefault(); }
            else if (key === "ArrowUp") { move(-1); event.preventDefault(); }
            else if (key === "PageDown") { move(PAGE_JUMP); event.preventDefault(); }
            else if (key === "PageUp") { move(-PAGE_JUMP); event.preventDefault(); }
            else if (key === "Home") { move(-items.length); event.preventDefault(); }
            else if (key === "End") { move(items.length); event.preventDefault(); }
            else if (key === "Enter" || key === " ") {
                if (open && active >= 0) { choose(items[active].value, true); shut(); }
                else show();
                event.preventDefault();
            } else if (key === "Escape") {
                // Only when this menu is the thing being escaped from. The
                // dialog closes on Escape too, and a dropdown that let the
                // key through would take the whole form with it.
                if (open) { shut(); event.preventDefault(); event.stopPropagation(); }
            } else if (key === "Tab") {
                shut();
            } else if (key && key.length === 1 && /\S/.test(key)) {
                jump(key.toLowerCase());
                event.preventDefault();
            }
        });
        // So that clicking a row cannot move focus off the trigger, which
        // would fire the blur above and shut the menu before the click landed.
        menu.addEventListener("mousedown", (event) => event.preventDefault());

        function setOptions(next) {
            items = (next || []).map((item) => ({
                value: String(item.value == null ? "" : item.value),
                label: String(item.label == null ? item.value : item.label),
            }));
            rows = items.map((item, index) => {
                const row = el("div", "connect-select-option", item.label);
                row.id = listId + "-" + index;
                row.setAttribute("role", "option");
                row.addEventListener("click", () => {
                    choose(item.value, true);
                    shut();
                });
                return row;
            });
            menu.replaceChildren(...rows);
            // The value is deliberately left alone. Replacing the options is
            // not an answer to the question, and the callers that DO want the
            // pick reset -- a new project's buckets, a new region's zones --
            // say so on the next line. `paint` shows a value the new list has
            // never heard of as itself rather than as a blank.
            active = -1;
            if (open) place();
            paint();
        }

        const api = {
            root: root,
            trigger: trigger,
            menu: menu,
            //: This control's own name, so the field around it can hang a
            //: label id off it and point `aria-labelledby` at it.
            id: listId,
            //: Assigned, never added to -- the same rule the rest of this form
            //: follows, so a redraw cannot stack a second reaction.
            onchange: null,
            plexoraHint: null,
            get value() { return value; },
            set value(next) { choose(next, false); },
            get disabled() { return Boolean(trigger.disabled); },
            set disabled(off) {
                trigger.disabled = Boolean(off);
                if (off) shut();
            },
            get options() { return items.slice(); },
            has: (wanted) => indexOf(String(wanted)) >= 0,
            setOptions: setOptions,
            close: shut,
        };
        // Hung off the element so anything holding the DOM can reach the
        // control, the way `plexoraHint` is reached on the field it belongs
        // to. The probe drives both.
        root.plexoraSelect = api;
        return api;
    }

    /**
     * The controls a preset form is built from, bound to one `boxes` map.
     *
     * Shared by both forms rather than written twice, because the thing that
     * has to stay true of all of them is the same: whatever a control is made
     * of, `boxes[key].value` is what the answer is read off. That is what lets
     * one collection loop serve a text box, a dropdown and a switch -- and it
     * is why a control added here needs nothing added to the submit handler.
     */
    function formFields(boxes) {
        function field(key, label, placeholder, initial, hint) {
            const wrap = el("label", "connect-field");
            wrap.append(el("span", "connect-field-label", label));
            const input = el("input", "form-control");
            input.type = "text";
            input.autocomplete = "off";
            input.spellcheck = false;
            input.value = initial == null ? "" : String(initial);
            if (placeholder) input.placeholder = placeholder;
            wrap.append(input);
            if (hint) wrap.append(el("span", "connect-field-hint", hint));
            boxes[key] = input;
            return wrap;
        }

        /**
         * A dropdown, shaped like the boxes beside it.
         *
         * `menuSelect`, not a native `<select>`: see the note on it for why
         * the browser's own menu is not usable on this dialog, and why the
         * app's SearchableSelect is not either.
         */
        function selectField(key, label, options, initial, hint) {
            const select = menuSelect();
            // A `<div>`, not the `<label>` the text boxes use. A label
            // forwards a click to the first labelable thing inside it, the
            // trigger IS a button, and the menu is inside the field -- so
            // clicking a row would choose it, close the menu, and then have
            // the label re-open it. The name is tied on with `aria-labelledby`
            // instead, which is what the label element was buying.
            const wrap = el("div", "connect-field");
            const name = el("span", "connect-field-label", label);
            name.id = select.id + "-label";
            select.trigger.setAttribute("aria-labelledby", name.id);
            wrap.append(name);
            select.setOptions(options || []);
            if (initial != null) select.value = String(initial);
            wrap.append(select.root);
            const note = el("span", "connect-field-hint", hint || "");
            note.hidden = !hint;
            wrap.append(note);
            boxes[key] = select;
            // Handed back so a caller can react to a change or rewrite the
            // hint -- which is what "detected from your bucket" is.
            select.plexoraHint = note;
            return wrap;
        }

        /**
         * A dropdown of what the account actually has, plus room to name
         * something it did not list.
         *
         * A `<datalist>` on a text box was the first shape of this and it was
         * the wrong one. Browsers draw no affordance for a datalist: a list
         * that had been fetched, parsed and filled looked exactly like an
         * empty text box, and the only way to discover it was to start typing
         * a name you had opened the field to look up. A `<select>` says it can
         * be opened. The typed escape stays, because listing is a permission
         * of its own -- an account can have every right to one bucket and none
         * to enumerate the project's.
         */
        function pickField(key, label, placeholder, hint, words) {
            const select = menuSelect();
            // A `<div>` for the reason spelled out in `selectField`, and here
            // it matters twice over: this field holds a menu AND a box, and a
            // label wrapping both would send every click in it to the first
            // one.
            const wrap = el("div", "connect-field");
            const name = el("span", "connect-field-label", label);
            name.id = select.id + "-label";
            select.trigger.setAttribute("aria-labelledby", name.id);
            wrap.append(name);
            const input = el("input", "form-control");
            input.type = "text";
            input.autocomplete = "off";
            input.spellcheck = false;
            input.hidden = true;
            input.setAttribute("aria-labelledby", name.id);
            if (placeholder) input.placeholder = placeholder;
            wrap.append(select.root, input);
            const note = el("span", "connect-field-hint", hint || "");
            note.hidden = !hint;
            wrap.append(note);
            // Seeded, so the field reads as a dropdown with nothing in it yet
            // rather than as an empty control of unknown purpose. `fill` puts
            // the same line back above the real options.
            if (words.choose) select.setOptions([{ value: "", label: words.choose }]);

            //: Whether the box, rather than the menu, is the field right now.
            //: Two ways in: the list came back empty, or "type it" was chosen.
            let typing = false;

            // One answer out of two controls. Which of them is being read
            // follows from which one is on screen, so the collection loop
            // above never learns this field is made of more than one thing --
            // and neither does anything that disables the form.
            boxes[key] = {
                get value() {
                    return typing ? input.value.trim() : select.value;
                },
                set disabled(off) {
                    select.disabled = off;
                    input.disabled = off;
                },
            };

            function fill(items, empty, initial) {
                if (!items || !items.length) {
                    // Nothing to choose from is not a dead end. The box is the
                    // only way left to name one, so the box becomes the field.
                    typing = true;
                    select.root.hidden = true;
                    input.hidden = false;
                    note.textContent = empty || hint || "";
                    note.hidden = !note.textContent;
                    return;
                }
                typing = false;
                select.root.hidden = false;
                input.hidden = true;
                input.value = "";
                note.textContent = hint || "";
                note.hidden = !note.textContent;
                const rows = words.choose
                    ? [{ value: "", label: words.choose }].concat(items) : items;
                select.setOptions(rows.concat([
                    { value: PICK_OTHER, label: words.other },
                ]));
                select.value = initial == null ? "" : String(initial);
            }

            /** Wire both controls to one reaction, whichever is showing. */
            function onPick(react) {
                select.onchange = () => {
                    typing = select.value === PICK_OTHER;
                    input.hidden = !typing;
                    if (typing) {
                        input.value = "";
                        input.focus();
                    }
                    if (react) react();
                };
                if (react) input.onchange = react;
            }

            /**
             * Put a value into the field, whichever control is showing it.
             *
             * Only ever used to fill the form in from a saved profile. A name
             * the list covers selects that row; one it does not -- a public
             * bucket, a VM in another project -- turns the field into the box
             * it would have been typed into, holding that name. Silent about
             * an empty value: absent is not an answer to restore.
             */
            function choose(value) {
                const text = value == null ? "" : String(value).trim();
                if (!text) return;
                if (!typing && select.has && select.has(text)) {
                    select.value = text;
                    return;
                }
                typing = true;
                select.root.hidden = true;
                input.hidden = false;
                input.value = text;
            }

            return { wrap, select, input, note, fill, onPick, choose };
        }

        /**
         * One answer out of two or three, with the reason under it.
         *
         * A dropdown hides the options it is not showing, which is exactly
         * wrong for a question whose whole difficulty is comparing them:
         * Spot against Standard, and the three things that can happen to a
         * machine when a session ends, are choices somebody makes once and
         * has to be able to see the consequences of. So the options are on
         * screen together, and the explanation under the group belongs to
         * whichever one is selected -- one sentence at a time, rather than a
         * wall of three that nobody reads.
         *
         * Native radios, not buttons pretending to be them: this is the one
         * control on the form where a screen reader's own group semantics and
         * the arrow-key behaviour come free, and the visible mark is drawn
         * beside a real input the same way the switch above draws its track.
         */
        function choiceField(key, label, options, initial, hint) {
            choiceCount += 1;
            const groupName = "plexora-choice-" + choiceCount;
            const wrap = el("div", "connect-field connect-choice");
            if (label) wrap.append(el("span", "connect-field-label", label));
            const rows = el("div", "connect-choice-rows");
            rows.setAttribute("role", "radiogroup");
            if (label) rows.setAttribute("aria-label", label);
            wrap.append(rows);
            const why = el("span", "connect-field-hint connect-choice-why");
            const note = el("span", "connect-field-hint connect-choice-note");
            note.hidden = true;
            wrap.append(why, note);

            let value = initial == null ? "" : String(initial);
            let react = null;
            const made = (options || []).map((option) => {
                const row = el("label", "connect-choice-row");
                const input = el("input", "connect-choice-input");
                input.type = "radio";
                input.name = groupName;
                input.value = option.value;
                const mark = el("span", "connect-choice-mark");
                mark.setAttribute("aria-hidden", "true");
                row.append(input, mark,
                           el("span", "connect-choice-label", option.label));
                input.addEventListener("change", () => {
                    if (!input.checked || input.disabled) return;
                    value = option.value;
                    paint();
                    if (react) react();
                });
                rows.append(row);
                return { option: option, row: row, input: input };
            });

            function paint() {
                let chosen = null;
                made.forEach((entry) => {
                    const on = entry.option.value === value;
                    // Written on every paint rather than left to the browser's
                    // radio-group behaviour, because the value also moves from
                    // here -- when a row is disabled out from under it.
                    entry.input.checked = on;
                    entry.row.classList.toggle("is-chosen", on);
                    if (on) chosen = entry;
                });
                const text = (chosen && chosen.option.hint) || hint || "";
                why.textContent = text;
                why.hidden = !text;
            }

            /**
             * Take some options off the table, and say why.
             *
             * The reason is a line under the group rather than a tooltip on
             * the row: "Delete VM" being unavailable is a fact about whose
             * machine it is, and somebody who cannot see the explanation will
             * read the greyed row as a bug.
             */
            function disable(names, reason) {
                let moved = false;
                made.forEach((entry) => {
                    const off = (names || []).indexOf(entry.option.value) >= 0;
                    entry.input.disabled = off;
                    entry.row.classList.toggle("is-off", off);
                    if (off && value === entry.option.value) moved = true;
                });
                if (moved) {
                    // Never leave the answer on a row that cannot be chosen:
                    // the collection loop reads this value whether or not the
                    // row it came from is still offerable.
                    const first = made.find((one) => !one.input.disabled);
                    value = first ? first.option.value : "";
                }
                note.textContent = (names || []).length ? (reason || "") : "";
                note.hidden = !note.textContent;
                paint();
                if (moved && react) react();
            }

            boxes[key] = {
                get value() { return value; },
                set disabled(off) {
                    made.forEach((entry) => { entry.input.disabled = off; });
                },
            };
            paint();
            return {
                wrap: wrap,
                onPick: (fn) => { react = fn; },
                disable: disable,
                set: (next) => { value = String(next || ""); paint(); },
            };
        }

        /**
         * A switch, shaped so the form's own grid places it.
         *
         * `.connect-form` is `repeat(auto-fit, minmax(14rem, 1fr))`, so this
         * is a cell like any other: it sits beside the box above it wherever
         * the dialog is wide enough for two columns, and wraps under it where
         * it is not. That is the whole of "inline when there is room" -- there
         * is no row here to be given one.
         */
        function switchField(key, label, hint, initial) {
            const wrap = el("label", "connect-field connect-switch");
            const text = el("span", "connect-switch-text");
            text.append(el("span", "connect-field-label", label));
            if (hint) text.append(el("span", "connect-field-hint", hint));
            const input = el("input", "connect-switch-input");
            input.type = "checkbox";
            input.checked = Boolean(initial);
            const track = el("span", "connect-switch-track");
            track.setAttribute("aria-hidden", "true");
            track.append(el("span", "connect-switch-thumb"));
            wrap.append(text, input, track);
            // Read by the same loop that reads every text box, so the answers
            // object stays one shape. `checked`, not `value`: the server reads
            // this key as a boolean (recipes.compose keeps the switches out of
            // the trim-to-string pass for exactly this reason).
            boxes[key] = { get value() { return input.checked; } };
            return wrap;
        }

        /**
         * An answer with no control: something the form worked out for itself.
         *
         * The bucket's location and the signed-in account are facts the form
         * LEARNED rather than asked for, and they belong in the same map for
         * the same reason everything else does -- the submit handler must not
         * have to know which answers came from a box.
         */
        function derived(key, read) {
            boxes[key] = { get value() { return read(); } };
        }

        /**
         * A list of ports to carry through the tunnel, built one at a time.
         *
         * One box and an Add button into a list of chips, rather than a
         * textarea of them: a textarea asks somebody to know the format before
         * they can type, and accepts anything -- including the thing they meant
         * to delete last time. The same control the Settings form had before
         * this form absorbed it, in the same words.
         *
         * Spans the whole grid rather than sitting in a 14rem cell, because it
         * is a row of two controls over a wrapping list and none of that fits
         * beside a text box.
         */
        function portsField(key, label, hint, initial) {
            const wrap = el("div", "connect-field connect-field-wide");
            wrap.append(el("span", "connect-field-label", label));
            const row = el("div", "connect-port-row");
            const input = el("input", "form-control");
            input.type = "text";
            input.autocomplete = "off";
            input.spellcheck = false;
            input.placeholder = "8642";
            row.append(input, button("btn connect-port-add", "Add port",
                                     () => take()));
            const chips = el("div", "connect-chips");
            wrap.append(row, chips);
            if (hint) wrap.append(el("span", "connect-field-hint", hint));

            let ports = (initial || []).map(String)
                .filter((port) => port.trim());

            function draw() {
                chips.replaceChildren();
                ports.forEach((port) => {
                    const chip = el("span", "connect-chip");
                    chip.append(el("span", null, port));
                    const drop = button("connect-chip-drop", null, () => {
                        ports = ports.filter((other) => other !== port);
                        draw();
                    });
                    drop.setAttribute("aria-label", "Remove port " + port);
                    drop.innerHTML =
                        '<span class="fas fa-xmark" aria-hidden="true"></span>';
                    chip.append(drop);
                    chips.append(chip);
                });
                chips.hidden = !ports.length;
            }

            function take() {
                const value = (input.value || "").trim();
                input.value = "";
                if (input.focus) input.focus();
                // Silently, not with an error: adding 8642 twice is somebody
                // who cannot see whether the first one landed, and the answer
                // they want is the list, not a complaint.
                if (!value || ports.indexOf(value) >= 0) return;
                ports.push(value);
                draw();
            }

            // Enter is what a person types after a number in a box next to an
            // Add button. There is no <form> here to submit, so without this it
            // does nothing at all and the port is silently not added.
            input.addEventListener("keydown", (event) => {
                if (event.key !== "Enter") return;
                event.preventDefault();
                take();
            });
            draw();
            // An array, read by the same loop that reads every text box.
            // `collect` passes anything that is not a string through untouched,
            // and `recipes.compose` keeps this key out of its trim-to-string
            // pass for the same reason it keeps the switches out.
            boxes[key] = { get value() { return ports.slice(); } };
            return wrap;
        }

        return { field, selectField, switchField, derived, pickField,
                 choiceField, portsField };
    }

    //: So two radio groups on one form never share a `name`, which is what
    //: makes a browser treat them as one group.
    let choiceCount = 0;

    //: The option that hands a `pickField` back to typing. Not a name anything
    //: can be called, so it can never collide with a real bucket or VM.
    const PICK_OTHER = "::type-it::";

    /**
     * Which step the connection is on, and what has already happened.
     *
     * Derived from the current state rather than accumulated, because the
     * state is all the server reports and a client-side history would disagree
     * with it the first time a retry reused this dialog. Anything before the
     * current state in the list is done by definition: a connection cannot be
     * tunnelling without having signed in.
     *
     * `lastOpening` is the one exception, and it is only ever used to place a
     * failure. "failed" is not a step -- it is what happened to whichever step
     * was running -- and marking the whole list pending would throw away the
     * most useful thing on screen, which is how far it got.
     */
    function stepStates(state, kind, queued, lastOpening, install, gcloud) {
        const steps = STEPS.filter((step) => (!step.queued || queued)
                                          && (!step.install || Boolean(install))
                                          && (!step.gcloud || Boolean(gcloud)));
        const failed = state === "failed" || state === "exited";
        const at = steps.findIndex(
            (step) => step.state === (failed ? lastOpening : state));
        return steps.map((step, index) => {
            let label = step.label;
            if (step.state === "waiting_for_app" && kind === "node") {
                label = NODE_LAST_STEP;
            } else if (step.state === "installing") {
                // `install` doubles as the environment's name when there is
                // one: the caller has a profile, not two separate answers, and
                // a second parameter that could only ever be set alongside
                // this one is a pair that can be passed inconsistently.
                label = installLabel(typeof install === "string" ? install : "");
            }
            let status = "pending";
            if (state === "connected") status = "done";
            else if (at < 0) status = "pending";
            else if (index < at) status = "done";
            else if (index === at) status = failed ? "failed" : "active";
            return { label: label, status: status };
        });
    }

    // -- the dialog ----------------------------------------------------------

    function build() {
        const dialog = el("dialog", "connect-modal");

        const head = el("div", "connect-modal-head");
        const heading = el("div", "connect-modal-heading");
        const title = el("h2", "connect-modal-title", "Connect to a server");
        const subtitle = el("p", "connect-modal-subtitle");
        heading.append(title, subtitle);
        head.append(heading);
        dialog.append(head);

        const body = el("div", "connect-modal-body");
        dialog.append(body);

        const actions = el("div", "connect-modal-actions");
        dialog.append(actions);

        document.body.appendChild(dialog);
        return { dialog, title, subtitle, body, actions };
    }

    /**
     * @function open - connect to a machine, showing the whole of it.
     *
     * @param options `name` -- the saved profile to connect, or null to ask
     *   which; `kind` -- "node" for a data node (everything in the app) or
     *   "viewer" for a tunnelled Plexora (which nothing in the app opens: see
     *   the note in views/settingsPage.js); `intent` -- one sentence saying
     *   why, shown under the title; `view: "recipes"` -- open straight on the
     *   presets, for a caller whose button said "Add a server" rather than
     *   "Connect".
     * @returns Promise<{connected, name, node, kind}>. `connected` is false
     *   for every way out that did not end connected, including closing the
     *   dialog on a connection that is still opening -- which keeps running.
     */
    function open(options = {}) {
        if (openDialog) {
            openDialog.close();
            openDialog = null;
        }
        const kind = options.kind === "viewer" ? "viewer" : "node";
        const intent = options.intent || "";
        const parts = build();
        const dialog = parts.dialog;
        openDialog = dialog;

        //: Which profile this dialog is about, or null while it is still the
        //: question. Set by choosing a row, and by `open({name})`.
        let name = options.name || null;
        let unwatch = null;
        let result = { connected: false, name: null, node: null, kind: kind };
        //: The question drawn right now, so a redraw does not replace the box
        //: somebody is halfway through typing a password into.
        let drawnPrompt = null;
        //: The log pane. Built once, repainted, and never replaced -- it owns
        //: a scroll position that this closure does not, and whether it is
        //: following its own output. See services/logTerminal.js.
        let logView = null;
        //: The last step this dialog actually saw running, so that a failure
        //: can be drawn against it. See stepStates.
        let lastOpening = null;
        //: "auto" -- the body follows the state, which is what it does for all
        //: of connecting. Anything else is a form somebody is typing into, and
        //: the second-by-second update must leave it alone: without this, a
        //: poll would replace the half-filled Add-a-server boxes with the list
        //: of machines every second.
        let view = "auto";

        function close(answer) {
            result = Object.assign(result, answer || {});
            if (unwatch) unwatch();
            unwatch = null;
            dialog.close();
        }

        // -- the two views -----------------------------------------------------

        /** No profile chosen yet: which machine is this? */
        function drawChooser(snapshot) {
            drawnActions = null;
            parts.title.textContent = "Connect to a server";
            parts.subtitle.textContent = intent
                || "Pick the machine to open a connection to. Nothing has to "
                   + "be set up in advance.";
            parts.body.replaceChildren();
            parts.actions.replaceChildren();

            const entries = snapshot.entries || [];
            if (!entries.length) {
                parts.body.append(el("p", "connect-modal-empty",
                                     "No servers saved yet. Add one and "
                                     + "Plexora will handle the SSH for you."));
            } else {
                const list = el("ul", "connect-modal-list");
                entries.forEach((entry) => list.append(chooserRow(entry)));
                parts.body.append(list);
            }
            if (snapshot.error) parts.body.append(errorLine(snapshot.error));

            parts.actions.append(
                button("btn btn-secondary", "Add a new server", addServer),
                el("div", "connect-modal-spacer"),
                button("btn btn-outline-light", "Cancel", () => close(null)));
        }

        function chooserRow(entry) {
            const item = el("li", "connect-modal-row");
            const main = el("div", "connect-modal-row-main");
            main.append(el("span", "connect-modal-row-name", entry.label));
            main.append(el("span", "connect-modal-row-detail", entry.detail));
            item.append(main);

            const half = Remotes().half(entry, kind);
            const ready = kind === "node" ? Boolean(half.node)
                                          : half.state === "connected";
            const chip = el("span", "connect-modal-chip",
                            ready ? "Connected" : Remotes().label(half.state));
            chip.classList.add(ready ? "is-ready"
                : (Remotes().isOpening(half.state) ? "is-busy" : "is-idle"));
            item.append(chip);

            if (entry.queued && !ready) {
                item.append(el("div", "connect-modal-row-note",
                               "Runs Plexora inside a job, so connecting waits "
                               + "for the scheduler."));
            }
            item.append(button("btn btn-primary", ready ? "Use this" : "Connect",
                               () => {
                                   name = entry.name;
                                   if (ready) return settle(entry);
                                   begin();
                               }));
            return item;
        }

        /** A profile chosen: what is it doing? */
        function drawProgress(snapshot) {
            const entry = (snapshot.entries || [])
                .find((item) => item.name === name);
            const half = entry ? Remotes().half(entry, kind) : null;
            const deep = Remotes().focused(name, kind) || {};
            const state = (half && half.state) || deep.state || "connecting";
            const prompt = (half && half.prompt) || deep.prompt || null;
            const phase = (half && half.phase) || deep.phase || "";
            const error = (half && half.error) || deep.error || null;
            const log = (deep.log && deep.log.length)
                ? deep.log : ((half && half.log) || []);

            if (state === "connected" && entry) return settle(entry);
            if (Remotes().isOpening(state)) lastOpening = state;

            parts.title.textContent = "Connecting to “" + name + "”";
            // The address, not the intent: by now the user has chosen, and
            // what is worth confirming is which machine they chose.
            parts.subtitle.textContent = entry ? entry.detail : "";

            // **The body is built once and repainted, never rebuilt.** This
            // runs every second, and two things in it hold state the DOM owns
            // rather than this closure: the password box, mid-typing, and the
            // log pane's scroll position. Replacing the pane every tick threw
            // somebody reading the log back to wherever a fresh element starts
            // -- which looked like it was working, because the position it
            // reset to was usually the one they had just scrolled to.
            if (!stepsEl || stepsEl.parentNode !== parts.body) {
                parts.body.replaceChildren();
                parts.body.append(stepList(), phaseLine(), promptSlot(),
                                  errorSlot(), logPane());
                drawnPrompt = null;
            }
            paintSteps(state, entry, phase, error);
            // The prompt is redrawn only when it is a DIFFERENT question --
            // and cleared when there is none, so an answered one does not sit
            // there looking as though it still wants something.
            if (!prompt) {
                if (drawnPrompt !== null) promptEl.replaceChildren();
                drawnPrompt = null;
            } else if (drawnPrompt !== prompt.id) {
                drawPrompt(prompt);
                drawnPrompt = prompt.id;
            }
            paintLog(log);
            paintActions(state, error,
                         (half && half.recovery) || deep.recovery || "");
        }

        // -- the pieces of the progress view -----------------------------------

        let stepsEl = null;
        let phaseEl = null;
        let promptEl = null;
        let errorEl = null;

        function stepList() {
            stepsEl = el("ol", "connect-steps");
            return stepsEl;
        }

        function phaseLine() {
            phaseEl = el("p", "connect-phase");
            return phaseEl;
        }

        function promptSlot() {
            promptEl = el("div", "connect-prompt-slot");
            return promptEl;
        }

        function errorSlot() {
            errorEl = el("div", "connect-modal-error");
            errorEl.setAttribute("role", "alert");
            errorEl.hidden = true;
            return errorEl;
        }

        function paintSteps(state, entry, phase, error) {
            stepsEl.replaceChildren();
            // The environment's name when the profile has one, `true` when it
            // installs without naming one, `false` when it does not install.
            // See stepStates: one value, because "does it install" and "into
            // what" are one fact about one profile.
            const installing = !entry || !entry.install ? false
                : (entry.installEnv || true);
            stepStates(state, kind, entry ? entry.queued : false, lastOpening,
                       installing, Boolean(entry && entry.gcloud))
                .forEach((step) => {
                    const item = el("li", "connect-step is-" + step.status);
                    item.append(el("span", "connect-step-mark"));
                    item.append(el("span", "connect-step-label", step.label));
                    stepsEl.append(item);
                });
            // The server's own sentence, not a translation of it: it is where
            // "a first start can take a few minutes while it loads" comes
            // from, and that is the line that stops a slow node reading as a
            // hung one.
            phaseEl.textContent = phase;
            phaseEl.hidden = !phase;
            errorEl.textContent = error || "";
            errorEl.hidden = !error;
        }

        function drawPrompt(prompt) {
            promptEl.replaceChildren();
            const box = el("form", "connect-prompt");
            const secret = Remotes().isSecret(prompt.text);
            // Verbatim and wrapped rather than truncated: on a host-key
            // question this text IS the fingerprint being checked, and it
            // arrives as several lines.
            box.append(el("div", "connect-prompt-text", prompt.text));

            const controls = el("div", "connect-prompt-controls");
            const field = el("input", "form-control");
            field.type = secret ? "password" : "text";
            field.autocomplete = "off";
            field.spellcheck = false;
            field.setAttribute("aria-label", secret ? "Your answer, hidden"
                                                    : "Your answer");
            controls.append(field);

            const send = (value) => {
                field.value = "";
                Remotes().answer(name, kind, prompt.id, value)
                    .catch((e) => showError(e.message));
            };
            if (!secret) {
                // The two answers ssh actually accepts, as buttons -- while
                // leaving the box, because some versions want the fingerprint
                // typed back and anything else it asks still has to be
                // answerable.
                controls.append(
                    button("btn btn-secondary", "Yes", () => send("yes")),
                    button("btn btn-secondary", "No", () => send("no")));
            }
            controls.append(button("btn btn-primary", "Send",
                                   () => send(field.value)));
            box.append(controls);
            box.addEventListener("submit", (event) => {
                if (event.preventDefault) event.preventDefault();
                send(field.value);
            });
            promptEl.append(box);
            if (field.focus) setTimeout(() => field.focus(), 0);
        }

        /**
         * The log, as a terminal that follows its own output.
         *
         * Everything about how it behaves is in services/logTerminal.js,
         * because the Settings page shows the same log for the same connection
         * and the two of them disagreeing about what a terminal does is how
         * this went wrong the first time.
         */
        function logPane() {
            logView = window.PlexoraLogTerminal.create({
                title: "Connection log",
                empty: "Waiting for the first line…",
            });
            return logView.element;
        }

        function paintLog(lines) {
            if (logView) logView.paint(lines);
        }

        //: Which set of buttons is on screen. Compared before rebuilding,
        //: because this runs every second and replacing the row would take the
        //: focus off whichever button somebody had tabbed to -- once a second,
        //: for the whole of a queued job.
        let drawnActions = null;

        function paintActions(state, error, recovery) {
            const opening = Remotes().isOpening(state);
            const shape = (state === "failed" || state === "exited") ? "failed"
                : (opening ? "opening" : "idle");
            // The recovery is part of the shape, not a detail inside it: it
            // arrives on the same tick as the failure it belongs to, and a row
            // compared on state alone would keep the buttons drawn for the
            // previous attempt.
            const want = shape + ":" + (recovery || "");
            if (want === drawnActions) {
                if (error) showError(error);
                return;
            }
            drawnActions = want;
            parts.actions.replaceChildren();

            if (state === "failed" || state === "exited") {
                // Whatever the failure was, the two ways out are the same:
                // change the connection, or try it again. The third button
                // appears only when the server named a fix that IS one press
                // -- and it goes on the right, where the primary action is,
                // because it is the one most likely to work.
                parts.actions.append(
                    button("btn btn-secondary", "Edit connection", editServer),
                    el("div", "connect-modal-spacer"),
                    button("btn btn-outline-light", "Close", () => close(null)));
                if (recovery === "standard") {
                    parts.actions.append(
                        button("btn btn-outline-light", "Try again", begin),
                        button("btn btn-primary", "Reconnect with Standard",
                               useStandard));
                } else {
                    parts.actions.append(
                        button("btn btn-primary", "Try again", begin));
                }
                return;
            }
            if (opening) {
                parts.actions.append(
                    button("btn btn-secondary", "Stop connecting", stopIt),
                    el("div", "connect-modal-spacer"),
                    // Not "Cancel". The ssh belongs to the server and a queued
                    // job is a real fifteen minutes; closing this window ends
                    // the watching, not the connection, and the button has to
                    // say which of those it does.
                    button("btn btn-outline-light", "Continue in background",
                           () => close(null)));
                return;
            }
            parts.actions.append(
                el("div", "connect-modal-spacer"),
                button("btn btn-outline-light", "Cancel", () => close(null)),
                button("btn btn-primary", "Connect", begin));
            if (error) showError(error);
        }

        function errorLine(message) {
            const line = el("div", "connect-modal-error", message);
            line.setAttribute("role", "alert");
            return line;
        }

        function showError(message) {
            if (!errorEl) return;
            errorEl.textContent = message;
            errorEl.hidden = !message;
        }

        // -- actions -----------------------------------------------------------

        /**
         * Start this connection, or attach to the one already running.
         *
         * Pressing Connect on something that is already connecting is not an
         * error and must not look like one: the profile may have been opened
         * from a second data field, or from Settings in another tab, and what
         * the user wants is to watch it -- which is what this dialog is. The
         * server would refuse the second POST with a 409, and reporting that
         * as a failure is how the old flow made "somebody else already started
         * it" look like a broken connection.
         */
        async function begin() {
            if (!name) return;
            view = "auto";
            drawnPrompt = null;
            lastOpening = null;
            // A new attempt is new output, and whoever pressed Try again is
            // asking to watch it rather than to stay where they had scrolled.
            if (logView) logView.follow();
            watch();
            const known = Remotes().snapshot().loaded
                ? Remotes().snapshot() : await Remotes().refresh();
            if (live(known)) return;
            try {
                await Remotes().connect(name, kind);
            } catch (e) {
                // A refusal is usually "already connecting", which is not a
                // failure -- re-read before believing it.
                if (!live(Remotes().snapshot())) showError(e.message);
            }
        }

        /**
         * Buy the VM outright, then retry the request that was refused.
         *
         * Offered only where a zone had no spare Spot capacity, which is a
         * price problem rather than a configuration one: everything else about
         * the profile is right, and the same request at full price is very
         * likely to succeed in the same zone, this minute.
         *
         * The saved profile is changed, not overridden for one attempt. A
         * profile that said Spot while the machine it describes was bought
         * outright would be wrong on the Settings card, wrong in the form and
         * wrong on the next connection -- and the price is exactly the sort of
         * thing that must not be quietly different from what the record says.
         * The Settings card prints `standard` afterwards, so it is visible.
         */
        async function useStandard() {
            if (!name) return;
            try {
                await Remotes().vmStandard(name);
            } catch (e) {
                showError(e.message);
                return;
            }
            begin();
        }

        /** Whether this profile's half of the connection is up or on its way. */
        function live(snapshot) {
            const entry = (snapshot.entries || [])
                .find((item) => item.name === name);
            if (!entry) return false;
            const half = Remotes().half(entry, kind);
            const ready = kind === "node" ? Boolean(half.node)
                                          : half.state === "connected";
            if (ready) {
                settle(entry);
                return true;
            }
            return Remotes().isOpening(half.state);
        }

        function stopIt() {
            if (!name) return close(null);
            // Ends the ssh AND the watching. "Stop connecting" is the one
            // button here that touches the connection, so it also finishes the
            // errand -- offering "Try again" to somebody who just said stop is
            // asking them the question they have answered.
            Remotes().disconnect(name, kind).catch(() => {});
            close(null);
        }

        function settle(entry) {
            const half = Remotes().half(entry, kind);
            close({ connected: true, name: entry.name, kind: kind,
                    node: half.node || null, label: entry.label,
                    detail: entry.detail });
        }

        function editServer() {
            close(null);
            window.location.href = plexoraUrl("settings#remotes");
        }

        // -- adding a server ---------------------------------------------------
        //
        // A preset, then two or three boxes. The catalogue and its cards are at
        // module scope -- Settings draws the same grid on the page itself --
        // and what is left here is which form a chosen card opens.

        async function addServer() {
            view = "recipes";
            drawnActions = null;
            parts.title.textContent = "Add a server";
            parts.subtitle.textContent = "Start from the machine you use. You "
                + "can change any of it afterwards.";
            parts.body.replaceChildren(
                el("p", "connect-modal-empty", "Loading…"));
            parts.actions.replaceChildren(
                button("btn btn-secondary", "Back", () => {
                    view = "auto";
                    drawChooser(Remotes().snapshot());
                }),
                el("div", "connect-modal-spacer"),
                button("btn btn-outline-light", "Cancel", () => close(null)));

            const list = await recipeGrid(openRecipe);
            parts.body.replaceChildren(list);
        }

        /**
         * The form one preset wants, filling it in from a saved profile or not.
         *
         * A preset that names its own flow draws its own form. Every other one
         * is a machine somebody already has, described in the same three or
         * four boxes; the Google Cloud one describes a machine that does not
         * exist yet, and none of those boxes is the question.
         */
        function openRecipe(recipe, saved) {
            const flow = (recipe.extra && recipe.extra.flow) || "";
            if (flow === "gcloud") gcloudForm(recipe, saved);
            else recipeForm(recipe, saved);
        }

        /**
         * One preset's form -- adding a machine, or editing one already saved.
         *
         * `saved` is a profile from `/settings/remotes`, and its presence is
         * the whole of the difference: every box starts out holding what was
         * recorded rather than what the preset guesses, and the button saves
         * instead of connecting. One form for both, because a preset is a
         * filled-in form and an edit is the same form filled in from the other
         * direction -- a second one would be a second set of boxes to keep in
         * step with `compose`, which is exactly what the Settings page's own
         * form was before this absorbed it.
         *
         * The server does the reading: `target_parts` splits the address into
         * the boxes this template names, `srun_parts` splits the job line into
         * the three numbers. Neither is parsed here, so the page that SHOWS a
         * walltime and the route that STORES one cannot disagree.
         */
        function recipeForm(recipe, saved) {
            view = "form";
            drawnActions = null;
            parts.title.textContent = saved ? "Edit “" + saved.name + "”"
                                            : recipe.label;
            parts.subtitle.textContent = recipe.blurb;
            parts.body.replaceChildren();

            const form = el("div", "connect-form");
            const boxes = {};
            const { field, switchField, choiceField, portsField }
                = formFields(boxes);
            const address = (saved && saved.target_parts) || {};
            const job = (saved && saved.srun_parts) || {};

            // The three job numbers arrive FILLED IN, not as grey placeholder
            // text over an empty box. A default nobody can see is a default
            // nobody can correct, and on a multiplexed image these three are
            // the difference between an import that finishes and one the
            // scheduler kills partway through: a 40-channel pyramid is tens of
            // gigabytes before anything is drawn.
            //
            // On an edit the three job numbers come off the saved line rather
            // than off the defaults, and an empty one stays empty: a profile
            // with no `-t` is somebody who said "whatever the site does", and
            // filling the box with 4:00:00 would answer that question for them.
            const fields = [
                ["name", "Name this connection",
                 "A short name you will recognise",
                 saved ? saved.name : recipe.id],
                ["user", "Your username on that machine", "",
                 address.user || ""],
                ["host", "Address", "login.cluster.edu", address.host || ""],
                ["walltime", "How long to keep it (walltime)",
                 recipeDefaults.walltime,
                 saved ? (job.walltime || "") : recipeDefaults.walltime],
                ["cores", "CPU cores", recipeDefaults.cores,
                 saved ? (job.cores || "") : recipeDefaults.cores],
                ["memory", "Memory", recipeDefaults.memory,
                 saved ? (job.memory || "") : recipeDefaults.memory],
            ];
            fields.forEach(([key, label, placeholder, initial]) => {
                if (key !== "name" && recipe.ask.indexOf(key) < 0) return;
                form.append(field(key, label, placeholder, initial));
            });
            // One more question, for the one preset whose answer can be
            // Windows: which operating system is over there. It decides how
            // every command line is quoted, which shell reads the install
            // chain, and whether the connection asks for a terminal at all --
            // none of which can be worked out from an address.
            //
            // Through the `ask` vocabulary rather than through a `flow`,
            // because a flow means "draw nothing standard at all" and this
            // wants the two boxes above plus this. The choices and the
            // sentence under each one ride down with the recipe.
            const extra = recipe.extra || {};
            if (recipe.ask.indexOf("os") >= 0 && extra.os_choices) {
                form.append(choiceField(
                    "os", "What kind of machine is it?",
                    extra.os_choices.map((choice) => ({
                        value: choice.name,
                        label: choice.label,
                        hint: choice.hint,
                    })),
                    (saved && saved.workstation && saved.workstation.os)
                        || extra.default_os).wrap);
            }
            // Where the data sits over there -- the one question about the far
            // machine that no preset can answer and every one of them needs. In
            // the body rather than behind Advanced because it is the third
            // thing somebody adding a workstation knows: what to call it, where
            // it is, and where their images are on it.
            form.append(field(
                "data_dir", "Remote data directory (optional)",
                "/path/to/data", saved ? (saved.data_dir || "") : "",
                "Default location for browsing data on this server."));
            parts.body.append(form);

            if (recipe.notes && recipe.notes.length) {
                const notes = el("ul", "connect-notes");
                recipe.notes.forEach(
                    (note) => notes.append(el("li", null, note)));
                parts.body.append(notes);
            }

            // The same escape hatch the Settings form has, in the same words
            // and shut by default. A preset is a starting point and never a
            // lock -- but until this was here, correcting one meant saving it,
            // leaving the dialog, finding the server on another page and
            // editing it there, which is not an escape hatch but a detour.
            //
            // The job-options box holds this site's options MINUS the three
            // above it (the server's Recipe.srun_extra), so the two can never
            // contradict each other: a walltime box reading 4:00:00 above a
            // line reading `-t 8:00:00` would be two answers to one question,
            // and only one of them would be the one that ran.
            const advanced = el("details", "connect-advanced");
            advanced.append(el("summary", "connect-advanced-summary",
                               "Advanced — job options, launch command, ports"));
            const advancedForm = el("div", "connect-form");
            const scheduler = recipe.srun !== null && recipe.srun !== undefined;
            if (scheduler) {
                advancedForm.append(field(
                    "srun", "Other job options", "-p interactive",
                    saved ? (job.extra || "") : recipe.srun_extra,
                    "Passed to srun as written. The walltime, cores and "
                    + "memory above are added to this line."));
            }
            advancedForm.append(field(
                "remote_command", "Plexora command or environment",
                "plexora",
                saved ? saved.remote_command : recipe.remote_command,
                "Set this if a login over SSH cannot find plexora — by a wide "
                + "margin the commonest reason a connection fails. An "
                + "environment path is enough."));
            // Next to the field that names the environment, because that is
            // the environment it writes to. Off on arrival for every preset:
            // no starting point gets to decide that software should be
            // installed into somebody's account on a machine it has only read
            // the documentation for.
            advancedForm.append(switchField(
                "install", "Install or update Plexora",
                "Runs pip install --upgrade plexora in that environment "
                + "before launching, and shows it in the connection log.",
                saved ? saved.install : false));
            // Only where there is a job to bind to. Whether the second hop
            // into the compute node works is a fact about the site and the
            // preset carries it -- but a site that allows it for one account
            // and not another is real, and the preset cannot be right about
            // both.
            if (scheduler) {
                advancedForm.append(switchField(
                    "bind_node", "Forward from the login node",
                    "Tunnel to the compute node from the login node instead "
                    + "of connecting to it directly. Needed where the site "
                    + "refuses the second hop.",
                    saved ? saved.bind_node : recipe.bind_node));
            }
            advancedForm.append(portsField(
                "forwards", "Additional port forwarding",
                "Only needed when another service must be reachable through "
                + "the SSH connection.",
                (saved && saved.forwards) || []));
            advanced.append(advancedForm);
            // Open on an edit that has something in here to see. Shut is the
            // right default for a preset, whose whole claim is that these are
            // already answered; on a profile that has an install switched on
            // or a forwarded port, shut hides the answer somebody came to
            // change.
            advanced.open = Boolean(saved && (
                saved.install
                || (saved.forwards && saved.forwards.length)
                || (job.extra || "")
                || (saved.remote_command && saved.remote_command !== "plexora")
            ));
            parts.body.append(advanced);

            parts.body.append(errorSlot());

            // No Back on an edit: this form was not reached through the
            // catalogue and there is nothing behind it -- the machine is
            // already saved, and the list is a page away rather than a screen
            // back.
            parts.actions.replaceChildren.apply(parts.actions, [].concat(
                saved ? [] : [button("btn btn-secondary", "Back", addServer)],
                [el("div", "connect-modal-spacer"),
                 button("btn btn-outline-light", "Cancel", () => close(null)),
                 button("btn btn-primary",
                        saved ? "Save changes" : "Save and connect",
                        () => submitRecipe(recipe, boxes, saved))]));
            if (!saved && boxes.user && boxes.user.focus) {
                setTimeout(() => boxes.user.focus(), 0);
            }
        }

        /** Every control's answer, in the one shape the server reads. */
        function collect(boxes) {
            const answers = {};
            Object.keys(boxes).forEach((key) => {
                const value = boxes[key].value;
                answers[key] = typeof value === "string" ? value.trim() : value;
            });
            return answers;
        }

        async function submitRecipe(recipe, boxes, editing) {
            try {
                const answer = await saveRecipe(recipe.id, collect(boxes));
                name = answer.remote.name;
                await Remotes().refresh();
                if (editing) {
                    // Editing a profile is not asking to open a file on it,
                    // and a machine that was connected when this form opened
                    // is still connected now -- reconnecting it because a data
                    // directory changed would be answering a question nobody
                    // asked.
                    close(null);
                    return;
                }
                // Straight into connecting: somebody who has just described a
                // machine in order to read a file on it has not asked to be
                // returned to a list.
                begin();
            } catch (e) {
                showError(e.message);
            }
        }

        // -- the Google Cloud form ---------------------------------------------
        //
        // The one preset whose questions are not about a machine somebody
        // already has. Its order is deliberate and is the order the answers
        // depend on each other in: **Identity, then Project, then Data, then
        // Compute.** You cannot list projects until you know who is asking;
        // you cannot list buckets until you know the project; and the region
        // and the size of the VM are decided by where the data turned out to
        // be. Asking for the machine first -- which is how a cloud console
        // usually asks -- would mean choosing a region before knowing which
        // one the data is in, which is the mistake that costs money in egress.

        //: Where the last project is remembered, so somebody with fourteen of
        //: them does not scroll to the same one every time. A convenience per
        //: browser and nothing more: it is re-checked against the real list
        //: before it is used, and a stale value simply loses.
        const PROJECT_KEY = "plexora.gcloud.project";

        function rememberedProject() {
            try {
                return window.localStorage.getItem(PROJECT_KEY) || "";
            } catch (e) {
                return "";
            }
        }

        function rememberProject(value) {
            try {
                window.localStorage.setItem(PROJECT_KEY, value || "");
            } catch (e) {
                // A private window, or site data switched off. Forgetting the
                // last project is not a reason to fail a connection.
            }
        }

        async function gcloudAsk(path) {
            const response = await fetch(plexoraUrl(path));
            let payload = {};
            try {
                payload = await response.json();
            } catch (e) {
                payload = {};
            }
            if (!response.ok) {
                throw new Error(payload.error || "Google Cloud did not answer.");
            }
            return payload;
        }

        //: The four questions, in the order the answers depend on each other,
        //: one page at a time. Identity and project first, because nothing can
        //: be listed until they are answered; then the data; then a machine to
        //: read it with, in the region the data turned out to be in; then what
        //: to do with that machine afterwards, which is the setting that
        //: decides what a session costs after it has ended.
        //:
        //: Pages rather than one long form because the form had grown to
        //: nineteen controls, six of which only mean anything for one of the
        //: two kinds of VM, and an Advanced panel holding the single most
        //: consequential question on it. Splitting it is not decoration: a
        //: question nobody scrolls to is a question answered by its default.
        const GCLOUD_PAGES = [
            { chip: "Google Cloud",
              title: "Connect to Google Cloud",
              subtitle: "Plexora signs in through the Google Cloud CLI on this "
                        + "computer. It never sees your password." },
            { chip: "Data",
              title: "Where is your data?",
              subtitle: "A bucket is mounted on the VM with gcsfuse and "
                        + "becomes Plexora's data directory for the session." },
            { chip: "Compute",
              title: "Initialize a virtual machine",
              subtitle: "Something to read that bucket with — a machine "
                        + "Plexora starts for you, or one you already run." },
            { chip: "When Plexora exits",
              title: "What should happen when Plexora exits?",
              subtitle: "Choose what happens to the VM once you disconnect." },
        ];

        //: How a Compute Engine status reads in the VM picker. Google's own
        //: words are shouted constants; a list somebody is choosing from
        //: should read like a sentence.
        const VM_STATUS_WORDS = {
            RUNNING: "Running", TERMINATED: "Stopped", STOPPED: "Stopped",
            SUSPENDED: "Suspended", STAGING: "Starting",
            PROVISIONING: "Starting", STOPPING: "Stopping",
            REPAIRING: "Repairing",
        };

        function gcloudForm(recipe, saved) {
            view = "form";
            drawnActions = null;
            parts.title.textContent = saved ? "Edit “" + saved.name + "”"
                                            : "Add a server";
            parts.subtitle.textContent = recipe.blurb;
            parts.body.replaceChildren();

            const catalogue = recipe.extra || {};
            //: The machine this profile already describes, or nothing at all
            //: when the machine does not exist yet. Editing fills the same
            //: four pages in from here: the questions are the same questions,
            //: and a second read-only view of them would be a second thing to
            //: keep in step with what `compose` accepts.
            const cloud = (saved && saved.gcloud) || {};
            //: What a box should start out holding. Falls back for a value
            //: that is absent OR empty -- but never for `false` or `0`, which
            //: is why the two switches and the idle timer below spell their
            //: own defaults out instead of coming through here.
            function was(key, fallback) {
                const value = cloud[key];
                return (value === undefined || value === null || value === "")
                    ? fallback : value;
            }
            //: The three answers that arrive from Google rather than from the
            //: markup, held until the list that carries them comes back. Each
            //: is spent once: a later change of project is somebody choosing
            //: again, not the saved profile speaking a second time.
            let seedProject = was("project", "");
            let seedBucket = was("bucket", "");
            let seedZone = was("zone", "");
            let seedVm = cloud.vm_source === "existing"
                ? was("vm_name", "") : "";
            //: Whether the bucket's region may move the VM's. It may not on
            //: the first check of an edit -- the saved region is an answer
            //: somebody gave, and a bucket that has not changed is no reason
            //: to overrule it. Every check after that follows the data again.
            let followRegion = !saved;
            const boxes = {};
            const { field, selectField, switchField, derived, pickField,
                    choiceField } = formFields(boxes);

            //: What the form has learned, as opposed to what it was told. Read
            //: through `derived` below, so the submit handler never has to
            //: know which answers came from a box.
            let signedIn = "";
            let bucketLocation = "";
            //: Whether the bucket named right now is one this account can
            //: actually read. **The connection cannot be started until it is.**
            //: There is no "continue without a bucket": the bucket IS the
            //: reason the VM is being asked for, and a connection without one
            //: would start a machine, bill somebody for it, and open a viewer
            //: onto an empty directory.
            let bucketOk = false;
            //: Cancellation for the lookups that race with typing. The idiom is
            //: the codebase's: take a ticket, and drop the answer if a later
            //: request has taken one since.
            let bucketToken = 0;
            // Listing the buckets and checking one are two requests with two
            // lifetimes, and they need two tokens. Sharing one meant that
            // changing project -- which starts a list and then re-checks the
            // name still in the field -- had the check cancel the list it had
            // just asked for, leaving the previous project's buckets on
            // screen or none at all.
            let bucketListToken = 0;
            let projectToken = 0;
            let instanceToken = 0;
            //: The VMs this project already has, by name, so that choosing one
            //: can take its zone. Empty until the bring-your-own field is
            //: shown, because nothing else on the form needs it.
            let known = {};
            //: The region the current bucket implies, or "" when there is no
            //: single one -- a multi-region bucket genuinely has no region to
            //: match, and warning about it would be warning about nothing.
            let bucketRegion = "";
            //: Which page is on screen, and the furthest one reached. The
            //: second is what makes the strip at the top navigable: a step
            //: already passed is a step you may go back to, and one you have
            //: not reached yet is not a shortcut past the questions before it.
            let at = 0;
            let seen = 0;

            derived("account", () => signedIn);
            derived("bucket_location", () => bucketLocation);

            // -- the strip, and the four pages under it ----------------------

            const strip = el("div", "connect-wizard");
            parts.body.append(strip);
            const chips = GCLOUD_PAGES.map((page, index) => {
                const chip = button("connect-wizard-step", page.chip,
                                    () => goTo(index));
                strip.append(chip);
                return chip;
            });

            const pages = GCLOUD_PAGES.map(() => {
                const page = el("div", "connect-wizard-page");
                page.hidden = true;
                parts.body.append(page);
                return page;
            });

            // 1. Identity, then what to call this and which project it is in.
            //    Nothing below can be filled in until the first is answered,
            //    which is why it is a page of its own rather than a row above
            //    everything it decides.
            const identity = el("div", "connect-gcloud-identity");
            pages[0].append(identity);
            const cloudForm = el("div", "connect-form");
            pages[0].append(cloudForm);
            cloudForm.append(field("name", "Name this connection",
                                   "A short name you will recognise",
                                   saved ? saved.name : recipe.id));
            const projectWrap = selectField(
                "project", "Google Cloud project",
                [{ value: "", label: "Sign in to see your projects" }], "",
                "Where the VM is created and billed. A project with billing "
                + "enabled is required before Google will start one.");
            cloudForm.append(projectWrap);
            const projectSelect = boxes.project;

            // 2. The data. Picked from the project's own buckets, because by
            //    the time this page is reachable the account and the project
            //    are known and the list is a fact -- asking somebody to recall
            //    a name Plexora is already holding is asking them to do the
            //    lookup twice. Typing stays available for the bucket the list
            //    could not cover, which includes every public one: listing a
            //    project's buckets and reading somebody else's published atlas
            //    are different permissions.
            const dataForm = el("div", "connect-form");
            pages[1].append(dataForm);
            const bucketPick = pickField(
                "bucket", "Cloud Storage bucket", "gs:// bucket name",
                "Required. This bucket is mounted on the VM and becomes "
                + "Plexora's data directory. A public bucket can be typed in "
                + "even when it is not in this project.",
                { choose: "Choose a bucket…",
                  other: "Another bucket — type its name…" });
            dataForm.append(bucketPick.wrap);
            dataForm.append(field(
                "mount_path", "Mount location inside the VM",
                catalogue.mount_path || "~/plexora-data",
                was("mount_path", catalogue.mount_path || "~/plexora-data"),
                "Where Plexora accesses the data. Defaults to your home "
                + "directory, so no sudo is required."));

            // 3. Compute. Whose machine it is comes first, because it changes
            //    what every other question on the page means -- and it is a
            //    pair of radio buttons rather than a dropdown in Advanced,
            //    which is where it used to be. The choice between renting a
            //    machine and pointing at one is not an advanced detail; it is
            //    the question the rest of the page is about.
            const computeForm = el("div", "connect-form");
            pages[2].append(computeForm);
            const sourceChoice = choiceField(
                "vm_source", "The VM",
                (catalogue.vm_sources || []).map((entry) => ({
                    value: entry.name, label: entry.label, hint: entry.hint,
                })), was("vm_source", "plexora"));
            computeForm.append(sourceChoice.wrap);

            // A picker rather than a plain dropdown, because the curated list
            // is a shortlist and always will be: Compute Engine has hundreds
            // of types, and the ones missing here -- GPU machines, C3,
            // `custom-4-8192` -- are exactly the ones somebody who wants them
            // already knows the name of.
            const machinePick = pickField(
                "machine_type", "Machine type", "c3-highmem-22", "",
                { choose: "", other: "Custom — type a machine type…" });
            machinePick.fill((catalogue.machine_types || []).map(
                (entry) => ({ value: entry.name, label: entry.label })), "",
                             catalogue.default_machine_type || "");
            // After the fill, which is what puts the curated rows there. A
            // machine type the shortlist never named -- a GPU type, a C3, a
            // `custom-4-8192` -- comes back as the typed box it was entered in.
            machinePick.choose(was("machine_type", ""));
            // No reaction of its own -- nothing on this form is gated on the
            // machine type. Wired anyway, because `onPick` is also what swaps
            // the box in when "type its name" is chosen.
            machinePick.onPick(null);
            const machineWrap = machinePick.wrap;
            computeForm.append(machineWrap);

            const spotChoice = choiceField(
                "provisioning_model", "Provisioning",
                (catalogue.provisioning_models || []).map((entry) => ({
                    value: entry.name, label: entry.label, hint: entry.hint,
                })), was("provisioning_model",
                         catalogue.default_provisioning || "spot"));
            computeForm.append(spotChoice.wrap);

            // Picked or typed, exactly like the bucket and for the same
            // reason: the project's machines are known by the time this field
            // matters, and one the list did not cover is still one you can
            // name.
            const vmPick = pickField(
                "vm_name", "Existing VM", "analysis-box",
                "The instance name as it appears in Compute Engine. It must "
                + "already exist — Plexora will not create this one.",
                { choose: "Choose a VM…",
                  other: "Another VM — type its name…" });
            const vmWrap = vmPick.wrap;
            vmWrap.hidden = true;
            computeForm.append(vmWrap);

            // 4. VM location. Filled in from where the bucket turned out to
            //    be, and changeable -- the point of asking about the data
            //    first is that this answer arrives already correct.
            const regionOptions = (catalogue.regions || []).map((entry) => ({
                value: entry.name, label: entry.label,
            }));
            const regionWrap = selectField(
                "region", "VM location", regionOptions,
                was("region", catalogue.default_region || "us-east1"),
                "If your data is stored in GCP, choosing compute in the same "
                + "region can help reduce data access and API-related charges.");
            computeForm.append(regionWrap);
            const regionSelect = boxes.region;
            // No hint of its own while Plexora is renting the machine: the
            // region above is the answer that matters, "Choose automatically"
            // says what happens if this is left alone, and a second sentence
            // here would only repeat the one above it. In existing-VM mode it
            // does get one, because there the control means something else
            // entirely -- see `showVmSource`.
            const zoneWrap = selectField(
                "zone", "Zone", [{ value: "", label: "Choose automatically" }],
                "", "");
            computeForm.append(zoneWrap);
            const zoneSelect = boxes.zone;

            //: Where a machine that already exists is, which is a fact rather
            //: than a setting -- so it is reported instead of asked, and the
            //: two controls above are put away.
            const whereLine = el("div", "connect-gcloud-where");
            whereLine.hidden = true;
            computeForm.append(whereLine);

            const warn = el("div", "connect-gcloud-warn");
            warn.hidden = true;
            computeForm.append(warn);

            const advanced = el("details", "connect-advanced");
            const advancedSummary = el("summary", "connect-advanced-summary",
                                       "Advanced");
            advanced.append(advancedSummary);
            const advancedForm = el("div", "connect-form");
            const diskWrap = field(
                "boot_disk_gb", "Boot disk (GB)",
                String(catalogue.boot_disk_gb || 20),
                String(was("boot_disk_gb", catalogue.boot_disk_gb || 20)), "");
            advancedForm.append(diskWrap);
            // A way out, not a way in — and the hint has to say so, because
            // "public IP address" reads as an invitation to the internet and
            // this is the opposite of one. Plexora writes a firewall rule
            // that refuses everything except Google's tunnel; what the
            // address is for is reaching Google's package repository and
            // PyPI, which a VM with no address and no Cloud NAT cannot do —
            // and a VM that cannot install gcsfuse cannot connect at all.
            const publicIpWrap = switchField(
                "external_ip", "Give VM a public IP address",
                "Plexora blocks inbound access except Google IAP. Disable only "
                + "if Cloud NAT is configured.",
                saved ? cloud.external_ip !== false : true);
            advancedForm.append(publicIpWrap);
            // ON here, unlike every other preset, and the difference is whose
            // machine it is. The reason the switch arrives off elsewhere is
            // that no starting point gets to decide software should be
            // installed into somebody's account on a machine Plexora has only
            // read the documentation for. A VM Plexora rented has no such
            // account and no such doubt: the mount chain already pip-installs
            // on first boot, so leaving this off would mean every LATER
            // connection ran whatever version that first boot happened to
            // get, on a machine whose whole existence is Plexora's doing.
            const installWrap = switchField(
                "install", "Install Plexora", "",
                saved ? Boolean(saved.install) : true);
            advancedForm.append(installWrap);
            const idleWrap = field(
                "idle_shutdown_minutes", "Idle shutdown time (minutes)",
                String(catalogue.idle_shutdown_minutes || 30),
                String(cloud.idle_shutdown_minutes === undefined
                       ? (catalogue.idle_shutdown_minutes || 30)
                       : cloud.idle_shutdown_minutes),
                "The VM switches itself off after this long with nobody "
                + "connected.");
            advancedForm.append(idleWrap);
            const serviceWrap = field(
                "service_account", "Service account (optional)",
                "name@project.iam.gserviceaccount.com",
                was("service_account", ""),
                "Leave empty to use the project's default compute service "
                + "account.");
            advancedForm.append(serviceWrap);
            advancedForm.append(field(
                "remote_command", "Plexora command or environment",
                "~/plexora-venv",
                saved ? saved.remote_command : recipe.remote_command,
                "The environment the VM builds for itself on its first "
                + "connection."));
            advanced.append(advancedForm);
            pages[2].append(advanced);

            // 5. The ending. Three answers that are genuinely different
            //    decisions rather than degrees of one, each with what it costs
            //    underneath — and Delete is refused outright for a machine
            //    Plexora did not make.
            const exitForm = el("div", "connect-form");
            pages[3].append(exitForm);
            const exitChoice = choiceField(
                "on_exit", "When Plexora exits:",
                (catalogue.exit_actions || []).map((entry) => ({
                    value: entry.name, label: entry.label, hint: entry.hint,
                })), was("on_exit", catalogue.default_exit || "stop"));
            exitForm.append(exitChoice.wrap);

            // `recipe.notes` is deliberately NOT drawn here. This page is one
            // question, and it was sharing a screen with seven paragraphs
            // about billing, IAP roles and Spot -- none of which is about the
            // question being asked, and all of which had already been said on
            // the page it applied to. What survives is what the three answers
            // themselves cost, which is the choice rather than a commentary on
            // it, and the "Untested" badge on the preset card, which is where
            // somebody sees it before starting rather than at the end.

            parts.body.append(errorSlot());

            // -- the footer, which is different on every page -----------------

            const back = button("btn btn-secondary", "Back", () => goTo(at - 1));
            const blocked = el("span", "connect-wizard-blocked");
            const cancel = button("btn btn-outline-light", "Cancel",
                                  () => close(null));
            const next = button("btn btn-primary", "Next", () => goTo(at + 1));
            const finish = button("btn btn-primary",
                                  saved ? "Save changes" : "Create & Connect",
                                  () => submitRecipe(recipe, boxes, saved));
            parts.actions.replaceChildren(
                back, el("div", "connect-modal-spacer"), blocked, cancel,
                next, finish);

            /**
             * Move to a page, or out of the form entirely.
             *
             * Nothing is rebuilt: every control on all four pages was made
             * once and only ever hidden, which is the whole of "moving
             * backward does not lose what was entered". A wizard that redrew
             * the page it returns to would have to remember the answers
             * separately from the controls holding them, and the two would
             * disagree the first time a lookup came back late.
             */
            function goTo(index) {
                if (index < 0) return addServer();
                if (index >= pages.length) return;
                // Every page between here and there, not only this one: the
                // strip can jump forward to a page already visited, and a
                // question answered on the way there can be un-answered
                // afterwards.
                for (let one = at; one < index; one += 1) {
                    if (blocker(one)) return;
                }
                at = index;
                if (at > seen) seen = at;
                if (at === 2 && own()) loadInstances();
                paint();
            }

            function own() {
                return boxes.vm_source.value === "existing";
            }

            /**
             * Why this page cannot be left yet, in the words that say what to
             * do about it -- or "" when it can.
             *
             * A disabled button with no reason beside it is the commonest way
             * a form becomes unusable: the control that would explain the
             * refusal is the one that has been switched off. So the sentence
             * is next to the button, and it names the field.
             */
            function blocker(index) {
                if (index === 0) {
                    if (!signedIn) return "Sign in to Google to continue.";
                    if (!projectSelect.value) return "Choose a project.";
                    if (!boxes.name.value.trim()) return "Name this connection.";
                    return "";
                }
                if (index === 1) {
                    if (!bucketOk) {
                        return "Choose a bucket Plexora can read.";
                    }
                    if (!boxes.mount_path.value.trim()) {
                        return "Say where to mount it on the VM.";
                    }
                    return "";
                }
                if (index === 2) {
                    if (own() && !boxes.vm_name.value.trim()) {
                        return "Choose the VM to connect to.";
                    }
                    return "";
                }
                return "";
            }

            /** The first thing still unanswered, anywhere in the form. */
            function firstBlocker() {
                for (let index = 0; index < pages.length; index += 1) {
                    const why = blocker(index);
                    if (why) return why;
                }
                return "";
            }

            function paint() {
                const page = GCLOUD_PAGES[at];
                parts.title.textContent = page.title;
                parts.subtitle.textContent = page.subtitle;
                pages.forEach((one, index) => { one.hidden = index !== at; });
                chips.forEach((chip, index) => {
                    chip.classList.toggle("is-active", index === at);
                    // Every page reached but not the one on screen -- which
                    // includes the ones AHEAD of here after a Back, because
                    // those are answered too and the strip should not
                    // un-answer them just because somebody went back to check
                    // something.
                    chip.classList.toggle("is-done",
                                          index !== at && index <= seen);
                    // Forward is through the questions, never around them.
                    chip.disabled = index > seen;
                });
                const last = at === pages.length - 1;
                next.hidden = last;
                finish.hidden = !last;
                const why = last ? firstBlocker() : blocker(at);
                next.disabled = Boolean(why);
                finish.disabled = Boolean(why);
                blocked.textContent = why;
                blocked.hidden = !why;
                // The button says what will actually happen. "Create" would be
                // a lie about a machine that is already running, and this is
                // the button somebody presses to spend money.
                finish.textContent = own() ? "Connect to VM"
                                           : "Create & Connect";
            }

            // -- what the two kinds of machine change -------------------------

            /**
             * Show the questions this kind of VM actually raises.
             *
             * A machine somebody already runs takes no size, no disk, no
             * network and no shutdown timer from this form -- not because
             * those questions are hard, but because none of them is Plexora's
             * to decide about somebody else's server. Hiding them is the
             * honest version of the same thing: a form that offered a boot
             * disk size for an instance that already has one would be
             * describing something it is not going to do.
             */
            function showVmSource() {
                const mine = own();
                vmWrap.hidden = !mine;
                [machineWrap, spotChoice.wrap, diskWrap, publicIpWrap,
                 idleWrap, serviceWrap, regionWrap]
                    .forEach((wrap) => { wrap.hidden = mine; });
                // The zone stays, and it is the one control on this page that
                // means something different in each mode. For a rented VM it
                // is a preference inside the bucket's region. For a machine
                // that already exists it is a FACT, filled in from the one
                // that was picked -- and left editable for the case that has
                // no other way out: a VM this account cannot list, whose zone
                // Plexora therefore cannot look up by name.
                if (zoneSelect.plexoraHint) {
                    zoneSelect.plexoraHint.textContent = mine
                        ? "Filled in from the VM you chose. Only worth setting "
                          + "by hand if Plexora cannot find your VM by name."
                        : "";
                    zoneSelect.plexoraHint.hidden = !mine;
                }
                whereLine.hidden = !mine;
                advancedSummary.textContent = mine
                    ? "Advanced — install Plexora, launch command"
                    : "Advanced — disk, network, idle shutdown, launch command";
                // Deleting a machine Plexora did not create is not something
                // this form will offer at any price. The server refuses it
                // twice more -- on the saved record, and on the label written
                // to the instance itself -- but a row that is only ever going
                // to be refused should not be pressable here either.
                exitChoice.disable(mine ? ["delete"] : [],
                                   "Plexora will not delete a VM it did not "
                                   + "create. Remove it in the Google Cloud "
                                   + "console if you want it gone.");
                if (mine) {
                    loadInstances();
                    useInstanceZone();
                }
                showWarning(bucketRegion);
                paint();
            }
            sourceChoice.onPick(showVmSource);
            // On change rather than on every keystroke: this is what moves the
            // zone and the region under the user, and doing that halfway
            // through typing a name would be doing it to a name they have not
            // finished. The footer, though, has to follow the keystrokes.
            vmPick.onPick(() => { useInstanceZone(); paint(); });
            vmPick.input.oninput = paint;
            boxes.name.oninput = paint;
            boxes.mount_path.oninput = paint;

            function setControlsEnabled(enabled) {
                [projectSelect, boxes.bucket].forEach((control) => {
                    control.disabled = !enabled;
                });
                paint();
            }

            function showWarning(region) {
                const chosen = regionSelect.value;
                if (!region || !chosen || chosen === region) {
                    warn.hidden = true;
                    return;
                }
                warn.replaceChildren();
                // A VM that already exists is somewhere, and no button here
                // can move it. Saying "would run" about a running machine, and
                // offering to relocate it, would both be lies -- so the same
                // fact is reported in the tense that is true, and the offered
                // fix is the only one that exists: the data, not the machine.
                const mine = own();
                warn.append(el("span", null,
                               "Your data is in " + region + " and "
                               + (mine ? "this VM runs in " : "this VM would "
                                  + "run in ") + chosen + ". Reads will "
                               + "be slower, and Google charges for data "
                               + "leaving a region."));
                if (!mine) {
                    warn.append(button("btn btn-secondary", "Use bucket region",
                                       () => {
                                           regionSelect.value = region;
                                           loadZones();
                                           showWarning(region);
                                       }));
                }
                warn.hidden = false;
            }

            // -- what the form does once it is on screen ---------------------

            async function loadStatus() {
                identity.replaceChildren();
                identity.append(el("span", "connect-gcloud-identity-text",
                                   "Checking Google Cloud…"));
                let status = {};
                try {
                    status = await gcloudAsk("settings/gcloud/status");
                } catch (e) {
                    status = {};
                }
                identity.replaceChildren();
                if (!status.installed) {
                    signedIn = "";
                    identity.append(el(
                        "span", "connect-gcloud-identity-text",
                        "The Google Cloud CLI was not detected. Install it "
                        + "from cloud.google.com/cli and then return to "
                        + "Plexora."));
                    setControlsEnabled(false);
                    return;
                }
                if (!status.account) {
                    signedIn = "";
                    identity.append(el(
                        "span", "connect-gcloud-identity-text",
                        "Plexora uses the Google Cloud CLI installed on this "
                        + "computer to authenticate and connect to Google "
                        + "Cloud. Your password is never seen by Plexora — the "
                        + "sign-in happens in your browser and the credential "
                        + "stays in gcloud."));
                    identity.append(button("btn btn-primary",
                                           "Sign in with Google", signIn));
                    setControlsEnabled(false);
                    return;
                }
                signedIn = status.account;
                identity.append(el("span", "connect-gcloud-identity-text",
                                   "Signed in as " + signedIn + "."));
                identity.append(button("btn btn-secondary", "Use another "
                                       + "account", signIn));
                setControlsEnabled(true);
                loadProjects();
            }

            async function signIn() {
                identity.replaceChildren();
                identity.append(el(
                    "span", "connect-gcloud-identity-text",
                    "A browser window has opened for Google's sign-in. Finish "
                    + "it there and this will catch up."));
                try {
                    await fetch(plexoraUrl("settings/gcloud/auth"),
                                { method: "POST" });
                } catch (e) {
                    showError("Could not start the Google sign-in.");
                    return loadStatus();
                }
                // Polled rather than awaited: what happens next is a person
                // reading a consent screen, and nothing here can know how long
                // that takes. Two minutes, then the form says so instead of
                // waiting forever with no way back.
                for (let tries = 0; tries < 60; tries += 1) {
                    await new Promise((done) => setTimeout(done, 2000));
                    let status = {};
                    try {
                        status = await gcloudAsk("settings/gcloud/status");
                    } catch (e) {
                        status = {};
                    }
                    if (status.account) return loadStatus();
                    if (!openDialog) return;
                }
                loadStatus();
            }

            async function loadProjects() {
                const mine = ++projectToken;
                projectSelect.setOptions(
                    [{ value: "", label: "Loading projects…" }]);
                projectSelect.value = "";
                let payload = {};
                try {
                    payload = await gcloudAsk("settings/gcloud/projects");
                } catch (e) {
                    if (mine !== projectToken) return;
                    projectSelect.setOptions([]);
                    showError(e.message);
                    return;
                }
                if (mine !== projectToken) return;
                const found = payload.projects || [];
                if (!found.length) {
                    projectSelect.setOptions(
                        [{ value: "", label: "No projects on this account" }]);
                    projectSelect.value = "";
                    paint();
                    return;
                }
                projectSelect.setOptions(found.map((entry) => ({
                    value: entry.id,
                    label: entry.name === entry.id
                        ? entry.id : entry.name + " (" + entry.id + ")",
                })));
                // Said outright rather than left to the browser's "the first
                // option is selected": everything below reads this value, and
                // a form whose project is implicit is a form where one wrong
                // assumption means an empty bucket list and no reason given.
                // The profile's own project first, then the remembered one:
                // a form filled in from a saved machine is not a fresh start,
                // and the machine is in the project it is in.
                const remembered = seedProject || rememberedProject();
                seedProject = "";
                projectSelect.value =
                    (remembered && found.some((e) => e.id === remembered))
                        ? remembered : found[0].id;
                paint();
                loadBuckets();
            }

            async function loadBuckets() {
                const project = projectSelect.value;
                if (!project) return;
                rememberProject(project);
                const mine = ++bucketListToken;
                let payload = {};
                try {
                    payload = await gcloudAsk(
                        "settings/gcloud/buckets?project="
                        + encodeURIComponent(project));
                } catch (e) {
                    if (mine !== bucketListToken) return;
                    showError(e.message);
                    // Listing failed; the field must not. Naming a bucket is
                    // still a complete answer, and the check below is what
                    // decides whether it is a real one.
                    bucketPick.fill([], "Plexora could not list this project's "
                                    + "buckets. Type the name of the one to "
                                    + "mount.");
                    return;
                }
                if (mine !== bucketListToken) return;
                bucketPick.fill(
                    (payload.buckets || []).map((entry) => ({
                        value: entry.name,
                        // The location rides beside the name because it is
                        // what the next question is about -- compute belongs
                        // where the data already is, and reading it here means
                        // not going to the console to find out.
                        label: entry.location
                            ? entry.name + " — " + entry.location : entry.name,
                    })),
                    "No buckets in this project. Type the name of one you can "
                    + "reach, or choose a different project.");
                // After the fill, never before it. `fill` puts the field back
                // to "Choose a bucket…", and a check run against the name the
                // previous project left behind would leave the button enabled
                // for a bucket the form is no longer showing.
                // Before the check and after the fill: `fill` puts the field
                // back to "Choose a bucket…", so a value restored ahead of it
                // would be wiped, and a check run ahead of the value would
                // check the empty field.
                if (seedBucket) {
                    bucketPick.choose(seedBucket);
                    seedBucket = "";
                }
                checkBucket();
                loadZones();
            }

            async function checkBucket() {
                const project = projectSelect.value;
                const bucket = boxes.bucket.value.trim();
                bucketOk = false;
                bucketRegion = "";
                bucketLocation = "";
                paint();
                if (!project || !bucket) {
                    warn.hidden = true;
                    return;
                }
                const mine = ++bucketToken;
                let payload = {};
                try {
                    payload = await gcloudAsk(
                        "settings/gcloud/bucket?project="
                        + encodeURIComponent(project)
                        + "&name=" + encodeURIComponent(bucket));
                } catch (e) {
                    if (mine !== bucketToken) return;
                    showError(e.message);
                    return;
                }
                if (mine !== bucketToken) return;
                showError("");
                const found = payload.bucket || {};
                bucketOk = true;
                bucketLocation = found.location || "";
                bucketRegion = found.exact ? (found.region || "") : "";
                if (bucketPick.note) {
                    // Said on the bucket's own field, because it is a fact
                    // about the bucket: a world-readable one grants objects
                    // and not metadata, so Plexora can read the data and
                    // genuinely cannot see where it lives.
                    bucketPick.note.textContent = found.public
                        ? "Readable, but not by this account's own rights — so "
                          + "Plexora cannot read where it lives. Choose the "
                          + "region yourself on the next page."
                        : (bucketLocation
                           ? "gs://" + found.name + " — " + bucketLocation
                           : "gs://" + found.name);
                    bucketPick.note.hidden = false;
                }
                // The region follows the data, automatically, the moment the
                // bucket is known -- which is the whole reason the form asks
                // in this order.
                if (found.region && !found.public && followRegion) {
                    // A region the curated list has never heard of is still
                    // where the data is. Adding it beats showing a control
                    // that disagrees with the sentence underneath it.
                    if (!regionSelect.has(found.region)) {
                        regionSelect.setOptions(regionSelect.options.concat(
                            [{ value: found.region, label: found.region }]));
                    }
                    regionSelect.value = found.region;
                    if (regionSelect.plexoraHint) {
                        regionSelect.plexoraHint.textContent = found.exact
                            ? bucketLocation + " · detected from your bucket"
                            : bucketLocation + " is a multi-region — "
                              + found.region + " is the closest single region.";
                        regionSelect.plexoraHint.hidden = false;
                    }
                    loadZones();
                }
                // Whatever this check decided, the next one follows the data
                // again: a bucket somebody has just changed is a new answer,
                // not the saved one being overruled.
                followRegion = true;
                showWarning(bucketRegion);
                paint();
            }

            async function loadZones() {
                const project = projectSelect.value;
                const region = regionSelect.value;
                if (!project || !region) return;
                let payload = {};
                try {
                    payload = await gcloudAsk(
                        "settings/gcloud/zones?project="
                        + encodeURIComponent(project)
                        + "&region=" + encodeURIComponent(region));
                } catch (e) {
                    return;
                }
                zoneSelect.setOptions(
                    [{ value: "", label: "Choose automatically" }].concat(
                        (payload.zones || []).map(
                            (zone) => ({ value: zone, label: zone }))));
                // "Choose automatically" -- and the server resolves it, so an
                // empty answer here is a real one rather than a missing field.
                zoneSelect.value = payload.pick || "";
                if (seedZone) {
                    // A zone Google did not list is still where the machine
                    // is. Adding it beats a control that disagrees with the
                    // profile it was drawn from.
                    if (!zoneSelect.has(seedZone)) {
                        zoneSelect.setOptions(zoneSelect.options.concat(
                            [{ value: seedZone, label: seedZone }]));
                    }
                    zoneSelect.value = seedZone;
                    seedZone = "";
                }
            }

            async function loadInstances() {
                // A convenience, never a gate: the field it fills also takes a
                // typed name, and a VM this list did not cover works the same.
                // A project without permission to list instances is silence
                // rather than an error for exactly that reason.
                //
                // The WHOLE project, deliberately not the zone chosen for the
                // data. Where somebody's own machine lives has nothing to do
                // with where their bucket is, and filtering by the bucket's
                // zone hid exactly the machines this field exists to find.
                const project = projectSelect.value;
                if (!project || !boxes.vm_name) return;
                const mine = ++instanceToken;
                let payload = {};
                try {
                    payload = await gcloudAsk(
                        "settings/gcloud/instances?project="
                        + encodeURIComponent(project));
                } catch (e) {
                    return;
                }
                if (mine !== instanceToken) return;
                known = {};
                vmPick.fill(
                    (payload.instances || []).map((one) => {
                        known[one.name] = one;
                        // Everything choosing between two machines turns on:
                        // how big it is, where it is -- which decides the
                        // region below -- and whether Plexora will have to
                        // start it first.
                        const parts = [one.name];
                        if (one.machine_type) parts.push(one.machine_type);
                        if (one.zone) parts.push(one.zone);
                        const word = VM_STATUS_WORDS[one.status]
                            || one.status || "";
                        if (word) parts.push(word);
                        return { value: one.name, label: parts.join(" — ") };
                    }),
                    "No VMs in this project. Type the name of the one to use.");
                // The machine the profile already points at, restored after
                // the fill that would otherwise have cleared the field.
                if (seedVm) {
                    vmPick.choose(seedVm);
                    seedVm = "";
                }
                useInstanceZone();
            }

            /**
             * Take the chosen VM's own zone as the truth, and the region with it.
             *
             * The rest of this form reasons from the data outwards: the bucket
             * decides the region, the region decides the zone. A machine that
             * already exists inverts that -- it is somewhere, that somewhere is
             * a fact, and the form's job is to notice rather than to argue.
             * Being far from the data is real and costs egress, so the
             * mismatch warning still fires; it is information, not a refusal.
             */
            function useInstanceZone() {
                const chosen = (boxes.vm_name.value || "").trim();
                const found = known[chosen];
                if (!found || !found.zone) {
                    whereLine.textContent = chosen
                        ? "Plexora will look up where “" + chosen + "” runs."
                        : "";
                    whereLine.hidden = !whereLine.textContent || !own();
                    // And it can only look it up if this end stops asserting
                    // an answer. The zone in the control is the bucket's --
                    // chosen for a machine Plexora would have created -- and
                    // sending it for a machine somebody typed the name of
                    // would describe an instance in a zone it is not in, and
                    // fail with "there is no VM called that" about a VM that
                    // exists.
                    if (own() && chosen) zoneSelect.value = "";
                    return;
                }
                const region = found.zone.replace(/-[a-z]$/, "");
                if (!zoneSelect.has(found.zone)) {
                    zoneSelect.setOptions(zoneSelect.options.concat(
                        [{ value: found.zone, label: found.zone }]));
                }
                zoneSelect.value = found.zone;
                if (!regionSelect.has(region)) {
                    regionSelect.setOptions(regionSelect.options.concat(
                        [{ value: region, label: region }]));
                }
                regionSelect.value = region;
                whereLine.textContent = "This VM runs in " + found.zone
                    + (found.machine_type ? " · " + found.machine_type : "")
                    + ". Plexora connects to it where it is.";
                whereLine.hidden = !own();
                showWarning(bucketRegion);
            }

            // Assigned rather than added, so a redraw of this form cannot
            // stack a second listener on the same control -- the same reason
            // the data-source field does it this way.
            projectSelect.onchange = () => {
                // Shut the gate at once, and let the reload decide when to
                // open it again -- `loadBuckets` re-checks once the new
                // project's list has actually replaced the old one.
                bucketOk = false;
                paint();
                loadBuckets();
                // The machines belong to the project too, so a different
                // project is a different list -- and leaving the old one up
                // would offer VMs that are not in scope any more.
                if (own()) loadInstances();
            };
            bucketPick.onPick(checkBucket);
            regionSelect.onchange = () => {
                showWarning(bucketRegion);
                loadZones();
            };

            setControlsEnabled(false);
            showVmSource();
            // Every page reachable from the start when the form was filled in
            // from a saved machine: none of its questions is unanswered, so
            // the strip is a way of getting to the one being changed rather
            // than a sequence to be walked again.
            if (saved) seen = pages.length - 1;
            goTo(0);
            loadStatus();
        }


        async function saveRecipe(id, answers) {
            const response = await fetch(
                plexoraUrl("settings/recipes/" + encodeURIComponent(id)), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(answers),
                });
            let payload = {};
            try {
                payload = await response.json();
            } catch (e) {
                payload = {};
            }
            if (!response.ok) {
                throw new Error(payload.error || "That server could not be saved.");
            }
            return payload;
        }

        // -- wiring ------------------------------------------------------------

        function update(snapshot) {
            if (!snapshot.loaded || view !== "auto") return;
            if (name) drawProgress(snapshot);
            else drawChooser(snapshot);
        }

        function watch() {
            if (unwatch) unwatch();
            // `active` and `focus`: this dialog is open in front of somebody
            // AND it is showing one connection's whole log, which the list
            // payload does not carry.
            unwatch = Remotes().subscribe(update, {
                active: true,
                focus: name ? { name: name, kind: kind } : null,
            });
        }

        const promise = new Promise((resolve) => {
            dialog.addEventListener("close", () => {
                if (unwatch) unwatch();
                unwatch = null;
                dialog.remove();
                if (openDialog === dialog) openDialog = null;
                resolve(result);
            });
        });

        // Escape is a plain way out and means "stop showing me this", never
        // "kill the connection" -- same reading as Continue in background.
        dialog.addEventListener("cancel", () => close(null));
        dialog.showModal();
        if (options.view === "recipe" && options.recipe) {
            // Straight to one preset's form: a card on the Settings page has
            // already chosen the preset, and an Edit button there has chosen
            // both the preset and the profile it fills the form in from.
            // Still watching, because saving a NEW machine connects it and
            // the steps have to be there when it does.
            watch();
            // Said before the catalogue arrives, not after: `view` is what
            // stops the once-a-second poll drawing the list of machines over
            // a form somebody is typing into, and until it is set this dialog
            // is still "auto".
            view = "form";
            parts.body.replaceChildren(
                el("p", "connect-modal-empty", "Loading…"));
            loadRecipes().then((all) => {
                const chosen = all.filter(
                    (one) => one.id === options.recipe)[0];
                // A profile naming a preset this server no longer offers. The
                // catalogue is the honest answer -- better than a form drawn
                // out of nothing, and every other preset is one click away.
                if (chosen) openRecipe(chosen, options.remote || null);
                else addServer();
            });
        } else if (options.view === "recipes") {
            // Still watching, because saving a preset connects it and the
            // steps have to be there when it does -- but the first thing on
            // screen is the catalogue, not a list of machines somebody has
            // just told us they do not have.
            watch();
            addServer();
        } else if (name) begin();
        else watch();
        return promise;
    }

    // `recipeGrid` is public because Settings draws the catalogue on the page
    // itself, in the place its own hand-written "add a server" form used to
    // be. One grid, one set of cards, one request.
    return { open, recipeGrid, STEPS, stepStates };
})();

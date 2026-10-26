/**
 * pluginHelp.js - what the `?` in a tool card's header opens.
 *
 * One modal for every plugin, built from the `help` descriptor on its client
 * definition (see pluginRegistry.js): a summary, a few notes, and a table of
 * its keys -- or, for a tool with more to explain than a few notes hold,
 * `sections`: titled parts, each a sentence or two with the points and keys
 * that belong to it, laid out in a wider dialog. toolLoader.js draws the `?`
 * and calls open(); a plugin writes
 * nothing but the descriptor, so help looks, reads and closes the same way in
 * every tool, including third-party ones.
 *
 * THE MODAL IS PlexoraConfirm.tell. That primitive already owns opening,
 * focus, Escape and the backdrop, and it refuses HTML. The table is handed to
 * it as a node built here with textContent, so a plugin's help strings stay
 * text all the way to the screen.
 *
 * THE OPEN/CLOSE ROW IS CORE'S. It is read off the tool's card, which reads it
 * off the Tools menu row, which is what the binding actually is -- a plugin
 * that restated its own chord in its help could print one that had lost a
 * clash and does nothing.
 */
window.PlexoraPluginHelp = (function () {
    "use strict";

    /** One key as printed: a chord through the shortcut service, so it reads
     *  "⌘B" on a Mac and "Ctrl+B" elsewhere; anything else as written, with a
     *  single letter upper-cased the way a keyboard prints it. */
    function printed(key) {
        const text = String(key || "").trim();
        const shortcuts = window.PlexoraShortcuts;
        if (shortcuts && shortcuts.normalize && shortcuts.normalize(text)) {
            return shortcuts.format(text);
        }
        return text.length === 1 ? text.toUpperCase() : text;
    }

    function cap(text) {
        const kbd = document.createElement("kbd");
        kbd.className = "plx-kbd";
        kbd.textContent = text;
        return kbd;
    }

    function row(keys, label) {
        const tr = document.createElement("tr");
        const keyCell = document.createElement("td");
        keyCell.className = "plx-tool-help-keys";
        [].concat(keys).filter((key) => String(key || "").trim())
            .forEach((key) => keyCell.appendChild(cap(printed(key))));
        const labelCell = document.createElement("td");
        labelCell.className = "plx-tool-help-label";
        labelCell.textContent = String(label || "");
        tr.appendChild(keyCell);
        tr.appendChild(labelCell);
        return tr;
    }

    function shortcutTable(entries) {
        const table = document.createElement("table");
        table.className = "plx-tool-help-shortcuts";
        const body = document.createElement("tbody");
        for (const entry of entries) body.appendChild(row(entry.keys, entry.label));
        table.appendChild(body);
        return table;
    }

    function textList(className, items) {
        const list = document.createElement("ul");
        list.className = className;
        for (const text of items) {
            const item = document.createElement("li");
            item.textContent = String(text);
            list.appendChild(item);
        }
        return list;
    }

    /**
     * A plugin's `sections`, read at open rather than at registration: a
     * function is called, so a section can name something that depends on the
     * project on screen (the columns a save name will produce). One that
     * throws is no sections rather than no help.
     */
    function sectionsOf(help) {
        let sections = help && help.sections;
        if (typeof sections === "function") {
            try { sections = sections(); } catch (_error) { sections = null; }
        }
        return (Array.isArray(sections) ? sections : [])
            .filter((section) => section && String(section.title || "").trim());
    }

    /**
     * One titled part of a guide. The icon is a Font Awesome class list and
     * nothing else -- anything with a character outside it is dropped, so a
     * plugin string still cannot reach the page as markup.
     */
    function section({ title, icon, text, points, shortcuts }) {
        const part = document.createElement("section");
        part.className = "plx-tool-help-section";
        const head = document.createElement("h3");
        head.className = "plx-tool-help-section-title";
        if (typeof icon === "string" && /^[a-z0-9 -]+$/.test(icon.trim())) {
            const glyph = document.createElement("span");
            glyph.className = "plx-tool-help-section-icon " + icon.trim();
            glyph.setAttribute("aria-hidden", "true");
            head.appendChild(glyph);
        }
        const name = document.createElement("span");
        name.textContent = String(title);
        head.appendChild(name);
        part.appendChild(head);
        if (String(text || "").trim()) {
            const para = document.createElement("p");
            para.className = "plx-tool-help-section-text";
            para.textContent = String(text);
            part.appendChild(para);
        }
        const items = (Array.isArray(points) ? points : []).filter((p) => String(p || "").trim());
        if (items.length) part.appendChild(textList("plx-tool-help-points", items));
        const keys = (Array.isArray(shortcuts) ? shortcuts : [])
            .filter((entry) => entry && entry.keys && entry.label);
        if (keys.length) part.appendChild(shortcutTable(keys));
        return part;
    }

    function docsLink(docs) {
        // A page of the documentation site, relative to its /docs/ root.
        // Opened the way the Help menu opens the site (the desktop app
        // hands it to the system browser).
        const link = document.createElement("a");
        link.className = "plx-tool-help-docs";
        link.href = window.PlexoraHelpMenu.docsUrl(docs);
        link.target = "_blank";
        link.rel = "noopener";
        link.textContent = "Read the documentation";
        link.addEventListener("click", (event) => {
            event.preventDefault();
            window.PlexoraHelpMenu.openDocs(docs);
        });
        return link;
    }

    /**
     * A guide: the sections in two columns, then one line at the foot for the
     * card's own key and the documentation. The key goes there rather than in
     * a table of one row under the columns, where it would read as a part of
     * whichever section ended last.
     */
    function guide({ notes, sections, shortcuts, openKey, docs, label }) {
        const root = document.createElement("div");
        root.className = "plx-tool-help is-guide";
        if (notes.length) root.appendChild(textList("plx-tool-help-notes", notes));
        const grid = document.createElement("div");
        grid.className = "plx-tool-help-sections";
        for (const each of sections) grid.appendChild(section(each));
        root.appendChild(grid);
        if (shortcuts.length) root.appendChild(shortcutTable(shortcuts));
        if (openKey || docs) {
            const foot = document.createElement("div");
            foot.className = "plx-tool-help-foot";
            if (openKey) {
                const key = document.createElement("span");
                key.className = "plx-tool-help-open";
                key.appendChild(cap(openKey));
                const text = document.createElement("span");
                text.textContent = "Open or close " + (label || "this tool");
                key.appendChild(text);
                foot.appendChild(key);
            }
            if (docs) foot.appendChild(docsLink(docs));
            root.appendChild(foot);
        }
        return root;
    }

    /**
     * The notes and the key table, as one node, or null when there is neither.
     * `openKey` is the already-printed chord from the card ("⌘B"); empty when
     * the tool has none, or its chord lost a clash.
     */
    function content({ help, openKey, label }) {
        const notes = (help && Array.isArray(help.notes) ? help.notes : [])
            .filter((note) => String(note || "").trim());
        const shortcuts = (help && Array.isArray(help.shortcuts) ? help.shortcuts : [])
            .filter((entry) => entry && entry.keys && entry.label);
        const docs = help && typeof help.docs === "string" && help.docs.trim()
            && window.PlexoraHelpMenu?.openDocs ? help.docs.trim() : "";
        const sections = sectionsOf(help);
        if (sections.length) {
            return guide({ notes, sections, shortcuts, openKey, docs, label });
        }
        if (!notes.length && !shortcuts.length && !openKey && !docs) return null;

        const root = document.createElement("div");
        root.className = "plx-tool-help";
        if (notes.length) root.appendChild(textList("plx-tool-help-notes", notes));
        if (shortcuts.length || openKey) {
            const table = shortcutTable(shortcuts);
            const body = table.children[0];
            if (openKey) {
                // Already printed, so it goes in as a cap as it stands.
                const tr = document.createElement("tr");
                const keyCell = document.createElement("td");
                keyCell.className = "plx-tool-help-keys";
                keyCell.appendChild(cap(openKey));
                const labelCell = document.createElement("td");
                labelCell.className = "plx-tool-help-label";
                labelCell.textContent = "Open or close " + (label || "this tool");
                tr.appendChild(keyCell);
                tr.appendChild(labelCell);
                body.appendChild(tr);
            }
            root.appendChild(table);
        }
        if (docs) root.appendChild(docsLink(docs));
        return root;
    }

    /**
     * Open the help for one tool. Resolves when it is closed.
     *
     * Closed by the X in its corner (or Escape, or the backdrop), not by a
     * Close button under it: there is nothing to answer, and at the foot of
     * a guide a button is the last thing on the page to reach.
     */
    function open({ name, label, help, openKey }) {
        if (!help || !help.summary || !window.PlexoraConfirm) return Promise.resolve(null);
        return window.PlexoraConfirm.choose({
            title: label || name,
            body: String(help.summary),
            content: content({ help, openKey, label: label || name }),
            choices: [],
            close: true,
        });
    }

    return { open, _content: content };
})();

/**
 * pluginHelp.js - what the `?` in a tool card's header opens.
 *
 * One modal for every plugin, built from the `help` descriptor on its client
 * definition (see pluginRegistry.js): a summary, a few notes, and a table of
 * its keys. toolLoader.js draws the `?` and calls open(); a plugin writes
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
        if (!notes.length && !shortcuts.length && !openKey) return null;

        const root = document.createElement("div");
        root.className = "plx-tool-help";
        if (notes.length) {
            const list = document.createElement("ul");
            list.className = "plx-tool-help-notes";
            for (const note of notes) {
                const item = document.createElement("li");
                item.textContent = String(note);
                list.appendChild(item);
            }
            root.appendChild(list);
        }
        if (shortcuts.length || openKey) {
            const table = document.createElement("table");
            table.className = "plx-tool-help-shortcuts";
            const body = document.createElement("tbody");
            for (const entry of shortcuts) body.appendChild(row(entry.keys, entry.label));
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
            table.appendChild(body);
            root.appendChild(table);
        }
        return root;
    }

    /** Open the help for one tool. Resolves when it is closed. */
    function open({ name, label, help, openKey }) {
        if (!help || !help.summary || !window.PlexoraConfirm) return Promise.resolve(null);
        return window.PlexoraConfirm.tell({
            title: label || name,
            body: String(help.summary),
            content: content({ help, openKey, label: label || name }),
            confirm: "Close",
        });
    }

    return { open, _content: content };
})();

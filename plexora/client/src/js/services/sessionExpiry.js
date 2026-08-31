/**
 * PlexoraSessionExpiry -- saying so before the job ends, and when it has.
 *
 * A connection that runs inside a scheduler's job has a deadline nobody is
 * looking at. Slurm does not warn anybody; it kills the allocation, the tunnel
 * goes with it, and the first sign is a tile that will not load an hour into a
 * session. The countdown on the Settings card and in the navbar panel is there
 * for somebody who thinks to check. This is for everybody else.
 *
 * **Two moments, and only two.** Ten minutes out, which is enough time to
 * finish a thought and start a fresh session before the current one goes; and
 * at zero, because by then the machine is gone whatever anybody does. Anything
 * between those would be interrupting somebody who has already been told.
 *
 * **Closing it means closed.** A job that has ended does not stop being an
 * ended job: its node stays on the map, and the registry's `time_left` floors
 * at zero rather than becoming null, so `/data_places` reports the same
 * unchanged nought for as long as the entry is there. A dismissal held in a
 * variable is forgotten by the next page context -- a reload, a second tab, a
 * restart -- which met the fact fresh and announced it again. That is a dialog
 * that cannot be closed, only postponed, so the mark goes in storage.
 *
 * **It counts down locally.** `PlexoraRemotes` deliberately stops polling once
 * every connection is settled -- which is the state a job sits in for its whole
 * four hours -- so a watcher that waited for a request to come back would never
 * fire at all. The snapshot carries how long was left and when it said so, and
 * the arithmetic is `PlexoraRemotes.remaining()`. The only timer here is a
 * fifteen-second check, and it runs only while there is a clock to check.
 *
 * **The deadline is an estimate and it errs early.** It is measured from the
 * moment this Plexora saw the job get allocated, which is a fraction of a
 * second after Slurm started counting -- so a warning arrives slightly sooner
 * than it strictly had to. That is the right direction for the one error it can
 * make: a warning that comes early costs a few seconds of attention, and one
 * that comes late is not a warning.
 */
window.PlexoraSessionExpiry = (function () {
    "use strict";

    const Remotes = () => window.PlexoraRemotes;

    //: How often to look. Not once a second: nothing here is a display, the
    //: thresholds are ten minutes and zero, and a quarter of a minute is finer
    //: than either needs. The dialog, once open, runs its own faster clock.
    const CHECK_MS = 15000;

    //: What has already been said about each connection, by profile name.
    //: `1` warned, `2` expired. Cleared when a connection's remaining time
    //: goes back ABOVE the threshold, which is exactly what starting a new
    //: session does -- so a reconnected machine is warned about again, and a
    //: machine that stays connected is not warned twice.
    //:
    //: In storage rather than in a variable, for the reason in the header: the
    //: fact this is about outlives the page that heard it.
    const TOLD_KEY = "plexora.sessionExpiry.told";

    //: The fallback, and the cache. Storage THROWS rather than returning null
    //: in a private window and wherever site data is turned off, and a watcher
    //: that threw on every check would be a worse bug than one that repeats --
    //: so a failure here leaves this page context knowing what it knew.
    let toldCache = {};

    function told() {
        try {
            const raw = window.localStorage.getItem(TOLD_KEY);
            toldCache = raw ? (JSON.parse(raw) || {}) : {};
        } catch (e) {
            /* keep the cache */
        }
        return toldCache;
    }

    function remember(map) {
        toldCache = map;
        try {
            window.localStorage.setItem(TOLD_KEY, JSON.stringify(map));
        } catch (e) {
            /* this page still behaves; the next one starts over */
        }
    }

    let timer = null;
    let unwatch = null;
    let openDialog = null;
    let started = false;

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

    /** Every connection that is on a clock, with how long it has left. */
    function clocked(snapshot) {
        return (snapshot.entries || []).map((entry) => ({
            entry: entry,
            left: Remotes().remaining(entry),
        })).filter((row) => row.left !== null);
    }

    /**
     * Decide whether anything needs saying, and say it once.
     *
     * One dialog at a time, deliberately: two machines expiring together is a
     * real thing on a multi-cluster setup, and stacking two modals over a
     * viewer would make both unreadable. The second one is still on the clock
     * and gets its turn on the next check.
     */
    function check() {
        const snapshot = Remotes().snapshot();
        const rows = clocked(snapshot);
        if (!rows.length) return stop();

        const map = told();
        const known = (snapshot.entries || []).map((entry) => entry.name);
        let changed = false;
        Object.keys(map).forEach((name) => {
            // A profile nobody has any more cannot be warned about, and its
            // mark would otherwise sit in storage for good.
            if (known.indexOf(name) < 0) {
                delete map[name];
                changed = true;
            }
        });
        rows.forEach((row) => {
            if (row.left > Remotes().WARN_SECONDS
                    && map[row.entry.name] !== undefined) {
                delete map[row.entry.name];
                changed = true;
            }
        });
        if (changed) remember(map);
        if (openDialog) return;

        const expired = rows.find(
            (row) => row.left === 0 && map[row.entry.name] !== 2);
        const warning = rows.find(
            (row) => row.left > 0 && row.left <= Remotes().WARN_SECONDS
                     && !map[row.entry.name]);
        const row = expired || warning;
        if (!row) return;
        // Written before either branch runs, so that the fifteen-second check
        // behind this one cannot say the same thing twice while the first is
        // still deciding whether to say it at all.
        map[row.entry.name] = expired ? 2 : 1;
        remember(map);
        if (expired) return confirmGone(row.entry);
        announce(row.entry, false);
    }

    /**
     * Say a job has ended, once the server agrees that it has.
     *
     * The countdown is interpolated from the last snapshot, and the poll
     * deliberately stops once every connection is settled -- so zero can mean
     * "the allocation ended", or it can mean "we stopped asking an hour ago,
     * and a fresh session has been started since from another tab, from
     * Settings, or from the command line". From in here those are the same
     * number, and announcing the first when it is the second tells somebody
     * their working machine is gone while it is answering.
     *
     * One request at the moment of the transition tells them apart. One per
     * job, not one per check: the mark is already written, and the
     * above-the-threshold rule in `check` takes it back if the answer is that
     * the machine is fine.
     */
    function confirmGone(entry) {
        const say = () => {
            const fresh = Remotes().entry(entry.name) || entry;
            // Renewed while nobody was looking, or disconnected outright.
            // Either way there is no longer anything to announce.
            if (Remotes().remaining(fresh) !== 0) return;
            if (!openDialog) announce(fresh, true);
        };
        // A server that will not answer is not evidence either way, and of the
        // two readings a job that has ended is much the likelier. Say it: the
        // dialog runs its own clock and closes itself if the connection turns
        // out to still be there.
        Remotes().refresh().then(say, say);
    }

    /**
     * The dialog itself.
     *
     * Structurally a `.connect-modal`, so this and the dialog that connects a
     * machine are one flow rather than two windows that happen to follow each
     * other -- pressing the button here opens that one directly.
     */
    function announce(entry, expired) {
        const dialog = el("dialog", "connect-modal expiry-modal");
        const head = el("div", "connect-modal-head");
        const heading = el("div", "connect-modal-heading");
        const title = el("h2", "connect-modal-title",
                         expired
                             ? "“" + entry.label + "” has run out of time"
                             : "“" + entry.label + "” is about to run out of time");
        const subtitle = el("p", "connect-modal-subtitle");
        heading.append(title, subtitle);
        head.append(heading);

        const body = el("div", "connect-modal-body");
        const readout = el("div", "expiry-readout");
        const clock = el("div", "expiry-clock");
        readout.append(clock, el("div", "expiry-clock-label",
                                 expired ? "The job has ended"
                                         : "left on this job"));
        body.append(readout);
        body.append(el("p", "expiry-note", expired
            ? "The scheduler has ended the job that was serving this machine, "
              + "so anything this Plexora was reading from it has stopped "
              + "answering. Starting a new session opens a fresh job and "
              + "reconnects the data node."
            : "When the job ends, the data node on that machine stops "
              + "answering and any project reading from it loses that layer. "
              + "Starting a new session now asks the scheduler for a fresh "
              + "job and reconnects."));
        body.append(el("p", "expiry-note expiry-note-quiet",
                       "Nothing on this computer is affected either way — your "
                       + "projects, ROIs, figures and gates are all here."));

        const actions = el("div", "connect-modal-actions");
        actions.append(button("btn btn-outline-light",
                              expired ? "Close" : "Not now",
                              () => { if (dialog.open) dialog.close(); }));
        actions.append(el("span", "connect-modal-spacer"));
        actions.append(button("btn btn-primary", "Start a new session", () => {
            if (dialog.open) dialog.close();
            restart(entry);
        }));

        dialog.append(head, body, actions);
        document.body.appendChild(dialog);

        // The one place a faster clock is worth it: somebody is reading this
        // number to decide whether there is time to finish what they are
        // doing, and a figure that moved in fifteen-second jumps would be a
        // worse answer than no figure.
        function tick() {
            const left = Remotes().remaining(Remotes().entry(entry.name)
                                             || entry);
            if (left === null) {
                // The connection this is about has stopped being one -- someone
                // disconnected it from the globe, or from here. A modal urging
                // a new session for a machine that already has none is worse
                // than no modal, so it goes.
                if (dialog.open) dialog.close();
                return;
            }
            clock.textContent = left ? Remotes().duration(left) : "0:00";
            subtitle.textContent = left
                ? "The scheduler ends this job in "
                  + Remotes().duration(left) + "."
                : "The scheduler has ended this job.";
            clock.classList.toggle("is-gone", left === 0);
        }
        tick();
        const ticking = window.setInterval(tick, 1000);

        dialog.addEventListener("cancel", () => { /* Escape closes it */ });
        dialog.addEventListener("close", () => {
            window.clearInterval(ticking);
            dialog.remove();
            if (openDialog === dialog) openDialog = null;
        });
        openDialog = dialog;
        dialog.showModal();
    }

    /**
     * End this session and open a fresh one.
     *
     * Disconnect first, and it matters: the old entry names a loopback port
     * whose tunnel is gone or going, and leaving it on the map would have the
     * new session's node land beside a stale one under the same name. It is
     * also what `nodes._disconnected` is keyed on, so work still holding the
     * old address stops trying it rather than rediscovering it one refused
     * connection at a time.
     */
    function restart(entry) {
        const go = () => window.PlexoraConnectionModal.open({
            name: entry.name,
            kind: "node",
            intent: "Asks the scheduler for a new job and opens the data node "
                    + "on it again.",
        });
        Remotes().disconnect(entry.name, "node").then(go, go);
    }

    function stop() {
        if (timer) window.clearInterval(timer);
        timer = null;
    }

    function onSnapshot(snapshot) {
        if (!clocked(snapshot).length) return stop();
        if (!timer) timer = window.setInterval(check, CHECK_MS);
        check();
    }

    /**
     * Start watching. Once per page, and once per PROCESS after that.
     *
     * Passively, like the navbar globe and for the same reason: this must not
     * turn a settled connection into a request a second for the privilege of
     * watching a number go down. The snapshot it already has is enough.
     */
    function start() {
        if (started) return null;
        started = true;
        unwatch = Remotes().subscribe(onSnapshot);
        return function dispose() {
            if (unwatch) unwatch();
            unwatch = null;
            stop();
            started = false;
        };
    }

    return { start, WARN_SECONDS: () => Remotes().WARN_SECONDS };
})();

// Through PlexoraPage rather than DOMContentLoaded: a router swap never fires
// one, and this has to be watching from whichever page the user landed on.
// Nothing is returned -- the guard inside start() is what makes a second run a
// no-op, exactly as the globe's `mounted` is.
PlexoraPage.register(() => {
    if (!window.PlexoraRemotes || !window.PlexoraConnectionModal) return null;
    window.PlexoraSessionExpiry.start();
    return null;
});

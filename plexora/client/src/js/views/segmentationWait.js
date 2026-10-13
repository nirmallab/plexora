/**
 * segmentationWait.js -- a mask pyramid being built, watched from inside the
 * viewer.
 *
 * A mask attached from the edit page is usually not pyramidized, and converting
 * a real slide's mask is minutes of work. That wait used to happen on the EDIT
 * page: the save handler dropped a blocking overlay over the form and reloaded
 * into the viewer only once the job finished. So the user sat on a form
 * watching a bar, with the image they had already imported one navigation away
 * and unreachable -- for a job that was running on the server either way and
 * needed nothing from the browser at all.
 *
 * Nothing has to be waited for, so the viewer opens straight away and this
 * shows the job in two places, one at a time: a MODAL on arrival, saying what
 * is happening and that closing it is safe, and a CHIP in the navbar once it
 * is closed. The modal, the chip and their rules are views/jobWait.js's, shared
 * with the 3-D mask's surfaces (views/maskSurfaceWait.js); this is the words
 * and the events.
 *
 * It asks the server nothing. main.js runs the one poll (`segmentation_status
 * === 'pending'` there) and announces every reading as
 * `plexora:segmentation-progress` / `-ready` / `-failed`; this is one listener
 * and Cell Explorer's mask wait is another. Two surfaces polling the same job
 * would be two answers free to disagree about one fact.
 *
 * Loaded by index.html, and only there: the pending state belongs to a viewer
 * looking at the project whose mask is converting. The chip's mount point is in
 * base.html because the navbar is, and stays empty everywhere else.
 */
(function () {
    "use strict";

    const wait = window.PlexoraJobWait.create({
        mount: "segmentation_chip",
        copy: {
            //: The navbar's wording, fixed. The modal's detail line is the
            //: server's and changes as the job proceeds; this one has to stay
            //: recognisable at a glance over the several minutes it is up.
            chip: "Pyramidizing segmentation mask…",
            working: "Pyramidizing segmentation mask",
            //: Shown until the first poll answers. The job was started by the
            //: save that attached the mask, so this reports a fact rather than
            //: promising one.
            opening: "This mask is not pyramidized. Processing has started in the background.",
            //: The whole point of the modal being dismissible, said plainly. A
            //: progress bar with no way out reads as something the user has to
            //: sit through.
            note: "You can close this at any time and keep viewing the image. "
                + "Processing continues in the background, and the mask is drawn as "
                + "outlines by itself when it is ready.",
            button: "Continue viewing",
            readyTitle: "Segmentation mask ready",
            //: Present tense, and deliberately. main.js starts the switch to the
            //: mask as it announces this and does not wait for it, so on a large
            //: pyramid the first tiles are still arriving while this is up.
            readyDetail: "The mask is ready. Cells are being drawn on the image.",
            failed: "Segmentation mask failed",
            failedDetail: "The mask could not be converted.",
            chipWorkingTip: "Converting in the background — click to see progress",
            chipFailedTip: "The mask could not be converted — click for the reason",
        },
        notify: { title: "Segmentation mask ready", body: "Cells are being drawn on the image." },
    });

    window.addEventListener("plexora:segmentation-progress",
                            (event) => wait.progress(event.detail));
    window.addEventListener("plexora:segmentation-ready", () => wait.ready());
    window.addEventListener("plexora:segmentation-failed",
                            (event) => wait.failed((event.detail || {}).error));

    //: `start` is called by main.js the moment it sees a pending job --
    //: deliberately before its own poll, which runs at the end of viewer boot.
    //: A mask a data node is converting is announced in the chip only
    //: (`start({modal: false})`): the unconverted mask is already on screen.
    //: `progress`/`ready`/`failed` are the same readings the events carry, for
    //: a caller whose news must not reach every listener: a data node
    //: converting a mask that is already drawn (resourceStatus.js). Its failure
    //: is not the plugins' "no mask is coming" -- the unconverted one still is.
    window.PlexoraSegmentationWait = {
        start: wait.start, progress: wait.progress, ready: wait.ready, failed: wait.failed,
    };
})();

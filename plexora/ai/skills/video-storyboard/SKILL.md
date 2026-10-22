# Video storyboard

Make a scientific video of an image with Video Studio: choose a preset or a
storyboard from what the user asks for, show them what it will do, preview it
in their viewer, and only then export it.

## When to use

- "Make a video / movie / animation of…", "rotate the stack", "sweep through
  the planes", "zoom into this region", "show the markers one by one".
- A figure panel the user wants animated.

## When not to use

- A single picture: visual-inspection renders one.
- Anything that needs a value blended between acquired timepoints or planes:
  Video Studio refuses that by design (a frame always shows a plane and a
  timepoint that were imaged). Say so rather than looking for a way round it.

## Required features

An open viewer for preview and export (`list_viewers`); a z-stack for the
volume and stack presets; a time series for the time-lapse; two or more
channels for a reveal. Video Studio is part of Plexora Pro (`video_studio` is a
plugin licensed as a whole, `plugin:video_studio`).

## Decision logic

1. Know the image (`inspect_project`; `inspect_volume` for a stack): planes,
   timepoints, spacing, channels. Never pick a volume preset for a flat image.
2. Map the request to a preset:
   - a turn: `orbit` (a full turn), `rock` (back and forth), `tumble`;
   - depth: `z_sweep` (plane by plane), `slice_reveal` (planes added in the
     volume view, or a cut from a side), `fly_through`;
   - time: `time_lapse`;
   - a region: `zoom` (with `end_view`), `xy_pan`;
   - markers: `channel_reveal` (`order` with channel keys exactly as
     `list_channels` gives them);
   - a story: `story_overview`, `story_structure`, `story_markers`,
     `story_orthogonal`, `story_timeseries`.
3. `create_video` with the preset and only the parameters the user asked for;
   the rest default from the image and the viewer (`from_viewer` starts from
   what they are looking at). `list_videos` finds one made earlier.
4. Show the user `get_video`'s summary in plain words: length, what moves,
   which planes or timepoints, any `notes` and `warnings` (a preset that
   switched the viewer to the volume view says so).
5. `preview_video` so they can watch it in their viewer. Adjust with
   `edit_video` until they are happy.
6. Only on their go-ahead: `export_video` (a job; `job_wait` follows it).

## Tools

`inspect_project`, `inspect_volume`, `list_channels`, `list_viewers`, `create_video`,
`get_video`, `list_videos`, `edit_video`, `preview_video`, `export_video`, `job_wait`.

## Evidence

Report the export's file name and size, its frame count and duration from the
manifest, and the preset and parameters it was made with (`get_video`).

## Uncertainty

- A volume view is drawn in perspective: its scale bar is true at the depth of
  the stack's centre only. Say so if the user will measure from it.
- `display_transitions` in a manifest are fades, window changes and cuts
  through the stack: changes of how data is shown, never of the data. Name
  them when describing the video.

## Mutation policy

`create_video`, `edit_video`, `add_video_keyframe`, `set_video_track` and
`duplicate_video` write a video in Plexora's own store, never the image;
every receipt carries an `undo_hint`. `delete_video` removes a video and its
exports and needs the user's confirmation. `export_video` writes a file and
its manifest beside the video.

## Provenance

Every export's manifest lists, per frame, the acquired plane and timepoint it
shows, and `acquired_verified` says the browser applied exactly those. Cite
the export id and the manifest.

## Done when

The user has seen the preview, agreed to the export, and has the file's name,
size, length and a sentence on what each part of the video shows.

## Failure modes

- `invalid_input` from `create_video`: the preset needs what the image lacks
  (planes, timepoints, channels); say which and offer one that fits.
- `invalid_input` from `edit_video` naming whole steps: an edit tried to blend
  acquired data; keep planes and timepoints as whole values.
- `viewer_not_available`: no open viewer; ask the user to open the project.
- `license_required`: Video Studio is not on this licence; pass on the hint.
- A rebuild from a preset discards edits: edit a recorded or edited video with
  `edit_video`, never by creating it again.

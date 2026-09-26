# Dataset triage

Know what a project is before proposing anything about it. Five facts decide
what is possible: the image (kind, channels, size, pixel size), the mask, the
cell table (roles, markers, cell count), where each lives, and what is still
unanswered.

## When to use

- First contact with a project or dataset in a conversation.
- The request is vague ("what's in my melanoma data?", "can you analyse this?").
- Before any other skill, if you have not inspected the project in this conversation.

## When not to use

- You inspected the project earlier in this conversation and nothing has changed.
- The user asked a single, specific factual question a single tool answers.

## Required features

None. Every project has at least a reference frame; this skill reports what else exists.

## Decision logic

1. `server_info` once per conversation: data directory, permissions, loaded plugins,
   whether a viewer is attached.
2. If the user named a dataset (a cohort), `list_datasets` then `get_dataset`;
   otherwise `list_projects` (use `query` to narrow by name).
3. `inspect_project` for each project you will talk about. Read:
   - `image`: kind, channels, `pixel_size` (null means uncalibrated: no microns anywhere),
   - `segmentation.available`,
   - `table`: `n_cells`, `roles` (cell_id/x/y/image_id), `markers`, `log_transformed`,
   - `capabilities`: per plugin, `applies` and `missing`,
   - `open_questions`: what the project still needs answered.
4. If anything is on a data node, or a read failed, `get_resource_status`.
5. `list_channels` / `list_markers` when the question is about specific markers; note
   `markers_without_channel` (a column you cannot look at) and
   `channels_without_marker` (a channel you cannot gate).
6. For the user's actual request, `validate_scope` with their words and the project.

## Tools

`server_info`, `list_datasets`, `get_dataset`, `list_projects`, `inspect_project`,
`get_resource_status`, `list_channels`, `list_markers`, `validate_scope`.

## Evidence

Numbers from `inspect_project` (cell count, channel list, pixel size, roles). No images
are needed for triage; do not render to "get a feel" unless asked.

## Uncertainty

- `markers_classified: false` means the marker list is Plexora's guess from the table's
  numeric columns. Say so.
- A missing `pixel_size` is not an error: report sizes in pixels and never invent microns.
- `validate_scope` returning `can_recommend` means Plexora knows how but something is
  missing -- name the missing thing rather than stopping.

## Mutation policy

Triage changes nothing. None of these tools write.

## Provenance

Name the project, the data directory from `server_info`, and cite the cell count and
channel list as reported by `inspect_project`.

## Done when

You can state, for each project in scope: image kind and channels, calibrated or not,
mask present or not, number of cells and the marker list, where the data lives, and
which of the user's requests are `can_execute`/`can_analyze`/`can_recommend`/
`outside_domain`.

## Failure modes

- `unknown_project`: use `detail.did_you_mean`, or `list_projects`.
- `resource_unavailable` (retryable): a data node is asleep or unreachable; say which
  node, suggest reconnecting it, and continue with what is local.
- `precondition_missing`: report `detail.missing` in plain words ("this project has no
  cell table yet").

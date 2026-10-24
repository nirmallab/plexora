"""Which images, masks and tables under a folder belong together.

`dataset_scan` finds the files; this decides what they make: one project per
image, its segmentation mask, its cell table and its channel names, each with
a confidence and the reasons for it. Deterministic, explainable, and a pure
function of the inventory and the user's corrections -- the same tree and the
same corrections always give the same plan, and nothing here reads a file
except the few channel lists it names channels from.

**Identity before anything else.** A file's identity is the words and numbers
of its name with the words that describe a ROLE stripped from either end:
`Sample02_registered` is Sample02, `LSP11385--unmicst_cell` is LSP11385,
`X_cp_masks` and `X_mesmer_whole_cell` are X. A name that is nothing BUT role
words (`image.ome.tiff`, `cell_mask.tif`) is generic and says nothing about
which sample it is; it takes its identity from the nearest folder above it
that is not itself a role folder (`Sample01/registered/image.ome.tiff` is
Sample01; `segmentation/unmicst-X/cell.ome.tif` is X), and from nothing at
all when that search reaches the scan root. Two generic `image.ome.tiff`s are
therefore two projects, told apart by their folders, and never one project
matched by name.

**Scores, then margins.** A mask or table is scored against every project by
independent signals (`WEIGHTS`): same identity, the project's name inside its
path, the same folder, a naming convention seen elsewhere in the tree, the
same pixel size when no other image shares it. A different pixel size vetoes
the pair outright. Confidence is about the COMPETITION, not the score: a
match is high only when it is strong AND well clear of the runner-up, so a
mask that two projects could equally own is never quietly given to one of
them -- it is left unassigned with both suggested.

- high: filled in, nothing to do.
- medium: filled in and flagged for review.
- low: left unassigned, with the best suggestions and their reasons.

**One table, many images.** An AnnData that holds several images' cells (an
obs column naming the image) is matched per project by that column's values
(`import_proposal.subset_match`) and assigned to every project it names, each
reading only its own rows. A CSV cannot be filtered that way, so a CSV with an
image-id column and no single owner is left unassigned, saying why.
"""

from __future__ import annotations

import copy
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

#: What each signal adds to a pair's score (capped at 1).
WEIGHTS = {
    "identity_exact": 0.6,
    "identity_contains": 0.45,
    "same_folder": 0.2,
    "convention": 0.3,
    "size_unique": 0.25,
    "role_folder": 0.05,
    "subset_value": 0.9,
}

#: `(minimum score, minimum margin over the runner-up)` for each confidence.
HIGH = (0.6, 0.3)
MEDIUM = (0.4, 0.1)

IMAGE_WORDS = frozenset(
    "registration registered images image img imgs raw stitched ashlar scans "
    "scan slides slide morphology tiles tile".split())
MASK_WORDS = frozenset(
    "segmentation segmentations seg segs masks mask labels label cellpose "
    "mesmer unmicst stardist deepcell cp nuclei nuclear nucleus nuc nucs cell "
    "cells wholecell whole cyto cytoplasm cellring ring membrane".split())
TABLE_WORDS = frozenset(
    "quantification quant quants tables table data csv features feature "
    "singlecell single scimap anndata h5ad counts".split())
STRUCTURAL_WORDS = frozenset(
    "outs output outputs results result processed analysis run runs pipeline "
    "final ome".split())
ROLE_WORDS = IMAGE_WORDS | MASK_WORDS | TABLE_WORDS | STRUCTURAL_WORDS

#: Folder names that are a role in full, whatever their words say.
ROLE_FOLDER_NAMES = {"s3seg": "mask", "mcquant": "table", "dearray": "image"}

#: Words that, with no number beside them, name nothing in particular.
GENERIC_WORDS = frozenset(
    "sample samples tissue specimen core region roi default untitled new "
    "copy test".split())

SLOTS = ("mask", "table")
ROLE_LABELS = {"image": "image", "mask": "segmentation", "table": "data",
               "channels": "channel list"}


# -- identity -------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """What a file says it is: `key` compares, `name` is shown."""

    key: tuple
    name: str
    source: str  # stem | folder | bundle
    evidence: str

    @property
    def text(self):
        return "_".join(str(token) for token in self.key)


def _tokens(text):
    from plexora.server.models.import_proposal import _id_tokens

    return _id_tokens(text)


def _strip(tokens):
    """`tokens` without role words at either end."""
    tokens = list(tokens)
    while tokens and isinstance(tokens[0], str) and tokens[0] in ROLE_WORDS:
        tokens.pop(0)
    while tokens and isinstance(tokens[-1], str) and tokens[-1] in ROLE_WORDS:
        tokens.pop()
    return tuple(tokens)


def is_generic(tokens) -> bool:
    """Whether a stripped name says nothing about which sample it is."""
    if not tokens:
        return True
    if any(isinstance(token, int) for token in tokens):
        return False
    return all(token in GENERIC_WORDS or token in ROLE_WORDS for token in tokens)


def _display(text):
    """`text` without role words at either end, in its own spelling."""
    parts = list(re.finditer(r"[A-Za-z]+|[0-9]+", text))
    keep = list(parts)
    while keep and not keep[0].group().isdigit() and keep[0].group().lower() in ROLE_WORDS:
        keep.pop(0)
    while keep and not keep[-1].group().isdigit() and keep[-1].group().lower() in ROLE_WORDS:
        keep.pop()
    if not keep:
        return None
    return text[keep[0].start():keep[-1].end()].strip(" _-.")


def dir_role(name):
    """`image`, `mask`, `table` or `structural` for a folder whose name is a
    role and nothing else (`segmentation`, `quantification`, `outs`); None for
    a folder that names something (`Sample01`, `run3`)."""
    lowered = str(name).lower()
    if lowered in ROLE_FOLDER_NAMES:
        return ROLE_FOLDER_NAMES[lowered]
    tokens = _tokens(lowered)
    if not tokens or any(isinstance(token, int) for token in tokens):
        return None
    if not all(token in ROLE_WORDS for token in tokens):
        return None
    for words, role in ((MASK_WORDS, "mask"), (TABLE_WORDS, "table"),
                        (IMAGE_WORDS, "image")):
        if any(token in words for token in tokens):
            return role
    return "structural"


def _stem(name):
    from plexora.server.models.import_proposal import _file_stem

    stem = _file_stem(name)
    for suffix in (".zarr", ".n5", ".btf"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem


def _folder_identity(entry):
    """The nearest folder below the root that names something, or None."""
    parts = PurePosixPath(entry.rel).parts[:-1]
    for depth in range(len(parts) - 1, -1, -1):
        folder = parts[depth]
        if dir_role(folder) is not None:
            continue
        tokens = _strip(_tokens(folder))
        if is_generic(tokens):
            continue
        evidence = "/".join(parts[:depth + 1]) + "/"
        return Identity(tokens, _display(folder) or folder, "folder", evidence)
    return None


def identity_of(entry):
    """The `Identity` a file or run carries, or None for a generic name with
    no naming folder between it and the scan root."""
    if entry.kind == "bundle":
        for text in (entry.label, _stem(entry.name)):
            tokens = _strip(_tokens(text or ""))
            if not is_generic(tokens):
                return Identity(tokens, _display(text) or text, "bundle", entry.rel)
        return _folder_identity(entry)
    stem = _stem(entry.name)
    tokens = _strip(_tokens(stem))
    if not is_generic(tokens):
        return Identity(tokens, _display(stem) or stem, "stem", entry.name)
    return _folder_identity(entry)


def _parent(entry):
    return os.path.dirname(entry.path)


def _role_folder_of(entry, role):
    """The nearest folder above `entry` whose name is `role`, or None."""
    for folder in reversed(PurePosixPath(entry.rel).parts[:-1]):
        if dir_role(folder) == role:
            return folder
    return None


def _size_text(size):
    return f"{size[0]}×{size[1]}" if size else ""


# -- conventions -------------------------------------------------------------------


def _identity_regex(identity):
    parts = []
    for token in identity.key:
        parts.append(f"0*{token}" if isinstance(token, int) else re.escape(token))
    body = r"[^A-Za-z0-9/]*".join(parts)
    first, last = identity.key[0], identity.key[-1]
    before = r"(?<![0-9])" if isinstance(first, int) else r"(?<![A-Za-z])"
    after = r"(?![0-9])" if isinstance(last, int) else r"(?![A-Za-z])"
    return re.compile(before + body + after, re.IGNORECASE)


def pattern_of(entry, identity):
    """`entry`'s path relative to its root, with `identity` written `{id}`:
    `Sample01_nuc.tiff` -> `{id}_nuc.tiff`. None when the path does not
    spell the identity (it came from somewhere the path does not show)."""
    if identity is None or not identity.key:
        return None
    regex = _identity_regex(identity)
    if not regex.search(entry.rel):
        return None
    return regex.sub("{id}", entry.rel)


def _pattern_regex(pattern):
    pieces = pattern.split("{id}")
    body = re.escape(pieces[0])
    for index, piece in enumerate(pieces[1:]):
        body += (r"(?P<id>[^/]+?)" if index == 0 else r"(?P=id)") + re.escape(piece)
    return re.compile(body + r"\Z", re.IGNORECASE)


def _pattern_owner(pattern_regex, entry, keys):
    """The project a convention says `entry` belongs to, or None."""
    found = pattern_regex.match(entry.rel)
    if not found:
        return None
    tokens = _strip(_tokens(found.group("id")))
    return keys.get(tokens)


# -- overrides ---------------------------------------------------------------------


def clean_overrides(overrides) -> dict:
    """The overrides document, checked and normalised.

    ```
    {"assign": [{"path", "project", "slot": "mask|table|image|channels", "value"?}],
     "exclude": [path], "detach": [path], "new_project": [{"image", "name"?}],
     "rename": {project_id: name}, "learn": [{"path", "project"}]}
    ```
    """
    raw = overrides or {}
    if not isinstance(raw, dict):
        raise ValueError("overrides must be an object")

    def paths(key):
        values = raw.get(key) or []
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError(f"overrides.{key} must be a list of paths")
        return list(dict.fromkeys(values))

    assign = []
    for item in raw.get("assign") or []:
        if not isinstance(item, dict) or not item.get("path") or not item.get("project"):
            raise ValueError("each overrides.assign needs a path and a project")
        slot = item.get("slot") or "mask"
        if slot not in ("mask", "table", "image", "channels"):
            raise ValueError(f"unknown slot {slot!r}")
        clean = {"path": str(item["path"]), "project": str(item["project"]),
                 "slot": slot}
        if item.get("value") not in (None, ""):
            clean["value"] = str(item["value"])
        assign.append(clean)
    new_project = []
    for item in raw.get("new_project") or []:
        if isinstance(item, str):
            item = {"image": item}
        if not isinstance(item, dict) or not item.get("image"):
            raise ValueError("each overrides.new_project needs an image")
        clean = {"image": str(item["image"])}
        if item.get("name"):
            clean["name"] = str(item["name"]).strip()
        new_project.append(clean)
    rename = raw.get("rename") or {}
    if not isinstance(rename, dict):
        raise ValueError("overrides.rename must map project ids to names")
    rename = {str(k): str(v).strip() for k, v in rename.items() if str(v).strip()}
    learn = []
    for item in raw.get("learn") or []:
        if not isinstance(item, dict) or not item.get("path") or not item.get("project"):
            raise ValueError("each overrides.learn needs a path and a project")
        learn.append({"path": str(item["path"]), "project": str(item["project"])})
    return {"assign": assign, "exclude": paths("exclude"), "detach": paths("detach"),
            "new_project": new_project, "rename": rename, "learn": learn}


def _excluded(path, excluded):
    for item in excluded:
        if path == item or path.startswith(item.rstrip("/\\") + os.sep):
            return item
    return None


# -- the plan ----------------------------------------------------------------------


@dataclass
class _Project:
    id: str
    image: object
    identity: Identity | None
    kind: str = "loose"
    keys: set = field(default_factory=set)
    alternatives: list = field(default_factory=list)
    order: int = 0
    name: str = ""
    existing: str | None = None
    forced: bool = False
    slots: dict = field(default_factory=dict)
    candidates: dict = field(default_factory=lambda: defaultdict(list))
    warnings: list = field(default_factory=list)
    image_confidence: str = "high"
    image_reasons: list = field(default_factory=list)
    channels: dict | None = None

    def has_own(self, slot):
        if self.kind != "bundle":
            return False
        role = "mask" if slot == "mask" else "table"
        return any(layer.get("role") == role for layer in (self.image.layers or ()))


@dataclass
class _Score:
    project: _Project
    score: float
    reasons: list
    veto: str | None = None
    convention: bool = False


def _confidence(best, runner_up, *, capped=False):
    margin = best - runner_up
    if best >= HIGH[0] and margin >= HIGH[1] - 1e-9:
        level = "high"
    elif best >= MEDIUM[0] and margin >= MEDIUM[1] - 1e-9:
        level = "medium"
    else:
        return "low"
    return "medium" if capped else level


def confidence(best, runner_up):
    """`high`, `medium` or `low` for a best score and its runner-up's."""
    return _confidence(best, runner_up)


def _mask_rank(entry):
    from plexora.server.models.import_proposal import _mask_rank as rank

    return rank({"relative": entry.rel})


def _reference_order(entry):
    from plexora.server.models.import_proposal import REFERENCE_PREFERENCE

    modality = entry.modality or ""
    preference = (REFERENCE_PREFERENCE.index(modality)
                  if modality in REFERENCE_PREFERENCE else len(REFERENCE_PREFERENCE))
    size = entry.size_wh() or (0, 0)
    in_image_folder = 0 if _role_folder_of(entry, "image") else 1
    return (preference, -(size[0] * size[1]), in_image_folder, entry.rel.lower())


def _existing_for(entry, config):
    from plexora.datasource import _find_existing_datasource_for_image

    if not config:
        return None
    if entry.kind == "bundle":
        from plexora.server.models.project import Project

        root = os.path.realpath(entry.path)
        for name, record in config.items():
            try:
                bundles = Project.from_entry(name, record).bundles
            except Exception:  # noqa: BLE001 -- one odd record is not a plan failure
                continue
            for bundle in bundles:
                if bundle.get("root") and os.path.realpath(bundle["root"]) == root:
                    return name
        return None
    try:
        return _find_existing_datasource_for_image(entry.real, config)
    except Exception:  # noqa: BLE001
        return None


_PANEL_CACHE: dict = {}


def _panel_names(entry, count):
    from plexora.server.models.import_proposal import _panel_names as names

    key = (entry.key, count)
    if key not in _PANEL_CACHE:
        try:
            _PANEL_CACHE[key] = names(entry.path, count)
        except Exception:  # noqa: BLE001
            _PANEL_CACHE[key] = None
    return _PANEL_CACHE[key]


def _public(entry):
    return {"path": entry.path, "rel": entry.rel, "name": entry.name,
            "label": entry.label or entry.name, "detail": entry.detail or "",
            "size": _size_text(entry.size_wh())}


def _kind_word(entry):
    if entry.role in ("image", "mask", "table", "channels"):
        return entry.role
    return "other"


def plan(inventory, overrides=None, *, config=None) -> dict:
    """The plan for `inventory` under `overrides`. Pure and deterministic.

    @param config - the registered projects, `{name: record}`, for finding
        images already imported. Read from the data root when None.
    """
    if config is None:
        from plexora import get_config

        config = get_config() or {}
    overrides = clean_overrides(overrides)
    entries = inventory.ordered()
    warnings = []

    # -- what is out of the plan before it starts --------------------------
    excluded_items = {item: 0 for item in overrides["exclude"]}
    kept = []
    for entry in entries:
        hit = _excluded(entry.path, overrides["exclude"])
        if hit is not None:
            excluded_items[hit] += 1
            continue
        kept.append(entry)
    excluded = [{"path": item, "count": count, "folder": os.path.isdir(item)}
                for item, count in excluded_items.items()]

    duplicates, unique, by_real, by_copy = [], [], {}, {}
    for entry in kept:
        same = by_real.get(entry.real)
        how = "the same file, reached through a link"
        if same is None and entry.kind != "bundle" and entry.size > 0:
            same = by_copy.get((entry.name.lower(), entry.size, entry.mtime_ns))
            how = "an identical copy"
        if same is not None:
            duplicates.append({"path": entry.path, "rel": entry.rel,
                               "same_as": same.path, "how": how})
            continue
        by_real[entry.real] = entry
        if entry.kind != "bundle" and entry.size > 0:
            by_copy[(entry.name.lower(), entry.size, entry.mtime_ns)] = entry
        unique.append(entry)
    by_path = {entry.path: entry for entry in unique}

    unrecognised, folders_said = [], set()
    for e in unique:
        if e.role is not None:
            continue
        folder = getattr(e, "channel_folder", None)
        if folder:
            # One row for the folder: its forty markers are one image.
            if folder in folders_said:
                continue
            folders_said.add(folder)
            unrecognised.append({"path": folder, "rel": os.path.dirname(e.rel) or folder,
                                 "reason": e.reason, "folder": True})
            continue
        unrecognised.append({"path": e.path, "rel": e.rel,
                             "reason": e.reason or f"{e.name}: nothing Plexora reads."})

    # -- roles, with the user's word over detection ---------------------------
    role = {entry.path: entry.role for entry in unique}
    forced_images = {item["image"]: item for item in overrides["new_project"]
                     if item["image"] in by_path}
    for path in forced_images:
        role[path] = "image"
    assigned = defaultdict(list)  # path -> [assign items]
    for item in overrides["assign"]:
        if item["path"] in by_path:
            assigned[item["path"]].append(item)
            if item["slot"] in ("mask", "table", "channels") and \
                    item["path"] not in forced_images:
                role[item["path"]] = item["slot"]
    detached = {path for path in overrides["detach"] if path in by_path}

    # -- projects: one per image identity ----------------------------------
    identities = {entry.path: identity_of(entry) for entry in unique}
    projects, groups = [], OrderedDictList()
    for entry in unique:
        if role[entry.path] != "image" or entry.path in detached:
            continue
        if entry.path in forced_images:
            groups.add(("n", entry.path), entry)
            continue
        if any(item["slot"] == "image" for item in assigned.get(entry.path, ())):
            continue  # placed into the project the user named, below
        identity = identities[entry.path]
        if entry.kind == "bundle":
            groups.add(("b", entry.path), entry)
        elif identity is None:
            groups.add(("f", entry.path), entry)
        else:
            groups.add(("p", identity.key), entry)
    for order, (group_key, members) in enumerate(groups.items()):
        members = sorted(members, key=_reference_order)
        reference = members[0]
        identity = identities[reference.path]
        kind_tag, value = group_key
        project_id = (f"p:{identity.text}" if kind_tag == "p"
                      else f"{kind_tag}:{value}")
        project = _Project(id=project_id, image=reference, identity=identity,
                           kind="bundle" if reference.kind == "bundle" else "loose",
                           order=order, forced=kind_tag == "n")
        if identity is not None:
            project.keys.add(identity.key)
            project.image_reasons.append(
                f"named {identity.name}" if identity.source != "folder"
                else f"in folder {identity.evidence}")
        home = _folder_identity(reference)
        if home is not None and kind_tag != "n":
            project.keys.add(home.key)
        for other in members[1:]:
            project.alternatives.append({
                **_public(other), "score": None,
                "reasons": [f"also named {identity.name}" if identity else "same name"]})
        if project.alternatives:
            project.image_confidence = "medium"
            project.image_reasons.append(
                f"{len(members)} images share this name; the "
                f"{(reference.modality or 'first')} one is used")
        if reference.ambiguous and reference.inspected:
            project.image_confidence = "medium"
            project.image_reasons.append("its pixels could also be a segmentation mask")
        if not reference.inspected:
            project.image_confidence = "medium"
        projects.append(project)
    by_id = {project.id: project for project in projects}

    # The user's image placements: replace a project's reference image.
    for path, items in assigned.items():
        for item in items:
            if item["slot"] != "image":
                continue
            project = by_id.get(item["project"])
            if project is None:
                warnings.append(f"{by_path[path].name} was assigned to a project "
                                "that is no longer in the plan.")
                continue
            previous = project.image
            project.image = by_path[path]
            project.image_confidence = "user"
            project.image_reasons = ["chosen by you"]
            project.alternatives = [alt for alt in project.alternatives
                                    if alt["path"] != path]
            if previous.path != path:
                project.alternatives.insert(0, {**_public(previous), "score": None,
                                                "reasons": ["replaced by your choice"]})

    for project in projects:
        project.existing = _existing_for(project.image, config)

    # -- names -------------------------------------------------------------
    taken = set(config) - {p.existing for p in projects if p.existing}
    for project in projects:
        forced = forced_images.get(project.image.path) or {}
        wanted = overrides["rename"].get(project.id) or forced.get("name")
        if project.existing and not wanted:
            project.name = project.existing
            continue
        from plexora.datasource import _dedupe_dataset_name
        from plexora.server.models.import_proposal import _clean_name

        if wanted:
            base = wanted
        elif project.kind == "bundle":
            base = _clean_name(project.image.label or project.image.name)
        elif project.identity is not None:
            base = _clean_name(project.identity.name)
        else:
            base = _clean_name(_stem(project.image.name))
        name = _dedupe_dataset_name(base, taken)
        if wanted and name != wanted:
            project.warnings.append(f"There is already a project called {wanted}; "
                                    f"this one will be {name}.")
        project.name = name
        taken.add(name)

    # -- indexes for scoring ---------------------------------------------------
    key_index = defaultdict(list)
    for project in projects:
        for key in project.keys:
            key_index[key].append(project)
    # "Path contains" reads a project's own name only. A folder key also
    # names whatever else is in that folder (`example_data/`), which is why
    # it only ever matches a generic file exactly.
    name_index = defaultdict(list)
    for project in projects:
        if project.identity is not None:
            name_index[project.identity.key].append(project)
    unique_keys = {key: found[0] for key, found in key_index.items() if len(found) == 1}
    folder_index = defaultdict(list)
    size_index = defaultdict(list)
    for project in projects:
        folder_index[_parent(project.image)].append(project)
        size = project.image.size_wh()
        if size:
            size_index[size].append(project)
    longest_key = max((len(key) for key in name_index), default=0)

    def contained(entry):
        """Projects whose identity is a run of words in `entry`'s path."""
        rel = str(PurePosixPath(entry.rel).with_name(_stem(entry.name)))
        tokens = _tokens(rel)
        numbers = [t for t in tokens if isinstance(t, int)]
        found = {}
        for size in range(min(longest_key, len(tokens)), 0, -1):
            for start in range(len(tokens) - size + 1):
                run = tuple(tokens[start:start + size])
                for project in name_index.get(run, ()):
                    words = "".join(t for t in run if isinstance(t, str))
                    bare = all(isinstance(t, int) for t in run)
                    if bare and numbers != list(run):
                        continue
                    if not bare and len(words) < 4 and not any(
                            isinstance(t, int) for t in run):
                        continue
                    found.setdefault(project.id, (project, size))
        return found

    def score_pair(entry, slot, conventions):
        """Every project's `_Score` for `entry` in `slot`, best first."""
        identity = identities.get(entry.path)
        candidates = {}
        if identity is not None:
            for project in key_index.get(identity.key, ()):
                candidates[project.id] = project
        contains = contained(entry)
        for project, _size in contains.values():
            candidates[project.id] = project
        for project in folder_index.get(_parent(entry), ()):
            candidates[project.id] = project
        size = entry.size_wh() if slot == "mask" else None
        if size:
            for project in size_index.get(size, ()):
                candidates[project.id] = project
        owners = {}
        for pattern, regex in conventions:
            owner = _pattern_owner(regex, entry, unique_keys)
            if owner is not None:
                owners[owner.id] = pattern
                candidates[owner.id] = owner
        scores = []
        role_folder = _role_folder_of(entry, "mask" if slot == "mask" else "table")
        for project in candidates.values():
            if project.has_own(slot):
                continue
            total, reasons, veto = 0.0, [], None
            if identity is not None and identity.key in project.keys:
                total += WEIGHTS["identity_exact"]
                reasons.append(f"named {project.identity.name if project.identity else identity.name}"
                               if identity.source != "folder"
                               else f"in folder {identity.evidence}")
            elif project.id in contains:
                total += WEIGHTS["identity_contains"]
                reasons.append(f"path contains {project.identity.name if project.identity else project.name}")
            if _parent(entry) == _parent(project.image):
                total += WEIGHTS["same_folder"]
                reasons.append("same folder")
            if project.id in owners:
                total += WEIGHTS["convention"]
                reasons.append(f"follows {owners[project.id]}")
            if slot == "mask":
                theirs = project.image.size_wh()
                if size and theirs and size != theirs:
                    veto = f"different size: {_size_text(size)} vs {_size_text(theirs)}"
                elif size and theirs and len(size_index.get(size, ())) == 1:
                    total += WEIGHTS["size_unique"]
                    reasons.append(f"same size {_size_text(size)}")
            if total > 0 and role_folder:
                total += WEIGHTS["role_folder"]
                reasons.append(f"in {role_folder}/")
            if veto:
                scores.append(_Score(project, 0.0, reasons, veto))
            elif total > 0:
                scores.append(_Score(project, round(min(total, 1.0), 3), reasons,
                                     convention=project.id in owners))
        scores.sort(key=lambda s: (-s.score, s.project.order))
        return scores

    # -- the pools ---------------------------------------------------------------
    user_placed = set(assigned)
    pool = {slot: [e for e in unique if role[e.path] == slot
                   and e.path not in user_placed and e.path not in detached]
            for slot in SLOTS}
    pool_tables = pool["table"]

    # Shared AnnData: one table, every project its image-id column names.
    shared = {}
    for entry in pool_tables:
        if not (entry.subset and entry.subset.get("values")):
            continue
        matches = _subset_matches(entry, projects)
        if matches:
            shared[entry.path] = matches
    pool["table"] = [e for e in pool_tables if e.path not in shared]

    # Conventions: pass one finds the high pairs, pass two applies the pattern
    # they share (and any the user taught) to everything else.
    learned = []
    for item in overrides["learn"]:
        entry, project = by_path.get(item["path"]), by_id.get(item["project"])
        if entry is None or project is None:
            continue
        pattern = pattern_of(entry, project.identity) or (
            pattern_of(entry, identities.get(entry.path)))
        if pattern is None:
            warnings.append(f"{entry.name} does not spell {project.name}'s name, "
                            "so there is no rule to learn from it.")
            continue
        slot = next((a["slot"] for a in assigned.get(entry.path, ())
                     if a["project"] == project.id), role[entry.path])
        if slot not in SLOTS:
            continue
        learned.append({"pattern": pattern, "slot": slot, "from": entry.path,
                        "regex": _pattern_regex(pattern)})
        # A learned rule says what role its files are, too: `{id}_nuc.tiff`
        # is a mask even where the pixels could have been an image.
        for other in unique:
            if (other.path not in user_placed and other.path not in detached
                    and other.path != entry.path and role[other.path] in ("image", "mask", "table")
                    and role[other.path] != slot and other.kind != "bundle"
                    and _pattern_owner(learned[-1]["regex"], other, unique_keys)
                    and not _is_project_image(other, projects)):
                role[other.path] = slot
                pool[slot].append(other)
                for other_slot in SLOTS:
                    if other_slot != slot and other in pool[other_slot]:
                        pool[other_slot].remove(other)

    first = {slot: {e.path: score_pair(e, slot, []) for e in pool[slot]} for slot in SLOTS}
    conventions = {}
    for slot in SLOTS:
        patterns = []
        for entry in pool[slot]:
            scores = first[slot][entry.path]
            if not scores:
                continue
            best = scores[0]
            runner = scores[1].score if len(scores) > 1 else 0.0
            if _confidence(best.score, runner) == "high":
                identity = identities.get(entry.path)
                pattern = pattern_of(entry, identity if identity and identity.key in best.project.keys
                                     else best.project.identity)
                if pattern:
                    patterns.append(pattern)
        if len(patterns) >= 2:
            pattern, count = Counter(patterns).most_common(1)[0]
            if count >= 2 and count * 2 >= len(patterns):
                conventions[slot] = pattern
    rules = {slot: ([(conventions[slot], _pattern_regex(conventions[slot]))]
                    if slot in conventions else [])
             + [(item["pattern"], item["regex"]) for item in learned if item["slot"] == slot]
             for slot in SLOTS}

    # -- assignment ----------------------------------------------------------------
    unassigned = []
    suggestions_for = {}
    for slot in SLOTS:
        for entry in pool[slot]:
            scores = score_pair(entry, slot, rules[slot]) if rules[slot] else first[slot][entry.path]
            best = scores[0] if scores and scores[0].score > 0 else None
            runner = next((s.score for s in scores[1:]), 0.0) if best else 0.0
            capped = not entry.inspected or (
                entry.ambiguous and not (best and _mask_corroborated(entry, best.project)))
            level = _confidence(best.score, runner, capped=capped) if best else "low"
            if best is not None and level != "low":
                best.project.candidates[slot].append((best, level, entry, runner))
            else:
                suggestions_for[entry.path] = scores
                unassigned.append(_unassigned(entry, slot, scores, role[entry.path]))

    # The user's placements.
    for path, items in assigned.items():
        entry = by_path[path]
        for item in items:
            if item["slot"] == "image":
                continue
            project = by_id.get(item["project"])
            if project is None:
                warnings.append(f"{entry.name} was assigned to a project that is "
                                "no longer in the plan.")
                continue
            if item["slot"] == "channels":
                project.channels = _channels_slot(entry, project, user=True)
                continue
            subset = None
            if item["slot"] == "table" and entry.subset and entry.subset.get("values"):
                value = item.get("value") or _subset_value(entry, project)
                if value:
                    subset = {"column": entry.subset.get("column"), "value": value}
            project.slots[item["slot"]] = {
                **_public(entry), "confidence": "user", "score": 1.0,
                "reasons": ["assigned by you"], "alternatives": [], "user": True,
                "subset": subset}

    for project in projects:
        for slot in SLOTS:
            if slot in project.slots:
                # The user's choice stands; what scored for it is an alternative.
                project.slots[slot]["alternatives"] = [
                    _alternative(c[0], c[2]) for c in project.candidates.get(slot, ())]
                continue
            chosen = sorted(project.candidates.get(slot, ()),
                            key=lambda c: (-c[0].score, _mask_rank(c[2])))
            if not chosen:
                continue
            best, level, entry, runner = chosen[0]
            reasons = list(best.reasons)
            if len(chosen) > 1:
                second = chosen[1]
                if (second[0].score == best.score
                        and _mask_rank(second[2])[0] == _mask_rank(entry)[0]):
                    level = "medium"
                    reasons.append(f"{second[2].rel} scores the same")
            if runner and level != "high":
                reasons.append(f"next best scores {runner:.2f}")
            project.slots[slot] = {
                **_public(entry), "confidence": level, "score": best.score,
                "reasons": reasons, "user": False, "subset": None,
                "alternatives": [_alternative(c[0], c[2]) for c in chosen[1:]]}

    # Shared tables land where the user has not put one already.
    for path, matches in shared.items():
        entry = by_path[path]
        column = entry.subset.get("column")
        claimed = Counter(value for _project, value in matches)
        landed = 0
        for project, value in matches:
            if "table" in project.slots:
                continue
            level = "high" if claimed[value] == 1 else "medium"
            reasons = [f"rows where {column} == {value}"]
            if claimed[value] > 1:
                reasons.append(f"{value} also matches another image")
            if not entry.inspected:
                level = "medium"
            project.slots["table"] = {
                **_public(entry), "confidence": level,
                "score": WEIGHTS["subset_value"], "reasons": reasons,
                "user": False, "alternatives": [], "shared": True,
                "subset": {"column": column, "value": value}}
            landed += 1
        values = [str(v) for v in entry.subset.get("values") or ()]
        matched = {value for _p, value in matches}
        missing = [v for v in values if v not in matched]
        if missing:
            listed = ", ".join(missing[:5]) + (" …" if len(missing) > 5 else "")
            warnings.append(f"{entry.name}: {len(missing)} of {len(values)} "
                            f"{column} values match no image here ({listed}).")
        if not landed:
            unassigned.append(_unassigned(entry, "table", [], "table"))

    # Channel lists: every project whose image sits in or below the list's folder.
    channel_entries = [e for e in unique if role[e.path] == "channels"
                       and e.path not in user_placed and e.path not in detached]
    used_lists = set()
    for project in projects:
        if project.channels is not None or project.kind == "bundle":
            continue
        best = None
        for entry in channel_entries:
            folder = os.path.dirname(entry.path)
            image_path = project.image.path
            if entry.root != project.image.root:
                continue
            if image_path.startswith(folder + os.sep):
                depth = folder.count(os.sep)
                if best is None or depth > best[0]:
                    best = (depth, entry)
        if best is not None:
            project.channels = _channels_slot(best[1], project)
            used_lists.add(best[1].path)
    for entry in channel_entries:
        if entry.path not in used_lists:
            unassigned.append({**_public(entry), "kind": "channels",
                               "reasons": ["no image sits in or below its folder"],
                               "suggestions": []})

    # Everything else that was found and is not a sample's.
    for entry in unique:
        path = entry.path
        if path in user_placed or path in shared:
            continue
        if path in detached and role[path] in ("image", "mask", "table", "channels"):
            if not any(u["path"] == path for u in unassigned):
                scores = (score_pair(entry, role[path], rules.get(role[path], []))
                          if role[path] in SLOTS else [])
                unassigned.append(_unassigned(entry, role[path], scores, role[path],
                                              note="removed by you"))
            continue
        if role[path] == "layer":
            unassigned.append({
                **_public(entry), "kind": "other",
                "reasons": [f"{entry.detail or 'A layer'}; add it to a sample "
                            "once the dataset exists"],
                "suggestions": []})

    # -- finishing ------------------------------------------------------------------
    any_mask = any("mask" in p.slots or p.has_own("mask") for p in projects)
    any_table = any("table" in p.slots or p.has_own("table") for p in projects)
    # By name, numbers as numbers (Sample2 before Sample10): the order the
    # folder was walked in is nobody's idea of a list.
    # The name the plan would give it, so a rename does not move the row.
    def default_name(project):
        if project.existing:
            return project.existing
        if project.kind == "bundle":
            return project.image.label or project.image.name
        return project.identity.name if project.identity else _stem(project.image.name)

    ordered = sorted(projects, key=lambda p: (_natural(default_name(p)), p.id))
    result_projects = [_project_dict(p, any_mask, any_table) for p in ordered]

    applied = []
    for item in learned:
        count = 0
        for project in result_projects:
            for slot in SLOTS:
                found = project.get(slot) or {}
                if found.get("path") and found["path"] != item["from"] and \
                        any(r == f"follows {item['pattern']}" for r in found.get("reasons", ())):
                    count += 1
        applied.append({"pattern": item["pattern"], "slot": item["slot"],
                        "from": item["from"], "applied_to": count})

    unassigned.sort(key=lambda u: (u["kind"] != "mask", u["kind"] != "table",
                                   u["rel"].lower()))
    statuses = Counter(p["status"] for p in result_projects)
    counts = {
        "projects": len(result_projects),
        "bundles": sum(1 for p in result_projects if p["kind"] == "bundle"),
        "existing": statuses.get("existing", 0),
        "matched": statuses.get("matched", 0),
        "review": statuses.get("review", 0),
        "missing_mask": statuses.get("missing_mask", 0),
        "missing_data": statuses.get("missing_data", 0),
        "image_only": statuses.get("image_only", 0),
        "checking": statuses.get("checking", 0),
        "with_mask": sum(1 for p in result_projects if p.get("mask")),
        "with_table": sum(1 for p in result_projects if p.get("table")),
        "images": sum(1 for e in unique if role[e.path] == "image"),
        "masks": sum(1 for e in unique if role[e.path] == "mask"),
        "tables": sum(1 for e in unique if role[e.path] == "table"),
        "unassigned": len(unassigned),
        "to_create": sum(1 for p in result_projects if not p["existing"]),
        "dirs": inventory.counts.get("dirs", 0),
        "files": inventory.counts.get("files", 0),
        "skipped_dirs": len(inventory.skipped),
    }
    counts["attention"] = counts["review"] + counts["missing_mask"] + counts["missing_data"]
    if inventory.budget_hit:
        hit = inventory.budget_hit
        warnings.append(
            f"Stopped after {hit['limit']:,} entries; folders past "
            f"{hit['at']} were not read." if hit["reason"] == "entries" else
            f"Folders more than {hit['limit']} levels down were not read "
            f"(first: {hit['at']}).")
    if inventory.errors:
        warnings.append(f"{len(inventory.errors)} folder"
                        f"{'s' if len(inventory.errors) != 1 else ''} could not be read.")
    if duplicates:
        warnings.append(f"{len(duplicates)} duplicate file"
                        f"{'s' if len(duplicates) != 1 else ''} counted once.")

    from plexora.server.models.dataset_scan import SCAN_MAX_DEPTH, SCAN_MAX_ENTRIES

    return {
        "roots": list(inventory.roots),
        "projects": result_projects,
        "unassigned": unassigned,
        "unrecognised": unrecognised,
        "duplicates": duplicates,
        "excluded": excluded,
        "unavailable": [],
        "errors": list(inventory.errors),
        "skipped": list(inventory.skipped),
        "counts": counts,
        "warnings": warnings,
        "conventions": {
            "layout": _layout(projects),
            "mask_pattern": conventions.get("mask"),
            "table_pattern": conventions.get("table"),
            "learned": applied,
        },
        "budget": {"hit": inventory.budget_hit, "max_depth": SCAN_MAX_DEPTH,
                   "max_entries": SCAN_MAX_ENTRIES},
    }


def _natural(text):
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part)
                 for part in re.findall(r"\d+|\D+", str(text).lower()))


class OrderedDictList:
    """Insertion-ordered `{key: [values]}`."""

    def __init__(self):
        self._data = {}

    def add(self, key, value):
        self._data.setdefault(key, []).append(value)

    def items(self):
        return self._data.items()


def _is_project_image(entry, projects):
    return any(project.image is entry for project in projects)


def _mask_corroborated(entry, project):
    """Whether a file whose pixels could be an image or a mask is a mask by
    everything else about it: a name or folder that says so, and exactly the
    size of the image it goes with."""
    size = entry.size_wh()
    if not size or size != project.image.size_wh():
        return False
    words = set(_tokens(entry.rel.lower()))
    return bool(words & MASK_WORDS)


def _alternative(score, entry):
    return {**_public(entry), "score": score.score, "reasons": list(score.reasons)}


def _unassigned(entry, slot, scores, role, note=None):
    reasons = [note] if note else []
    vetoes = [s for s in scores if s.veto]
    live = [s for s in scores if not s.veto and s.score > 0]
    if not live and vetoes:
        reasons.append("a different size from every image it could belong to")
    elif not live:
        reasons.append("its name matches no image here")
    elif len(live) > 1 and live[0].score - live[1].score < MEDIUM[1]:
        reasons.append(f"matches {live[0].project.name} and "
                       f"{live[1].project.name} equally well")
    else:
        reasons.append("the match is too weak to apply on its own")
    if slot == "table" and entry.columns and not entry.name.lower().endswith(".h5ad"):
        column = _image_id_column(entry.columns)
        if column:
            reasons.append(f"has an `{column}` column, but CSV rows are not "
                           "filtered by it: assign it to the one project it "
                           "belongs to")
    if entry.ambiguous and entry.inspected:
        reasons.append("its pixels could be an image or a mask")
    return {**_public(entry), "kind": role if role in ("image", "mask", "table",
                                                       "channels") else "other",
            "reasons": reasons,
            "suggestions": [{"project": s.project.id, "name": s.project.name,
                             "score": s.score, "reasons": list(s.reasons)}
                            for s in live[:3]]}


def _image_id_column(columns):
    from plexora.server.models.adapters.classify import is_likely_image_identifier_name

    for column in columns:
        try:
            if is_likely_image_identifier_name(column):
                return column
        except Exception:  # noqa: BLE001
            continue
    return None


def _image_names(project):
    from plexora.server.models.import_proposal import _ExactName

    image = project.image
    names = [project.name]
    if project.identity is not None:
        names.append(project.identity.name)
    names.append(_stem(image.name))
    names.append(_ExactName(Path(image.path).parent.name))
    if image.ome_name:
        names.append(image.ome_name)
    return [name for name in names if name]


def _subset_value(entry, project):
    from plexora.server.models.import_proposal import subset_match

    values = entry.subset.get("values") or ()
    return subset_match(values, _image_names(project))


def _subset_matches(entry, projects):
    """`[(project, value)]` for every project the table's image column names."""
    found = []
    for project in projects:
        if project.kind == "bundle":
            continue
        value = _subset_value(entry, project)
        if value is not None:
            found.append((project, value))
    return found


def _channels_slot(entry, project, user=False):
    count = project.image.channel_count()
    names = _panel_names(entry, count) if count else None
    slot = {**_public(entry), "confidence": "user" if user else "high",
            "reasons": ["chosen by you"] if user else
            ["in a folder above the image"], "names": names, "user": user}
    if count and names is None:
        slot["warning"] = (f"{entry.name} does not list {count} names; the image "
                           "keeps its own channel names.")
    return slot


def _project_dict(project, any_mask, any_table):
    image = project.image
    checking = not image.inspected and not getattr(image, "budgeted", False)
    slots = {slot: project.slots.get(slot) for slot in SLOTS}
    for slot in SLOTS:
        if slots[slot] is None and project.has_own(slot):
            slots[slot] = {"path": None, "rel": None, "name": "",
                           "label": "In the run", "detail": "", "size": "",
                           "confidence": "high", "score": None, "user": False,
                           "reasons": [f"the {_bundle_word(image)} holds its own"],
                           "alternatives": [], "bundled": True, "subset": None}
    warnings = list(project.warnings)
    if project.channels and project.channels.get("warning"):
        warnings.append(project.channels["warning"])
    review = (project.image_confidence == "medium"
              or any(s and s.get("confidence") == "medium" for s in slots.values()))
    if project.existing:
        status = "existing"
    elif checking:
        status = "checking"
    elif review:
        status = "review"
    elif slots["mask"] is None and any_mask:
        status = "missing_mask"
    elif slots["table"] is None and any_table:
        status = "missing_data"
    elif slots["mask"] is None and slots["table"] is None:
        status = "image_only"
    else:
        status = "matched"
    return {
        "id": project.id,
        "name": project.name,
        "kind": project.kind,
        "status": status,
        "existing": project.existing,
        "identity": ({"key": project.identity.text, "source": project.identity.source,
                      "evidence": project.identity.evidence}
                     if project.identity else None),
        "image": {**_public(image), "confidence": project.image_confidence,
                  "reasons": list(project.image_reasons),
                  "alternatives": list(project.alternatives),
                  "modality": image.modality,
                  "format": image.format,
                  "layers": [dict(layer) for layer in (image.layers or ())]},
        "mask": slots["mask"],
        "table": slots["table"],
        "channels": project.channels,
        "warnings": warnings,
    }


def _bundle_word(entry):
    from plexora.server.utils import dataset_marks

    return dataset_marks.TYPE_LABELS.get(entry.format, "run").lower() if entry.format else "run"


def _layout(projects):
    if not projects:
        return None
    total = len(projects)
    by_folder = sum(1 for p in projects if p.identity and p.identity.source == "folder"
                    or len(p.keys) > 1)
    in_role_folders = sum(1 for p in projects if _role_folder_of(p.image, "image")
                          or any(dir_role(part) for part in
                                 PurePosixPath(p.image.rel).parts[:-1]))
    if by_folder * 2 > total:
        return "per-sample"
    if in_role_folders * 2 > total:
        return "role-folders"
    if by_folder or in_role_folders:
        return "mixed"
    return "flat"


# -- after the plan ------------------------------------------------------------------


def verify(result) -> dict:
    """`result` with every file that has since disappeared taken out.

    A vanished mask or table is cleared from its project with a warning; a
    vanished image makes its project `unavailable`. Nothing else changes.
    """
    result = copy.deepcopy(result)
    unavailable = []
    for project in result["projects"]:
        image = project["image"]
        if image.get("path") and not os.path.exists(image["path"]):
            project["status"] = "unavailable"
            unavailable.append({"path": image["path"], "project": project["id"],
                                "slot": "image"})
            continue
        for slot in ("mask", "table", "channels"):
            found = project.get(slot)
            if found and found.get("path") and not os.path.exists(found["path"]):
                unavailable.append({"path": found["path"], "project": project["id"],
                                    "slot": slot})
                project["warnings"].append(f"{found['name']} is no longer there.")
                project[slot] = None
    result["unavailable"] = unavailable
    result["counts"]["unavailable"] = sum(1 for u in unavailable if u["slot"] == "image")
    return result

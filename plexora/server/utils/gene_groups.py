"""Gene groups read out of a table the user already has.

Backs each layer plugin's own `POST /plugins/<name>/groups` -- the Transcripts
layer's and the Visium HD bin layer's -- which answer the gene-group dialog
both panels open (client/src/js/views/geneGroupModal.js). Core's, like the
dialog, because the two plugins would otherwise hold two copies of one parse
and drift; what each plugin still owns is its VOCABULARY, which it reads from
its own store and hands in.

THE FILE IS PARSED HERE AND NOT IN THE BROWSER, for the reason
`/upload_channels` gives: an .xlsx is a zip full of XML, a CSV's delimiter has
to be sniffed, and a browser that got either subtly wrong would report a group
with the wrong genes in it rather than an error. Core already owns that
reading (`channel_file.read_grid`) and this is the same read.

The shape is the one Xenium Explorer's own import takes, because that is what
people already have: the FIRST column is a gene and every column after it is a
group that gene belongs to. Blank cells are skipped, so a gene in one group and
a gene in three are the same file.
"""

from __future__ import annotations

from pathlib import Path

from plexora.server.utils import channel_file

#: The first column's header, when the file has one. Recognised rather than
#: required: a two-column CSV with no header is the commonest thing somebody
#: exports out of a spreadsheet, and refusing it would be refusing the file
#: this exists to take.
GENE_HEADERS = ("gene", "genes", "feature", "feature_name", "target", "name")


def read_upload(files, form, trim=lambda raw: raw):
    """`(data, path, filename)` for the file the user chose, however chosen.

    The two ways in `/upload_channels` takes, for the same reason: on a
    cluster the browser is on a laptop and the list is beside the image on the
    remote filesystem, so there is nothing local to upload. Raises
    `channel_file.ChannelFileError` with a sentence the dialog can show.
    """
    upload = files.get("file")
    if upload is not None and upload.filename:
        return upload.read(), None, upload.filename

    raw = (form.get("path") or "").strip()
    if not raw:
        raise channel_file.ChannelFileError(
            "Choose a file, or paste the path to one.")
    path = Path(trim(raw)).expanduser()
    if not path.is_file():
        raise channel_file.ChannelFileError(f"There is no file at {raw}")
    return None, path, path.name


def groups_from_grid(grid, vocabulary):
    """`([(group, [gene, ...])], [unknown gene, ...])` from the parsed rows.

    `vocabulary` maps an UPPER-CASED name to the layer's own spelling.

    Order is the file's, both for the groups and for the genes inside them: a
    list somebody curated has an order, and sorting it alphabetically here
    would throw away the only thing the file said about priority.

    Names are matched case-insensitively and given back in the PANEL's
    spelling, because that is what every other part of a layer keys on -- a
    group holding "epcam" would draw nothing and look like an empty group.
    """
    rows = [row for row in (grid or []) if any(str(cell).strip() for cell in row)]
    if not rows:
        return [], []
    first = str(rows[0][0]).strip().lower() if rows[0] else ""
    if first in GENE_HEADERS:
        rows = rows[1:]

    order = []
    members = {}
    unknown = []
    seen_unknown = set()
    for row in rows:
        cells = [str(cell).strip() for cell in row]
        if not cells or not cells[0]:
            continue
        gene = vocabulary.get(cells[0].upper())
        if gene is None:
            if cells[0].upper() not in seen_unknown:
                seen_unknown.add(cells[0].upper())
                unknown.append(cells[0])
            continue
        for label in cells[1:]:
            if not label:
                continue
            if label not in members:
                members[label] = []
                order.append(label)
            if gene not in members[label]:
                members[label].append(gene)
    return [(name, members[name]) for name in order], unknown


def vocabulary_of(names):
    """UPPER-CASED name -> the layer's own spelling, for `groups_from_grid`."""
    return {str(name).strip().upper(): str(name) for name in (names or [])}


def answer(files, form, names, trim=lambda raw: raw):
    """The whole route: `(body, status)` for a group file against `names`.

    Answers with the groups it found and, separately, the names this layer's
    panel has never heard of. Separately and not as an error: a marker list
    written for a bigger panel is the ordinary case, and importing the twenty
    genes that ARE here is what somebody wants -- but silently dropping the
    rest would be the import quietly doing less than it said.
    """
    try:
        data, path, filename = read_upload(files, form, trim)
        grid = channel_file.read_grid(data=data, path=path, filename=filename)
    except channel_file.ChannelFileError as error:
        return {"error": str(error)}, 400
    found, unknown = groups_from_grid(grid, vocabulary_of(names))
    return {
        "groups": [{"name": name, "genes": genes} for name, genes in found],
        "unknown": unknown,
        "filename": filename,
    }, 200

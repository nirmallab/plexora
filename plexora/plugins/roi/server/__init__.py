"""Server half of the ROI plugin.

`routes` is the Flask surface, `repository` owns persistence and the revision
check, `operations` applies one edit to the state, `geometry`/`schema` say what
a valid annotation is, and `geojson`/`adapters` are the export formats. Nothing
here imports `data_model` -- everything about the project comes through
`plexora.api`.
"""

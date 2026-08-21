"""Cell Explorer's server half.

Four modules, one job each: `variables` decides what can be coloured by and how,
`values` moves one column's values to the browser, `state` remembers display
preferences, and `routes` is the thin HTTP surface over the three.

Everything reads through `plexora.api` and nothing else. That is not a style
rule -- `data_model` keeps the loaded table in module globals under a load lock
with two adjacent loaders whose names differ by one underscore, and the api
handles call the right one. See plexora/api/__init__.py.
"""

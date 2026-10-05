"""Image models that run beside the viewer: today, magic select.

`sam` owns the model (status, one-time setup, inference), `segment` turns a
project view and a few clicks into a polygon, `sam_weights` fetches and
verifies the files, `sam_backend` runs them on ONNX Runtime (on a GPU when
one is usable). Nothing in this package imports a plugin.
"""

"""Plexora on the website: what its Python does when it runs in the page.

On https://viewer.plexoraapp.com the same Flask app the desktop runs is built
inside Pyodide, in a Web Worker beside the viewer, and the page reaches it
through a request bridge instead of a socket. This package holds what that
needs and nothing the desktop, a server or a node ever imports: which routes
the page answers itself and which go to Python (`routes`), and in later
phases the worker's boot, the bridge and the cooperative job runner.
"""

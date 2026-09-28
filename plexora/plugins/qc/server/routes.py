"""The QC panel's routes, mounted under /plugins/qc/."""

from __future__ import annotations

from flask import Blueprint

qc_bp = Blueprint("qc", __name__, template_folder="../templates",
                  static_folder="../static", static_url_path="/static")

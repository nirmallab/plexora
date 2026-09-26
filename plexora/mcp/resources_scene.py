"""Scene views as resources, and the job URI reserved for long-running work."""

from __future__ import annotations

import json

from plexora.mcp import serialize


def register(server, runtime, resource, answer):
    def section(name, key):
        text = answer(runtime, "project.scene", {"project": name})
        data = json.loads(text)
        if "error" in data:
            return text
        return serialize.bound({"project": name, key: data.get(key, [])})

    @resource("plexora://project/{name}/scene", "project-scene",
              "The sample as assets, coordinate systems, entity sets and feature spaces.")
    def scene(name: str) -> str:
        return answer(runtime, "project.scene", {"project": name})

    @resource("plexora://project/{name}/assets", "project-assets", "Spatial assets.")
    def assets(name: str) -> str:
        return section(name, "assets")

    @resource("plexora://project/{name}/coordinate-systems", "project-coordinate-systems",
              "Coordinate systems and the transforms between them.")
    def systems(name: str) -> str:
        text = answer(runtime, "project.scene", {"project": name})
        data = json.loads(text)
        if "error" in data:
            return text
        return serialize.bound({"project": name,
                                "coordinate_systems": data["coordinate_systems"],
                                "transforms": data["transforms"]})

    @resource("plexora://project/{name}/entity-sets", "project-entity-sets",
              "Cells/spots and where their table is.")
    def entities(name: str) -> str:
        return section(name, "entity_sets")

    @resource("plexora://project/{name}/feature-spaces", "project-feature-spaces",
              "Markers and metadata measured on each entity set.")
    def features(name: str) -> str:
        return section(name, "feature_spaces")

    @resource("plexora://job/{job_id}", "job", "A long-running job (reserved).")
    def job(job_id: str) -> str:
        return serialize.bound({"error": {
            "code": "capability_unavailable",
            "message": "no capability in this release runs as a job; every call is "
                       "immediate", "detail": {"job_id": job_id}, "retryable": False}})

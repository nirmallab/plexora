"""`plexora://artifact/{id}`: a stored render's manifest (the image is fetched
with the `get_artifact` tool, which returns it as image content)."""

from __future__ import annotations

from plexora.mcp import serialize


def register(server, runtime, resource, answer):
    @resource("plexora://artifact/{artifact_id}", "artifact",
              "A stored render or capture: its manifest and where the PNG is.")
    def artifact(artifact_id: str) -> str:
        return answer(runtime, "artifact.get", {"artifact_id": artifact_id,
                                                "include_image": False})

    @resource("plexora://artifacts", "artifacts", "Recent renders and captures.")
    def artifacts_index() -> str:
        return answer(runtime, "artifact.list", {"limit": 50})

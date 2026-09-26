"""Read-only views as MCP resources, under `plexora://`.

Each one is a capability call underneath (through the same `invoke`), so a
resource and the matching tool can never disagree. Resources exist for the
clients that attach context by URI rather than by calling tools, and so a
receipt can cite where its before/after state can be read.
"""

from __future__ import annotations

from plexora.mcp import serialize

JSON = "application/json"


def _answer(runtime, capability, arguments):
    from plexora.agent import registry

    try:
        registry.get(capability)
    except Exception:
        return serialize.bound({"error": {"code": "capability_unavailable",
                                          "message": f"{capability} is not loaded in "
                                                     "this server", "detail": None,
                                          "retryable": False}})
    outcome = runtime.invoke(capability, arguments)
    if not outcome["ok"]:
        return serialize.bound({"error": outcome["error"]})
    result = outcome["result"]
    if isinstance(result, dict):
        result.pop("_images", None)
    return serialize.bound(result)


def register(server, runtime):
    """Add every resource to `server`."""

    def resource(uri, name, description, mime_type=JSON):
        def decorate(fn):
            server.resource(uri, name=name, description=description,
                            mime_type=mime_type)(fn)
            return fn
        return decorate

    @resource("plexora://projects", "projects", "Every project, with what it has.")
    def projects() -> str:
        return _answer(runtime, "project.list", {"limit": 200})

    @resource("plexora://datasets", "datasets", "Every dataset and its projects.")
    def datasets() -> str:
        return _answer(runtime, "dataset.list", {})

    @resource("plexora://dataset/{dataset}", "dataset", "One dataset's projects.")
    def dataset(dataset: str) -> str:
        return _answer(runtime, "dataset.get", {"dataset": dataset})

    @resource("plexora://project/{name}", "project",
              "One project: image, channels, pixel size, segmentation, table roles.")
    def project(name: str) -> str:
        return _answer(runtime, "project.inspect", {"project": name, "include_table": False})

    @resource("plexora://project/{name}/manifest", "project-manifest",
              "What the project was told and what is still open.")
    def project_manifest(name: str) -> str:
        try:
            from plexora import datasets as datasets_api

            return serialize.bound(datasets_api.project_manifest(name))
        except KeyError as exc:
            return serialize.bound({"error": {"code": "unknown_project",
                                              "message": str(exc.args[0]),
                                              "detail": None, "retryable": False}})

    @resource("plexora://project/{name}/layers", "project-layers",
              "Every layer of the sample, synthesized ones included.")
    def project_layers(name: str) -> str:
        try:
            from plexora.server.models import manifest

            record = runtime.session.project(name)
            return serialize.bound({"project": name, "layers": manifest.layers(record)})
        except Exception as exc:
            from plexora.agent.errors import as_agent_error

            return serialize.bound({"error": as_agent_error(exc).to_problem()})

    @resource("plexora://project/{name}/markers", "project-markers",
              "Marker and metadata columns and the column roles.")
    def project_markers(name: str) -> str:
        return _answer(runtime, "table.markers", {"project": name})

    @resource("plexora://project/{name}/gates", "project-gates",
              "Every marker's stored gate.")
    def project_gates(name: str) -> str:
        return _answer(runtime, "gating.get_all", {"project": name})

    @resource("plexora://project/{name}/rois", "project-rois", "Every region drawn.")
    def project_rois(name: str) -> str:
        return _answer(runtime, "roi.list", {"project": name})

    @resource("plexora://project/{name}/resource-status", "project-resource-status",
              "Where the image, mask and table are and whether each can be read.")
    def project_resource_status(name: str) -> str:
        return _answer(runtime, "resource.status", {"project": name})

    @resource("plexora://skills", "skills", "The scientific skills.")
    def skills_index() -> str:
        from plexora.ai import skills

        return serialize.bound({"skills": skills.list_skills()})

    @resource("plexora://skill/{name}", "skill", "One skill's instructions.",
              mime_type="text/markdown")
    def skill(name: str) -> str:
        from plexora.ai import skills

        try:
            return skills.read_skill(name)
        except KeyError as exc:
            return str(exc.args[0])

    @resource("plexora://audit/recent", "audit", "The last 50 mutations attempted.")
    def audit_recent() -> str:
        return serialize.bound({"path": str(runtime.audit.path),
                                "entries": runtime.audit.tail(50)})

    # Later milestones add their own: artifacts, scene views, jobs.
    for extra in ("plexora.mcp.resources_visual", "plexora.mcp.resources_scene"):
        try:
            module = __import__(extra, fromlist=["register"])
        except ImportError:
            continue
        module.register(server, runtime, resource, _answer)

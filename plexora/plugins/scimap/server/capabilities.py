"""SCIMAP as agent capabilities: Plexora's AI drives the same notebook a person does.

Two owners:

* `scimap` -- the notebook: list and open notebooks, add a step, run it, read
  its result, show it in the viewer. Same functions as the page's buttons
  (server/notebooks.py), so an agent's step is an ordinary step the user sees,
  can rerun and can export.
* `scimappro` -- every Scimap Pro function by itself (`scimappro.<ns>.<fn>`,
  tool name `scimappro_<ns>_<snake_fn>`), on a project's own AnnData table,
  plus `scimappro.search/describe/run/drop_result`. Moved here from the
  retired `scimappro_plexora` package with the same names, so MCP profiles
  and task routing that name them keep working. Built from Scimap Pro's
  `registry.json` without importing it.

Every run, of either owner, goes through the one runner queue: an agent and a
person never run two Scimap Pro analyses at once.
"""

from __future__ import annotations

from typing import Any

from plexora.plugins.scimap.server import catalog

OWNER = "scimappro"
NOTEBOOK_OWNER = "scimap"
#: How long an agent's call waits for its run before answering "still running".
WAIT_SECONDS = 600


def _error(code, message, detail=None):
    from plexora.agent.errors import AgentError

    return AgentError(code, message, detail=detail)


def _data(inp):
    return inp.model_dump(exclude_none=True) if hasattr(inp, "model_dump") else dict(inp)


def _base():
    try:
        from plexora.agent.schemas import AgentModel as Base
    except ImportError:  # pragma: no cover
        from pydantic import BaseModel, ConfigDict

        class Base(BaseModel):
            model_config = ConfigDict(extra="forbid")
    return Base


# -- scimappro.<ns>.<fn> on a project's own table -------------------------


def _project_table(project, session=None):
    from plexora import api

    try:
        info = api.project_info(project)
    except KeyError:
        info = {}
    if not info.get("src"):
        raise _error("precondition_missing", f"project {project!r} has no cell table; add one first",
                     detail={"missing": ["table"]})
    subset = dict(info.get("subset") or {})
    return str(info["src"]), subset.get("column"), subset.get("value")


def _run_on_project(project, key, params, call=None):
    import inspect

    from plexora.plugins.scimap.server import adapter, runner

    path, column, value = _project_table(project, getattr(call, "session", None))
    params = {k: v for k, v in dict(params or {}).items()
              if v is not None and k not in ("project", "expected_revision", "confirm")}

    def work(job):
        sm = adapter.scimappro()
        fn = adapter.function_for(sm, key)
        signature = inspect.signature(inspect.unwrap(fn)).parameters
        kwargs = dict(params)
        if column and "imageId" in signature:
            kwargs.setdefault("imageId", column)
        if value is not None and "subset" in signature:
            kwargs.setdefault("subset", str(value))
        if key.startswith("pl."):
            for knob, setting in (("show", False), ("interactive", False), ("returnData", True)):
                if knob in signature:
                    kwargs[knob] = setting
        label = kwargs.get("label")
        progress = getattr(call, "progress", None)
        say = (lambda message: (job.say(message), progress and progress(message=message)))
        return adapter.execute(path, key, kwargs, label=label, say=say)

    job = runner.submit(runner.Job("agent", work, label=key))
    answer = runner.wait(job.id, WAIT_SECONDS)
    if answer["status"] == "done":
        return {"function": key, **(job.result or {})}
    if answer["status"] == "failed":
        error = answer.get("error") or {}
        raise _error(error.get("code") or "internal_error", error.get("message") or "the run failed",
                     detail=error.get("detail"))
    raise _error("conflict", f"{key} is still running; it will finish in the background.")


def _input_model(key, record):
    from pydantic import Field, create_model

    fields: dict[str, Any] = {
        "project": (str, Field(description="The Plexora project whose table to analyse.")),
        "label": (str | None, Field(None, description="Name the result is stored under.")),
        "expected_revision": (int | None, Field(None, description="Refuse if the table changed.")),
        "confirm": (bool, Field(False, description="Confirm writing into the source table.")),
    }
    for name, required, description, _default in catalog.parameters(record):
        if name in fields:
            continue
        fields[name] = (Any, Field(description=description)) if required else \
            (Any, Field(None, description=description))
    model = "Scimappro" + "".join(p.capitalize() for p in key.replace(".", "_").split("_"))
    return create_model(model, __base__=_base(), **fields)


def _handler_for(key):
    def handler(call, inp):
        data = _data(inp)
        project = data.get("project") or getattr(call, "project_name", None)
        result = _run_on_project(project, key, data, call)
        if not key.startswith("pl.") and getattr(call, "notify", None):
            try:
                call.notify(project, "core", "dataset.changed",
                            {"sections": [s.lstrip("~") for s in result.get("outputs") or []]})
            except Exception:  # noqa: BLE001
                pass
        return result
    handler.__name__ = f"run_{key.replace('.', '_')}"
    return handler


def function_specs():
    out = []
    version = str(catalog.load().get("version") or "1")
    for key, record in catalog.offered().items():
        namespace = record["ns"]
        tags = tuple(t for t in (namespace, record.get("stage"), record.get("group"),
                                 *(record.get("synonyms") or []), *(record.get("roles") or [])) if t)
        common = dict(name=f"scimappro.{key}", tool_name=catalog.toolName(key), owner=OWNER,
                      purpose=catalog.purpose(record), input_model=_input_model(key, record),
                      handler=_handler_for(key), tags=tags, version=version, execution="job",
                      streams_progress=True, remote_safe=False, reads=("table",))
        if namespace == "pl":
            out.append({**common, "permission": "read", "visual_output": True,
                        "egress": "aggregates", "reversible": True, "persistent": False})
        else:
            out.append({**common, "permission": "source_file_write", "source_file_write": True,
                        "persistent": True, "reversible": False, "egress": "aggregates",
                        "writes": ("table",)})
    return out


def meta_specs():
    from pydantic import BaseModel, ConfigDict, Field

    class SearchInput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        query: str = Field(description="The question, in plain words.")
        limit: int = 8

    class DescribeInput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        key: str = Field(description="`ns.fn`, from scimappro.search.")

    class RunInput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        project: str
        function: str = Field(description="`ns.fn`, from scimappro.search.")
        params: dict[str, Any] = Field(default_factory=dict)
        label: str | None = None
        expected_revision: int | None = None
        confirm: bool = False

    class DropInput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        project: str
        label: str
        confirm: bool = False

    def search(call, inp):
        data = _data(inp)
        return {"hits": catalog.search(data.get("query", ""), limit=data.get("limit") or 8)}

    def describe(call, inp):
        key = _data(inp).get("key")
        record = catalog.offered().get(key)
        if record is None:
            raise _error("invalid_input", f"no function {key!r}")
        return {"key": key, "tool": catalog.toolName(key), "summary": record.get("summary"),
                "use_when": record.get("use_when"), "not_when": record.get("not_when"),
                "experimental_unit": record.get("experimental_unit"), "requires": record.get("requires"),
                "parameters": [{"name": n, "required": r, "description": d, "default": v}
                               for n, r, d, v in catalog.parameters(record)]}

    def run(call, inp):
        data = _data(inp)
        params = dict(data.get("params") or {})
        if data.get("label"):
            params["label"] = data["label"]
        if data["function"] not in catalog.offered():
            raise _error("invalid_input", f"Scimap Pro offers no function {data['function']!r} here")
        return _handler_for(data["function"])(call, {"project": data["project"], **params})

    def drop(call, inp):
        from plexora.plugins.scimap.server import adapter, runner

        data = _data(inp)
        path, _, _ = _project_table(data["project"], getattr(call, "session", None))
        job = runner.submit(runner.Job("drop", lambda job: adapter.drop_result(path, data["label"]),
                                       label=f"drop {data['label']}"))
        answer = runner.wait(job.id, WAIT_SECONDS)
        if answer["status"] != "done":
            raise _error("internal_error", (answer.get("error") or {}).get("message") or "not dropped")
        return {"project": data["project"], "label": data["label"], "dropped": job.result}

    meta = dict(owner=OWNER, version="1", tags=("scimappro", "analysis"))
    return [
        {**meta, "name": "scimappro.search", "tool_name": "scimappro_search_functions",
         "purpose": "Find the SCIMAP Pro function for a question about the cell table "
                    "(neighbourhoods, phenotypes, cohort statistics).",
         "permission": "read", "input_model": SearchInput, "handler": search},
        {**meta, "name": "scimappro.describe", "tool_name": "scimappro_describe_function",
         "purpose": "What one SCIMAP Pro function is for, what it needs, its parameters and the "
                    "experimental unit its result carries.",
         "permission": "read", "input_model": DescribeInput, "handler": describe},
        {**meta, "name": "scimappro.run", "tool_name": "scimappro_run",
         "purpose": "Run any SCIMAP Pro function on this project's table (writes the result into "
                    "the table).",
         "permission": "source_file_write", "source_file_write": True, "persistent": True,
         "reversible": False, "execution": "job", "streams_progress": True, "egress": "aggregates",
         "remote_safe": False, "input_model": RunInput, "handler": run},
        {**meta, "name": "scimappro.drop_result", "tool_name": "scimappro_drop_result",
         "purpose": "Remove one SCIMAP Pro result from the table: its columns, its records and its "
                    "ledger entry. The undo for a SCIMAP Pro run.",
         "permission": "destructive", "source_file_write": True, "persistent": True,
         "reversible": False, "remote_safe": False, "input_model": DropInput, "handler": drop},
    ]


# -- scimap.*: the notebook ----------------------------------------------


def notebook_specs():
    from pydantic import Field

    from plexora.plugins.scimap.server import notebooks, store, tools

    Base = _base()

    class Empty(Base):
        pass

    class ContextInput(Base):
        kind: str = Field(description='"project" or "dataset".')
        ref: str = Field(description="The project name or the dataset id.")
        title: str | None = None

    class NotebookInput(Base):
        notebook_id: str

    class AddInput(Base):
        notebook_id: str
        tool: str = Field(description="A tool id from scimap.tools.list.")
        params: dict[str, Any] = Field(default_factory=dict)
        after: str | None = None

    class CellInput(Base):
        notebook_id: str
        cell_id: str

    class RunInput(Base):
        notebook_id: str
        cell_id: str
        wait: bool = True
        confirm: bool = False

    class ShowInput(Base):
        notebook_id: str
        cell_id: str
        focus: list[str] | None = None

    def tools_list(call, inp):
        return {"categories": [{"id": c["id"], "title": c["title"],
                                "tools": [{"id": t["id"], "name": t["name"], "summary": t["summary"],
                                           "fields": [{k: f.get(k) for k in ("name", "label", "kind", "default", "choices")}
                                                      for f in t["fields"]]}
                                          for t in c["tools"]]}
                               for c in tools.describe()]}

    def list_notebooks(call, inp):
        return {"notebooks": store.list_all()}

    def create(call, inp):
        data = _data(inp)
        document = notebooks.create(data["kind"], data["ref"], data.get("title"))
        return {"notebook_id": document["id"], "title": document["title"]}

    def get(call, inp):
        view = notebooks.view(_data(inp)["notebook_id"])
        nb = view["notebook"]
        return {"title": nb["title"], "context": nb["context"],
                "working": {k: view["working"].get(k) for k in ("exists", "file", "n_cells", "markers", "images")},
                "steps": [{"cell_id": c["id"], "tool": c["tool"], "state": c["state"],
                           "settings": c.get("chips"), "stale": c.get("stale")} for c in nb["cells"]],
                "available": {k: v for k, v in view["availability"].items() if v.get("enabled")}}

    def add(call, inp):
        data = _data(inp)
        op = {"op": "add_cell", "tool": data["tool"], "params": data.get("params") or {}}
        if data.get("after"):
            op["after"] = data["after"]
        notebooks.apply_ops(data["notebook_id"], [op])
        return {"cell_id": op.get("_created")}

    def run(call, inp):
        from plexora.plugins.scimap.server import runner

        data = _data(inp)
        queued = notebooks.run_cell(data["notebook_id"], data["cell_id"], initiated_by="agent")
        if not data.get("wait", True):
            return queued
        answer = runner.wait(queued["job_id"], WAIT_SECONDS)
        out = {**queued, "status": answer["status"] if answer else "unknown"}
        if answer and answer["status"] == "failed":
            out["error"] = answer.get("error")
        if answer and answer["status"] == "done":
            out["summary"] = _brief(notebooks.summary(data["notebook_id"], data["cell_id"]))
        return out

    def result(call, inp):
        data = _data(inp)
        return _brief(notebooks.summary(data["notebook_id"], data["cell_id"]))

    def show(call, inp):
        data = _data(inp)
        return notebooks.show_in_viewer(data["notebook_id"], data["cell_id"], focus=data.get("focus"))

    common = dict(owner=NOTEBOOK_OWNER, version="1", tags=("scimap", "notebook", "analysis"),
                  remote_safe=False)
    return [
        {**common, "name": "scimap.tools.list", "tool_name": "scimap_list_tools",
         "purpose": "The SCIMAP notebook's analysis tools and the settings each takes.",
         "permission": "read", "input_model": Empty, "handler": tools_list},
        {**common, "name": "scimap.notebooks.list", "tool_name": "scimap_list_notebooks",
         "purpose": "Every SCIMAP notebook on this computer, with its project or dataset.",
         "permission": "read", "input_model": Empty, "handler": list_notebooks},
        {**common, "name": "scimap.notebook.create", "tool_name": "scimap_create_notebook",
         "purpose": "Start a SCIMAP notebook for a project or a dataset.",
         "permission": "reversible_write", "persistent": True, "input_model": ContextInput, "handler": create},
        {**common, "name": "scimap.notebook.get", "tool_name": "scimap_get_notebook",
         "purpose": "A notebook's steps, their states and settings, and which tools can run.",
         "permission": "read", "input_model": NotebookInput, "handler": get},
        {**common, "name": "scimap.cell.add", "tool_name": "scimap_add_step",
         "purpose": "Add an analysis step to a notebook (it does not run until scimap.cell.run).",
         "permission": "reversible_write", "persistent": True, "input_model": AddInput, "handler": add},
        {**common, "name": "scimap.cell.run", "tool_name": "scimap_run_step",
         "purpose": "Run one notebook step with Scimap Pro and return its summary. Writes the result "
                    "into the notebook's working file.",
         "permission": "source_file_write", "source_file_write": True, "persistent": True,
         "reversible": False, "execution": "job", "egress": "aggregates", "input_model": RunInput,
         "handler": run},
        {**common, "name": "scimap.result.summary", "tool_name": "scimap_step_result",
         "purpose": "A completed step's result: group sizes, tables, and its experimental unit.",
         "permission": "read", "egress": "aggregates", "input_model": CellInput, "handler": result},
        {**common, "name": "scimap.result.show", "tool_name": "scimap_show_step",
         "purpose": "Colour the image viewer's cells by a step's result (optionally a few groups).",
         "permission": "reversible_write", "input_model": ShowInput, "handler": show},
    ]


def _brief(body):
    """A summary small enough for a model: no per-cell arrays."""
    drop = {"x", "y", "codes", "values"}
    return {k: v for k, v in (body or {}).items() if k not in drop}


def capabilities():
    from plexora.agent.registry import Capability

    specs = notebook_specs()
    if catalog.registryPath() is not None:
        specs += function_specs() + meta_specs()
    return [Capability(**spec) for spec in specs]

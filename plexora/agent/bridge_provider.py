"""Plexora as a peer on the shared protocol: the `Adapter` spatialbridge calls.

SCIMAP Pro and Plexora share one dataset through spatialbridge (an optional
extra). The protocol's bridge tools and its hand-off engine call only the
methods of an `Adapter`; this is Plexora's, and every method goes through the
capability registry, so a call from SCIMAP Pro leaves the receipt, the audit
line and the policy check an agent's call would:

- `capabilities()` -- the registered capabilities as descriptors, with roles
  (`bridge_roles`), filtered by what this connection's policy permits;
- `invoke()` -- `registry.invoke` with this connection's policy and origin;
- `job()` -- a job's state from the agent job store;
- `bind()` -- `find_project_for_table`, else `bind_project`;
- `collect()` -- gates, regions, QC and selections as the protocol's objects;
- `on_event()` -- `dataset_events.on_dataset_changed` for every project bound
  to the event's workspace.

Imported only when the bridge is used: importing it imports spatialbridge,
and `import plexora` must never pay for that.
"""

from __future__ import annotations

from spatialbridge.adapter import Adapter
from spatialbridge.errors import BridgeError

from plexora.agent import bridge_roles, bridge_wire

PROVIDER = "plexora"

#: The collect kinds Plexora answers (the contract's list).
COLLECT_KINDS = ("gates", "regions", "qc", "selection")

def _raise_problem(error) -> None:
    raise BridgeError.from_problem(bridge_wire.problem(error), provider=PROVIDER)


class PlexoraProvider(Adapter):
    """One connection's view of Plexora: its session, policy, origin and link.

    `notify(project, plugin, kind, payload)` tells open tabs (a running
    server's notifier, or an MCP process's attached link); `link` is the
    attached server for viewer commands. Both None for a headless process.
    """

    provider = PROVIDER

    def __init__(self, session=None, *, policy=None, audit=None, link=None, notify=None,
                 origin="bridge"):
        from plexora.agent import AgentSession, Policy
        from plexora.agent.audit import AuditLog
        from plexora.agent.schemas import plexora_version

        self.session = session or AgentSession()
        self.policy = policy or Policy()
        self.audit = audit or AuditLog()
        self.link = link
        self.notify = notify
        #: The origin every call made through this provider carries. The
        #: transport decides it: the HTTP route by its token, a bridge tool by
        #: the call it was reached through -- an outside agent calling
        #: `bridge_invoke` stays `mcp`, so nothing here launders an origin.
        #: None: an in-process call, which carries none.
        self.origin = origin
        self.version = plexora_version()

    # -- catalogue -----------------------------------------------------------
    def capabilities(self):
        from spatialbridge.schema import CapabilityDescriptor

        from plexora.agent import registry
        from plexora.agent.policy import permitted

        return [CapabilityDescriptor.model_validate(bridge_roles.describe(cap))
                for cap in registry.all_capabilities() if permitted(cap, self.policy)]

    # -- calls ---------------------------------------------------------------
    def _invoke(self, tool, arguments):
        from plexora.agent import registry

        return registry.invoke(self.session, tool, arguments, policy=self.policy,
                               audit=self.audit, link=self.link, notify=self.notify,
                               origin=self.origin)

    def invoke(self, tool, arguments, *, expected_revision=None, origin="bridge"):
        """Run one capability by tool name. A job answers `{job_id, status}`
        at once, which the peer's client waits on through `job()`."""
        from plexora.agent import registry

        arguments = dict(arguments or {})
        try:
            capability = registry.get(tool)
        except Exception:
            raise BridgeError("capability_unavailable", f"Plexora has no tool {tool!r}",
                              provider=PROVIDER,
                              hint="bridge_capabilities lists what Plexora offers") from None
        if expected_revision is not None and \
                "expected_revision" in capability.input_model.model_fields:
            arguments.setdefault("expected_revision", expected_revision)
        answer = self._invoke(capability.tool_name, arguments)
        if not answer["ok"]:
            _raise_problem(answer["error"])
        result = answer["result"]
        if not isinstance(result, dict):
            result = {"result": result}
        result.setdefault("operation_id", answer.get("operation_id"))
        return result

    def job(self, job_id):
        from plexora.agent import jobs

        record = jobs.store().get(str(job_id))
        if record is None:
            raise BridgeError("invalid_input", f"Plexora has no job {job_id!r}",
                              provider=PROVIDER, hint="job_list shows the jobs it has")
        return bridge_wire.job_state(record)

    # -- the dataset -----------------------------------------------------------
    def bind(self, *, table, workspace_id=None, image_id=None, image=None, mask=None,
             roles=None, table_name=""):
        """The project that shows this table (and image), found or made.

        Roles arrive in the protocol's words (`cell_id, x, y, image_id,
        celltype`), which are `bind_project`'s own."""
        roles = {k: v for k, v in (roles or {}).items()
                 if k in ("cell_id", "x", "y", "image_id", "celltype") and v}
        found = self.invoke("find_project_for_table", {
            "table": table, "image_id": image_id, "table_name": table_name or None})
        if found.get("project"):
            return {"bound": True, "project": found["project"], "created": False}
        if not image:
            raise BridgeError(
                "precondition_missing",
                "Plexora has no project for this table yet, and making one needs the image",
                provider=PROVIDER, detail={"candidates": found.get("candidates") or []},
                hint="pass the image path once (context.image, and context.mask for "
                     "cell outlines)")
        made = self.invoke("bind_project", {
            "table": table, "image": image, "mask": mask, "image_id": image_id,
            "roles": roles or None, "table_name": table_name or None, "exist_ok": True})
        return {"bound": True, "project": made["project"],
                "created": bool(made.get("created")),
                "operation_id": made.get("operation_id")}

    def table_for(self, dataset):
        """`{table, table_name}` for a Plexora project (its name, or
        `plexora://project/<name>`): the file it reads, so a hand-off FROM
        Plexora can name the dataset SCIMAP Pro should open."""
        if not dataset:
            return None
        name = str(dataset)
        if name.startswith("plexora://project/"):
            name = name[len("plexora://project/"):].split("/", 1)[0]
        from plexora.server.models.project import Project

        record = Project.find(name)
        if record is None or record.dataset is None:
            return None
        return {"table": record.dataset.src, "table_name": record.dataset.table or "",
                "project": name}

    def _project(self, arguments, image_id):
        project = (arguments or {}).get("project")
        if project:
            self.session.project(project)
            return project
        raise BridgeError("invalid_input", "name the Plexora project to collect from "
                          "(arguments.project)", provider=PROVIDER,
                          hint="bridge_bind answers with the project")

    def collect(self, kind, *, image_id=None, operation_id=None, arguments=None):
        if kind not in COLLECT_KINDS:
            raise BridgeError("capability_unavailable",
                              f"Plexora hands back {list(COLLECT_KINDS)}, not {kind!r}",
                              provider=PROVIDER)
        arguments = dict(arguments or {})
        project = self._project(arguments, image_id)
        try:
            return getattr(self, f"_collect_{kind}")(project, image_id=image_id,
                                                     operation_id=operation_id,
                                                     arguments=arguments)
        except BridgeError:
            raise
        except Exception as exc:
            from plexora.agent.errors import as_agent_error

            _raise_problem(as_agent_error(exc).to_problem())

    def _image_id(self, project, image_id):
        from plexora.server.models.project import Project

        record = Project.find(project)
        subset = (record.dataset.subset or {}) if record and record.dataset else {}
        if subset.get("value") is not None:
            return str(subset["value"])
        return str(image_id) if image_id is not None else project

    def _provenance(self, capability, operation_id=None, **extra):
        from spatialbridge.schema import Provenance

        return Provenance(producer=PROVIDER, capability=capability,
                          producer_version=self.version, origin=self.origin,
                          operation_id=operation_id, **extra)

    def _collect_gates(self, project, *, image_id, operation_id, arguments):
        """Every set gate of the project, as the protocol's `Gates`: one row
        per marker, `value` the lower bound (cells above it are positive),
        `upper` the upper, with how it was set and the receipt that set it."""
        from spatialbridge.schema import Gate, Gates

        # Gating's core, never its automatic (Paid) package: reading a gate is
        # Free, and the bridge must not couple core to what may ship apart.
        from plexora.plugins.gating.server import model, provenance

        ds = self.session.data(project)
        recorded = provenance.read(ds.name)
        rows = []
        for gate in model.all_gates(ds):
            prov = recorded.get(gate["marker"]) or {}
            rows.append({"channel": gate["marker"], "gate_start": gate["low"],
                         "gate_end": gate["high"], "thresholded": gate["thresholded"],
                         **{k: prov.get(k) for k in ("method", "status", "confidence",
                                                     "session_id", "operation_id")}})
        image = self._image_id(project, image_id)
        gates = []
        for row in rows:
            if not row.get("thresholded") or row.get("gate_start") is None:
                continue
            confidence = row.get("confidence")
            try:
                confidence = float(confidence) if confidence not in (None, "") else None
            except (TypeError, ValueError):
                confidence = None
            gates.append(Gate(image_id=image, marker=str(row["channel"]),
                              value=float(row["gate_start"]),
                              upper=None if row.get("gate_end") is None
                              else float(row["gate_end"]),
                              method=row.get("method") or "manual",
                              status=row.get("status") or None, confidence=confidence,
                              session_id=row.get("session_id") or None,
                              operation_id=row.get("operation_id") or None,
                              source=f"plexora://project/{project}"))
        return Gates(gates=gates, provenance=self._provenance(
            "gating.export", operation_id, params={"project": project})
        ).model_dump(exclude_none=True)

    def _collect_regions(self, project, *, image_id, operation_id, arguments):
        """The project's ROIs as the ROI tool exports them, with the `plexora`
        member copied into the protocol's `bridge` member."""
        from plexora.plugins.roi import VERSION
        from plexora.plugins.roi.server import geojson
        from plexora.plugins.roi.server.repository import ROIRepository

        state = ROIRepository(project).load()
        document = geojson.export_document(state, project, VERSION,
                                           image_id=self._image_id(project, image_id))
        return _with_bridge_member(document, "roi", self._provenance(
            "roi.export", operation_id, params={"project": project}))

    def _collect_qc(self, project, *, image_id, operation_id, arguments):
        """The project's active QC result: the cells it excludes (ids as the
        table names them, with their primary reason), its regions (the QC
        export, with its `plexora` member and per-feature `category_id`), and
        its summary."""
        from plexora.plugins.qc.server import export, results

        document = results.load(project)
        result = results.active(document)
        if result is None:
            raise BridgeError("precondition_missing", f"{project!r} has no QC result",
                              provider=PROVIDER,
                              hint="run qc_session_start, or draw QC regions, first")
        ds = self.session.image_data(project)
        regions = _with_bridge_member(export.regions_geojson(ds, result), "qc",
                                      self._provenance("qc.export", operation_id))
        cells = results.cells(project)
        excluded, reasons = [], {}
        if cells is not None and cells.height and "pass" in cells.columns:
            failed = cells.filter(~cells["pass"].fill_null(True))
            for row in failed.iter_rows(named=True):
                cell = str(row.get("cell_id"))
                excluded.append(cell)
                if row.get("primary_reason"):
                    reasons[cell] = str(row["primary_reason"])
        summary = dict(results.summary(result))
        summary["n_excluded"] = len(excluded)
        summary["cell_id"] = _cell_id_rule(project)
        out = {"result_id": str(result.get("result_id")),
               "image_id": self._image_id(project, image_id),
               "excluded": excluded, "reasons": reasons, "regions": regions,
               "summary": summary,
               "provenance": self._provenance("qc.export", operation_id).model_dump(
                   exclude_none=True)}
        return out

    def _collect_selection(self, project, *, image_id, operation_id, arguments):
        """A stored selection (`arguments.name`). Its ids travel inline only to
        the bridge origin -- the application that owns the table they index --
        or to a connection whose policy allows row-level egress; otherwise the
        answer is the reference, the count and the digest."""
        from plexora.agent.core import selection
        from plexora.agent.registry import ORIGIN_BRIDGE

        name = arguments.get("name")
        if not name:
            raise BridgeError("invalid_input", "name the selection (arguments.name)",
                              provider=PROVIDER, hint="list_selections")
        record = selection.read_all(project).get(str(name))
        if record is None:
            raise BridgeError("invalid_input", f"{project!r} has no selection {name!r}",
                              provider=PROVIDER, hint="list_selections")
        inline = self.origin == ORIGIN_BRIDGE or "row_level" in self.policy.egress
        return selection.describe(project, record, with_ids=inline)

    # -- events ---------------------------------------------------------------
    def bound_projects(self, workspace_id) -> list:
        """The projects showing a workspace's table: the ones its record names,
        and any other project reading the same file."""
        from spatialbridge import workspace

        try:
            ws = workspace.open_workspace(workspace_id)
            record = ws.read()
        except BridgeError:
            return []
        names = [image.get("project") for image in record["dataset"].get("images", [])
                 if image.get("project")]
        try:
            table = ws.location()
            found = self.invoke("find_project_for_table", {
                "table": table, "table_name": record["dataset"].get("table_name") or None})
            names += [c["project"] for c in found.get("candidates") or []]
        except BridgeError:
            pass
        return list(dict.fromkeys(names))

    def on_event(self, event):
        """A peer changed the dataset: every project bound to its workspace
        catches up (`dataset_events.on_dataset_changed`) and its tabs are told."""
        from plexora.server.models import dataset_events

        if event.get("kind") not in (dataset_events.KIND, "external_change"):
            return {"handled": False, "reason": f"Plexora does not act on {event.get('kind')!r}"}
        projects = self.bound_projects(event["workspace_id"])
        payload = {"sections": list(event.get("sections") or []),
                   "revision": event.get("revision"), "producer": event.get("producer"),
                   "workspace_id": event.get("workspace_id"),
                   "execution_id": event.get("execution_id"),
                   "operation_id": event.get("operation_id")}
        answers = [dataset_events.on_dataset_changed(project, payload,
                                                     notify=self._tabs_notifier())
                   for project in projects]
        return {"handled": bool(answers), "projects": projects, "results": answers}

    def _tabs_notifier(self):
        from plexora.agent.core.bridge import tabs_notifier

        return tabs_notifier(self.notify)

    def info(self):
        return {"plexora_version": self.version, "origin": self.origin,
                "collect": list(COLLECT_KINDS)}


def _cell_id_rule(project) -> dict:
    """How the ids in a collected object name cells: the table's id column
    when the project reads one, else the row's position within the image."""
    from plexora.server.models.project import Project

    record = Project.find(project)
    spec = record.dataset if record else None
    column = (spec.obs_id_field or (spec.roles.cell_id if spec.roles else None)) if spec \
        else None
    if spec is not None and spec.type in ("anndata", "spatialdata") and not spec.obs_id_field:
        return {"kind": "positional_in_subset", "column": None}
    return {"kind": "obs_column", "column": column}


def _with_bridge_member(document: dict, producer: str, provenance) -> dict:
    """A Plexora regions document with its `plexora` member copied into the
    protocol's `bridge` member (both kept: the file still imports into the ROI
    tool, and SCIMAP Pro reads either)."""
    member = dict(document.get("plexora") or {})
    out = dict(document)
    out["bridge"] = {"producer": member.get("producer") or producer,
                     "image_id": member.get("image_id"),
                     "datasource": member.get("datasource"),
                     "coordinate_space": {k: v for k, v in
                                          (member.get("coordinate_space") or {}).items()
                                          if v is not None},
                     "categories": member.get("categories") or [],
                     "result_id": member.get("result_id")}
    out["provenance"] = provenance.model_dump(exclude_none=True)
    return out


def tools_for(provider, *, read_only=False):
    from spatialbridge.tools import BridgeTools

    return BridgeTools(provider, read_only=read_only)

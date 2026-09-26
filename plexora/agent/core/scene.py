"""The scene view of a project (schema 0.5): pure adapters of the record.

Reads the project record and nothing else -- no file is opened, no table is
loaded -- so it is cheap and safe for a project whose data is on a node that is
asleep. Counts that would need the table are included only when the session
already holds it.
"""

from __future__ import annotations

import uuid

from pydantic import Field

from plexora.agent.limits import MAX_LIST
from plexora.agent.registry import Capability
from plexora.agent.scene_models import (Association, CoordinateSystem, EntitySet,
                                        FeatureSpace, Scene, SpatialAsset, TransformEdge)
from plexora.agent.schemas import ProjectInput

#: The namespace every derived id is made in. Fixed forever: changing it would
#: change every id an agent has ever been handed.
PLEXORA_NS = uuid.UUID("6f1c4a52-1d3e-5b8a-9c0e-7a2d4b6e8f10")

#: Which capabilities apply to which kind of asset, by tool name.
_ASSET_TOOLS = {
    "reference": ("render_region", "list_channels"),
    "labels": ("render_region",),
    "points": ("count_cells_in_roi", "sample_gate_validation_regions"),
}


def derived_id(kind, project, key):
    return f"{kind}_{uuid.uuid5(PLEXORA_NS, f'{kind}:{project}:{key}').hex[:16]}"


def _known_tools():
    from plexora.agent import registry

    return {cap.tool_name for cap in registry.all_capabilities()}


def _format(layer, record):
    if layer.id == "__image__":
        return record.image.kind
    if layer.id == "__mask__":
        return "label_pyramid"
    if layer.id == "__centroids__":
        return record.dataset.type if record.dataset else None
    return (layer.source or {}).get("format") or layer.extra.get("format")


def build_scene(record, *, n_cells=None, artifacts_list=()):
    from plexora.server.models.project import CENTROID_LAYER_ID, MASK_LAYER_ID
    from plexora.server.utils import pixel_scale

    name = record.name
    ref = f"cs:{name}:reference"
    systems = [CoordinateSystem(id=ref, name="reference image pixels", units="pixel",
                                description="full-resolution pixels of the reference "
                                            "image, origin top-left")]
    transforms = []
    pixel = pixel_scale.pixel_size(record)
    notes = []
    if pixel:
        physical = f"cs:{name}:physical"
        systems.append(CoordinateSystem(id=physical, name="physical", units="µm",
                                        description=f"microns ({pixel['source']})"))
        v = pixel["value"]
        transforms.append(TransformEdge(source=ref, target=physical, kind="scale",
                                        matrix=[v, 0.0, 0.0, v, 0.0, 0.0],
                                        provenance=f"pixel size, {pixel['source']}"))
    else:
        notes.append("uncalibrated: no physical coordinate system")

    known = _known_tools()
    assets, associations = [], []
    for layer in record.all_layers:
        asset_id = derived_id("asset", name, layer.id)
        cs = ref
        if layer.id not in ("__image__", MASK_LAYER_ID, CENTROID_LAYER_ID) and layer.transform:
            cs = f"cs:{name}:{layer.id}"
            systems.append(CoordinateSystem(id=cs, name=f"{layer.label or layer.id} pixels",
                                            units="pixel"))
            transforms.append(TransformEdge(source=cs, target=ref, kind="affine2d",
                                            matrix=list(layer.affine),
                                            provenance=layer.transform_source or "registered"))
        role = ("reference" if layer.id == "__image__" else layer.kind)
        tools = [t for t in _ASSET_TOOLS.get(role, ()) if t in known]
        binding = layer.binding
        provider = "node" if binding is not None and binding.is_node else (
            "derived" if layer.id == CENTROID_LAYER_ID else "local")
        extra = dict(layer.extra)
        if layer.id == MASK_LAYER_ID:
            extra["mask_scale"] = record.segmentation.scale
        assets.append(SpatialAsset(
            asset_id=asset_id, layer_id=layer.id, label=layer.label or layer.id,
            element_type=layer.kind, modality=layer.modality, format=_format(layer, record),
            provider=provider, node=binding.node if binding is not None else None,
            shape=([layer.height, layer.width] if layer.width or layer.height
                   else ([record.image.height, record.image.width]
                         if layer.id == "__image__" else None)),
            levels=((layer.max_level or 0) + 1 if layer.max_level is not None else
                    ((record.image.max_level or 0) + 1 if layer.id == "__image__"
                     and record.image.max_level is not None else None)),
            coordinate_system=cs, capabilities=tools, status=layer.status,
            visible=layer.visible, extra=extra))
        for bundle in record.bundles:
            if layer.id in [l.id for l in record.layers_from(bundle.get("id"))]:
                associations.append(Association(kind="layer_in_bundle", source=asset_id,
                                                target=str(bundle.get("id")),
                                                detail={"format": bundle.get("format")}))

    entity_sets, spaces = [], []
    if record.has_table:
        cells = derived_id("entities", name, "cells")
        roles = record.roles
        from plexora.api.dataset import TableHandle

        handle = TableHandle(record)
        entity_sets.append(EntitySet(
            entity_set_id=cells, name="cells",
            kind="spots" if record.visium_spot_radius else "cells",
            count=n_cells, id_column=roles.cell_id,
            coordinate_columns=[roles.x, roles.y], coordinate_system=ref,
            table=handle.locator.to_dict()))
        markers = list(record.columns.markers) if record.columns.classified else []
        spaces.append(FeatureSpace(
            feature_space_id=derived_id("features", name, "markers"), name="markers",
            entity_set_id=cells, features=markers[:MAX_LIST],
            truncated=len(markers) > MAX_LIST, kind="markers",
            scale="log1p" if record.log_transformed else "raw"))
        metadata = handle.metadata_columns
        spaces.append(FeatureSpace(
            feature_space_id=derived_id("features", name, "metadata"), name="metadata",
            entity_set_id=cells, features=metadata[:MAX_LIST],
            truncated=len(metadata) > MAX_LIST, kind="metadata"))
        if not record.columns.classified:
            notes.append("markers are not classified yet; the marker feature space is empty")
        for asset in assets:
            if asset.layer_id == MASK_LAYER_ID:
                associations.append(Association(
                    kind="labels_entities", source=asset.asset_id, target=cells,
                    detail={"label_value": "cell id column" if roles.cell_id else
                            "row number"}))
            if asset.layer_id == CENTROID_LAYER_ID:
                associations.append(Association(kind="points_are_entities",
                                                source=asset.asset_id, target=cells))
    return Scene(project=name, reference_coordinate_system=ref, coordinate_systems=systems,
                 transforms=transforms, assets=assets, entity_sets=entity_sets,
                 feature_spaces=spaces, associations=associations,
                 artifacts=list(artifacts_list), notes=notes)


class SceneInput(ProjectInput):
    include_artifacts: bool = Field(False, description="List recent renders of this project.")


def get_scene(call, inp):
    from plexora.agent import artifacts
    from plexora.agent.scene_models import Artifact

    record = call.session.project(inp.project)
    n_cells = None
    if inp.project in call.session.held():
        frame = call.session.data(inp.project).table.geometry()
        n_cells = int(frame.height) if frame is not None else None
    arts = []
    if inp.include_artifacts:
        arts = [Artifact(artifact_id=a["id"], uri=a["uri"], kind=a["kind"] or "render",
                         created=a.get("created"))
                for a in artifacts.list_artifacts(inp.project, 20)]
    return build_scene(record, n_cells=n_cells, artifacts_list=arts)


def capabilities():
    return [
        Capability(
            name="project.scene", tool_name="get_scene", owner="core",
            purpose="A modality-neutral description of a sample: spatial assets (image, "
                    "mask, points, other layers) with their coordinate systems and "
                    "transforms, the entity sets (cells/spots) and feature spaces "
                    "(markers, metadata), and how they are associated. Reads the record "
                    "only.",
            permission="read", input_model=SceneInput, handler=get_scene,
            tags=("scene", "layers", "modality", "coordinate", "transform", "assets",
                  "structure", "spatialdata")),
    ]

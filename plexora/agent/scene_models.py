"""A modality-neutral description of a sample, for agents (schema 0.5).

Plexora's project record grew up around one image, one mask and one table,
with layers added beside them. An agent reasoning about spatial data needs the
general shape instead -- the one SpatialData and similar models use: spatial
**assets** (images, label masks, points, shapes) placed in **coordinate
systems** by **transforms**, **entity sets** (cells, spots, bins) with
**feature spaces** (markers, metadata) measured on them, and the
**associations** between them (this mask labels those cells).

Version 0.5 is a derived view: every field is computed from the project
record (`core/scene.py`) and nothing is stored. Ids are therefore
deterministic (uuid5 of the project and the layer) rather than persisted; a
later version may keep them in `Project.extra["agentIds"]`. A new modality
arrives as a layer with its own `modality` string and needs no change here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from plexora.agent.schemas import AgentModel

SCENE_SCHEMA_VERSION = "0.5"

ElementType = Literal["image", "labels", "points", "shapes"]


class CoordinateSystem(AgentModel):
    id: str
    name: str
    units: Literal["pixel", "µm"]
    axes: list[str] = Field(default_factory=lambda: ["x", "y"])
    description: str = ""


class TransformEdge(AgentModel):
    """Maps `source` coordinates into `target`. `matrix` is the 2-D affine
    [a, b, c, d, e, f] in the layers' canvas order: x' = a x + c y + e,
    y' = b x + d y + f."""

    source: str
    target: str
    kind: Literal["identity", "affine2d", "scale"]
    matrix: list[float] | None = None
    provenance: str = ""


class SpatialAsset(AgentModel):
    asset_id: str
    layer_id: str
    label: str = ""
    element_type: ElementType
    modality: str | None = None
    format: str | None = None
    provider: Literal["local", "node", "derived"] = "local"
    node: str | None = None
    axes: list[str] = Field(default_factory=lambda: ["y", "x"])
    shape: list[int | None] | None = None
    levels: int | None = None
    coordinate_system: str
    capabilities: list[str] = Field(default_factory=list)
    status: str = "ready"
    visible: bool = True
    extra: dict[str, Any] = Field(default_factory=dict)


class EntitySet(AgentModel):
    entity_set_id: str
    name: str
    kind: str = "cells"
    count: int | None = None
    id_column: str | None = None
    coordinate_columns: list[str | None] = Field(default_factory=list)
    coordinate_system: str
    table: dict[str, Any] | None = None


class FeatureSpace(AgentModel):
    feature_space_id: str
    name: str
    entity_set_id: str
    features: list[str]
    truncated: bool = False
    scale: Literal["raw", "log1p"] | None = None
    kind: Literal["markers", "metadata", "other"] = "other"


class Association(AgentModel):
    kind: Literal["labels_entities", "points_are_entities", "layer_in_bundle",
                  "measured_on"]
    source: str
    target: str
    detail: dict[str, Any] = Field(default_factory=dict)


class Artifact(AgentModel):
    artifact_id: str
    uri: str
    kind: str
    created: float | None = None


class Job(AgentModel):
    job_id: str
    status: Literal["queued", "running", "done", "failed"]
    capability: str


class ViewerWorkspace(AgentModel):
    """What an open viewer is showing, when one is attached (see viewer.py)."""

    view_id: str
    project: str
    viewport: dict[str, Any] | None = None
    channels: list[dict[str, Any]] = Field(default_factory=list)


class Scene(AgentModel):
    schema_version: str = SCENE_SCHEMA_VERSION
    project: str
    reference_coordinate_system: str
    coordinate_systems: list[CoordinateSystem]
    transforms: list[TransformEdge]
    assets: list[SpatialAsset]
    entity_sets: list[EntitySet]
    feature_spaces: list[FeatureSpace]
    associations: list[Association]
    artifacts: list[Artifact] = Field(default_factory=list)
    jobs: list[Job] = Field(default_factory=list)
    workspaces: list[ViewerWorkspace] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

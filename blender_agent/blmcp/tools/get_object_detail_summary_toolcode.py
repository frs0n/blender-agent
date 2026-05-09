# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tool-code for returning structured detail about a single object.
"""

__all__ = (
    "Params",
    "Result",
    "main",
)

from typing import NamedTuple


class Params(NamedTuple):
    name: str


class Result(NamedTuple):
    status: str
    name: str | None = None
    type: str | None = None
    location: list[float] | None = None
    rotation: list[float] | None = None
    scale: list[float] | None = None
    dimensions: list[float] | None = None
    parent: str | None = None
    children: list[str] | None = None
    modifiers: list[dict[str, object]] | None = None
    constraints: list[dict[str, object]] | None = None
    materials: list[str | None] | None = None
    visibility: dict[str, bool] | None = None
    data_name: str | None = None
    collections: list[str] | None = None
    message: str | None = None


def main(params: Params) -> Result:
    import bpy  # pylint: disable=import-error,no-name-in-module

    obj = bpy.data.objects.get(params.name)
    if obj is None:
        available = sorted(bpy.data.objects.keys())
        return Result(
            status="error",
            message="Object {!r} not found. Available objects: {:s}".format(
                params.name, ", ".join(available) if available else "(none)",
            ),
        )

    return Result(
        status="ok",
        name=obj.name,
        type=obj.type,
        location=list(obj.location),
        rotation=list(obj.rotation_euler),
        scale=list(obj.scale),
        dimensions=list(obj.dimensions),
        parent=obj.parent.name if obj.parent else None,
        children=[child.name for child in obj.children],
        modifiers=[
            {
                "name": mod.name,
                "type": mod.type,
                "show_viewport": mod.show_viewport,
                "show_render": mod.show_render,
            }
            for mod in obj.modifiers
        ],
        constraints=[
            {
                "name": con.name,
                "type": con.type,
                "enabled": con.enabled,
            }
            for con in obj.constraints
        ],
        materials=[
            slot.material.name if slot.material else None
            for slot in obj.material_slots
        ],
        visibility={
            "hide_viewport": obj.hide_viewport,
            "hide_render": obj.hide_render,
            "hide_get": obj.hide_get(),
        },
        data_name=obj.data.name if obj.data else None,
        collections=[col.name for col in obj.users_collection],
    )

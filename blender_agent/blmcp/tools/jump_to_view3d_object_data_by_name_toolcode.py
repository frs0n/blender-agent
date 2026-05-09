# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tool-code for focusing the 3D viewport on an object's data block.
"""

__all__ = (
    "Params",
    "Result",
    "main",
)

from typing import Any, NamedTuple


class Params(NamedTuple):
    name: str
    allow_edits: bool


class Result(NamedTuple):
    status: str
    object: str | None = None
    data_name: str | None = None
    type: str | None = None
    location: list[float] | None = None
    message: str | None = None


def main(params: Params) -> Result:
    import bpy  # pylint: disable=import-error,no-name-in-module

    if bpy.app.background:
        return Result(status="error", message="Not available in background mode")
    if bpy.context.window is None:
        return Result(status="error", message="No active window")

    target = None
    for obj in bpy.data.objects:
        if obj.data is not None and obj.data.name == params.name:
            target = obj
            break
    if target is None:
        return Result(
            status="error",
            message="No object found with data named {!r}".format(params.name),
        )

    if params.allow_edits:
        if target.hide_viewport:
            target.hide_viewport = False
        if target.hide_get():
            target.hide_set(False)

        def _enable_collections(layer_col: Any, tgt: Any) -> bool:
            found = False
            for child in layer_col.children:
                if _enable_collections(child, tgt):
                    found = True
            if tgt.name in layer_col.collection.objects:
                found = True
            if found:
                layer_col.exclude = False
                layer_col.hide_viewport = False
            return found

        _enable_collections(bpy.context.view_layer.layer_collection, target)

    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    bpy.ops.object.select_all(action="DESELECT")
    target.select_set(True)
    bpy.context.view_layer.objects.active = target

    view3d_found = False
    for area in bpy.context.screen.areas:
        if area.type == "VIEW_3D":
            view3d_found = True
            # Exit camera view so the user sees the object in a free viewport.
            r3d = area.spaces.active.region_3d
            if r3d and r3d.view_perspective == "CAMERA":
                r3d.view_perspective = "PERSP"
            for region in area.regions:
                if region.type == "WINDOW":
                    with bpy.context.temp_override(area=area, region=region):
                        bpy.ops.view3d.view_selected()
                    break
            break

    return Result(
        status="ok",
        object=target.name,
        data_name=params.name,
        type=target.type,
        location=list(target.location),
        message=None if view3d_found else "No 3D viewport found, object selected but not framed",
    )

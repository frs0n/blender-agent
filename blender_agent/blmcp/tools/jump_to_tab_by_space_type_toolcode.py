# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tool-code for switching workspace tabs by space-type.
"""

__all__ = (
    "Params",
    "Result",
    "main",
)

from typing import NamedTuple


class Params(NamedTuple):
    space_type: str
    allow_edits: bool


class Result(NamedTuple):
    status: str
    workspace: str | None = None
    space_type: str | None = None
    created: bool | None = None
    message: str | None = None
    available_space_types: list[str] | None = None


def main(params: Params) -> Result:
    import bpy  # pylint: disable=import-error,no-name-in-module

    if bpy.app.background:
        return Result(status="error", message="Not available in background mode")
    if bpy.context.window is None:
        return Result(status="error", message="No active window")

    def _largest_area(screen: "bpy.types.Screen") -> "bpy.types.Area | None":
        return max(screen.areas, key=lambda a: a.width * a.height, default=None)

    # Find an existing workspace whose largest area matches the desired space type.
    found = None
    for ws in bpy.data.workspaces:
        for screen in ws.screens:
            area = _largest_area(screen)
            if area is not None and area.type == params.space_type:
                found = ws
                break
        if found:
            break

    if found:
        bpy.context.window.workspace = found
        return Result(status="ok", workspace=found.name, space_type=params.space_type)

    if params.allow_edits:
        # Duplicate the current workspace and change its main area type.
        try:
            bpy.ops.workspace.duplicate()
        except RuntimeError as ex:
            return Result(status="error", message=str(ex))
        new_ws = bpy.context.window.workspace
        new_ws.name = params.space_type.replace("_", " ").title()
        area = _largest_area(bpy.context.screen)
        if area is not None:
            area.type = params.space_type
        return Result(
            status="ok",
            workspace=new_ws.name,
            space_type=params.space_type,
            created=True,
        )

    available = sorted({
        area.type
        for ws in bpy.data.workspaces
        for screen in ws.screens
        for area in ((_largest_area(screen),) if _largest_area(screen) else ())
        if area is not None
    })
    return Result(
        status="error",
        message="No workspace with space type {!r} found".format(params.space_type),
        available_space_types=available,
    )

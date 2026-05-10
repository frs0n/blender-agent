# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tool-code for snapping objects to various targets.
"""

__all__ = (
    "Params",
    "Result",
    "main",
)

from typing import NamedTuple


class Params(NamedTuple):
    object_names: list[str]
    target: str  # 'cursor', 'grid', 'active', 'world_origin'
    snap_point: str = "center"  # 'center', 'min' (bottom/left/back), 'max' (top/right/front)
    axes: list[str] = ("x", "y", "z")  # Which axes to snap


class Result(NamedTuple):
    status: str
    snapped_count: int = 0
    message: str | None = None


def _get_aabb(obj):
    """Returns the world-space axis-aligned bounding box (AABB) of an object."""
    import mathutils  # pylint: disable=import-error,no-name-in-module

    if obj.type != 'MESH':
        return None

    local_bbox_corners = [mathutils.Vector(corner) for corner in obj.bound_box]
    world_bbox_corners = [obj.matrix_world @ corner for corner in local_bbox_corners]
    min_corner = mathutils.Vector(map(min, zip(*world_bbox_corners)))
    max_corner = mathutils.Vector(map(max, zip(*world_bbox_corners)))
    return min_corner, max_corner


def _get_object_point(obj, snap_point, axis_idx):
    """Returns the specified point of an object along a specific axis."""
    import mathutils  # pylint: disable=import-error,no-name-in-module

    if obj.type == 'MESH':
        min_corner, max_corner = _get_aabb(obj)
        if snap_point == 'min':
            return min_corner[axis_idx]
        elif snap_point == 'max':
            return max_corner[axis_idx]
        else:  # center
            return (min_corner[axis_idx] + max_corner[axis_idx]) * 0.5
    else:
        if snap_point == 'min' or snap_point == 'max':
            return obj.matrix_world.translation[axis_idx]
        else:  # center
            return obj.matrix_world.translation[axis_idx]


def main(params: Params) -> Result:
    import bpy  # pylint: disable=import-error,no-name-in-module
    import mathutils  # pylint: disable=import-error,no-name-in-module

    axis_map = {'x': 0, 'y': 1, 'z': 2}
    axes_indices = []
    for axis in params.axes:
        idx = axis_map.get(axis.lower())
        if idx is None:
            return Result(
                status="error",
                message="Invalid axis: {!r}. Must be 'x', 'y', or 'z'.".format(axis),
            )
        axes_indices.append(idx)

    snap_point = params.snap_point.lower()
    if snap_point not in ('center', 'min', 'max'):
        return Result(
            status="error",
            message="Invalid snap_point: {!r}. Must be 'center', 'min', or 'max'.".format(params.snap_point),
        )

    # Get target position
    target_pos = [0.0, 0.0, 0.0]

    if params.target == "cursor":
        cursor_loc = bpy.context.scene.cursor.location
        for idx in axes_indices:
            target_pos[idx] = cursor_loc[idx]

    elif params.target == "active":
        active = bpy.context.view_layer.objects.active
        if active is None:
            return Result(
                status="error",
                message="No active object for target 'active'.",
            )
        for idx in axes_indices:
            target_pos[idx] = _get_object_point(active, snap_point, idx)

    elif params.target == "world_origin":
        for idx in axes_indices:
            target_pos[idx] = 0.0

    elif params.target == "grid":
        # Snap to nearest grid point (round to nearest integer)
        for idx in axes_indices:
            target_pos[idx] = 0.0  # Will be calculated per object

    else:
        return Result(
            status="error",
            message="Invalid target: {!r}. Must be 'cursor', 'active', 'grid', or 'world_origin'.".format(params.target),
        )

    # Get objects
    objects = []
    for name in params.object_names:
        obj = bpy.data.objects.get(name)
        if obj is None:
            return Result(
                status="error",
                message="Object {!r} not found.".format(name),
            )
        objects.append(obj)

    # Snap objects
    for obj in objects:
        for idx in axes_indices:
            if params.target == "grid":
                # Snap to nearest grid point
                obj_point = _get_object_point(obj, snap_point, idx)
                grid_point = round(obj_point)
                offset = grid_point - obj_point
            else:
                obj_point = _get_object_point(obj, snap_point, idx)
                offset = target_pos[idx] - obj_point

            obj.location[idx] += offset

    bpy.context.view_layer.update()

    return Result(
        status="ok",
        snapped_count=len(objects),
        message="Snapped {} objects to {} (snap_point: {}).".format(
            len(objects), params.target, snap_point
        ),
    )

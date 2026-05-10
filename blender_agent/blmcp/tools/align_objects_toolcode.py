# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tool-code for aligning multiple objects along different axes.
"""

__all__ = (
    "Params",
    "Result",
    "main",
)

from typing import NamedTuple


class Params(NamedTuple):
    object_names: list[str]
    axis: str  # 'x', 'y', 'z'
    mode: str  # 'min' (bottom/left/back), 'max' (top/right/front), 'center'
    reference: str = "selection"  # 'selection', 'active', 'cursor', 'world_origin'


class Result(NamedTuple):
    status: str
    aligned_count: int = 0
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


def _get_object_center(obj):
    """Returns the world-space center of an object."""
    import mathutils  # pylint: disable=import-error,no-name-in-module

    if obj.type == 'MESH':
        min_corner, max_corner = _get_aabb(obj)
        return (min_corner + max_corner) * 0.5
    else:
        return obj.matrix_world.translation.copy()


def _get_object_bounds(obj, axis_idx):
    """Returns the min and max bounds of an object along a specific axis."""
    import mathutils  # pylint: disable=import-error,no-name-in-module

    if obj.type == 'MESH':
        min_corner, max_corner = _get_aabb(obj)
        return min_corner[axis_idx], max_corner[axis_idx]
    else:
        loc = obj.matrix_world.translation[axis_idx]
        return loc, loc


def main(params: Params) -> Result:
    import bpy  # pylint: disable=import-error,no-name-in-module
    import mathutils  # pylint: disable=import-error,no-name-in-module

    axis_map = {'x': 0, 'y': 1, 'z': 2}
    axis_idx = axis_map.get(params.axis.lower())
    if axis_idx is None:
        return Result(
            status="error",
            message="Invalid axis: {!r}. Must be 'x', 'y', or 'z'.".format(params.axis),
        )

    mode = params.mode.lower()
    if mode not in ('min', 'max', 'center'):
        return Result(
            status="error",
            message="Invalid mode: {!r}. Must be 'min', 'max', or 'center'.".format(params.mode),
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

    if len(objects) < 2:
        return Result(
            status="error",
            message="At least 2 objects are required for alignment.",
        )

    # Calculate reference position
    if params.reference == "active":
        active = bpy.context.view_layer.objects.active
        if active is None:
            return Result(
                status="error",
                message="No active object for reference mode 'active'.",
            )
        if mode == "min":
            ref_pos = _get_object_bounds(active, axis_idx)[0]
        elif mode == "max":
            ref_pos = _get_object_bounds(active, axis_idx)[1]
        else:  # center
            ref_pos = _get_object_center(active)[axis_idx]

    elif params.reference == "cursor":
        cursor_loc = bpy.context.scene.cursor.location
        ref_pos = cursor_loc[axis_idx]

    elif params.reference == "world_origin":
        ref_pos = 0.0

    else:  # selection
        # Calculate reference from selection
        if mode == "min":
            ref_pos = min(_get_object_bounds(obj, axis_idx)[0] for obj in objects)
        elif mode == "max":
            ref_pos = max(_get_object_bounds(obj, axis_idx)[1] for obj in objects)
        else:  # center
            centers = [_get_object_center(obj)[axis_idx] for obj in objects]
            ref_pos = sum(centers) / len(centers)

    # Align objects
    for obj in objects:
        if mode == "min":
            obj_min = _get_object_bounds(obj, axis_idx)[0]
            offset = ref_pos - obj_min
        elif mode == "max":
            obj_max = _get_object_bounds(obj, axis_idx)[1]
            offset = ref_pos - obj_max
        else:  # center
            obj_center = _get_object_center(obj)[axis_idx]
            offset = ref_pos - obj_center

        # Apply offset
        obj.location[axis_idx] += offset

    bpy.context.view_layer.update()

    return Result(
        status="ok",
        aligned_count=len(objects),
        message="Aligned {} objects along {} axis (mode: {}, reference: {}).".format(
            len(objects), params.axis, mode, params.reference
        ),
    )

"""Blender command tools adapted from Blender Lab's official blender_mcp.

The tool surface, prompt, and bundled documentation are sourced from:
https://projects.blender.org/lab/blender_mcp
"""

from __future__ import annotations

import io
import json
import base64
import os
import re
import subprocess
import tempfile
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Callable

import bpy
import mathutils


ToolHandler = Callable[..., Any]

OFFICIAL_MCP_ROOT = Path(__file__).resolve().parent / "blmcp"
OFFICIAL_TOOLS_ROOT = OFFICIAL_MCP_ROOT / "tools"
OFFICIAL_DATA_ROOT = OFFICIAL_MCP_ROOT / "data"
OFFICIAL_PROMPTS_PATH = OFFICIAL_DATA_ROOT / "prompts.yml"


def _get_aabb(obj: bpy.types.Object) -> list[list[float]]:
    if obj.type != "MESH":
        raise TypeError("Object must be a mesh")

    local_bbox_corners = [mathutils.Vector(corner) for corner in obj.bound_box]
    world_bbox_corners = [obj.matrix_world @ corner for corner in local_bbox_corners]
    min_corner = mathutils.Vector(map(min, zip(*world_bbox_corners)))
    max_corner = mathutils.Vector(map(max, zip(*world_bbox_corners)))
    return [[*min_corner], [*max_corner]]


def _normalise_image_format(format_name: str) -> tuple[str, str, str]:
    requested = (format_name or "png").strip().lower()
    if requested in {"jpg", "jpeg"}:
        return "JPEG", "jpg", "image/jpeg"
    if requested == "png":
        return "PNG", "png", "image/png"
    raise ValueError("format must be 'png', 'jpg', or 'jpeg'")


def _screenshot_filepath(filepath: str | None, extension: str) -> str:
    if filepath:
        path = Path(filepath).expanduser()
        if not path.suffix:
            path = path.with_suffix(f".{extension}")
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    fd, path = tempfile.mkstemp(prefix="blender_agent_screenshot_", suffix=f".{extension}")
    os.close(fd)
    return path


def _find_view3d_override() -> dict[str, Any] | None:
    window_manager = bpy.context.window_manager
    for window in window_manager.windows:
        screen = window.screen
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region:
                return {"window": window, "screen": screen, "area": area, "region": region}
    return None


def _resize_saved_image(filepath: str, max_size: int, image_format: str) -> tuple[int, int]:
    img = bpy.data.images.load(filepath)
    try:
        width, height = img.size
        if max(width, height) > max_size:
            scale = max_size / max(width, height)
            width = max(1, int(width * scale))
            height = max(1, int(height * scale))
            img.scale(width, height)
            img.file_format = image_format
            img.save()
        return int(width), int(height)
    finally:
        bpy.data.images.remove(img)


def _image_data_url(filepath: str, max_size: int, image_format: str, mime_type: str) -> tuple[str, int, int]:
    img = bpy.data.images.load(filepath)
    preview_path = None
    try:
        width, height = img.size
        if max(width, height) > max_size:
            scale = max_size / max(width, height)
            width = max(1, int(width * scale))
            height = max(1, int(height * scale))
            img.scale(width, height)

        fd, preview_path = tempfile.mkstemp(prefix="blender_agent_screenshot_preview_", suffix=f".{image_format.lower()}")
        os.close(fd)
        img.file_format = image_format
        img.save_render(preview_path)
        with open(preview_path, "rb") as preview_file:
            encoded = base64.b64encode(preview_file.read()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}", int(width), int(height)
    finally:
        bpy.data.images.remove(img)
        if preview_path and os.path.exists(preview_path):
            os.remove(preview_path)


def _scene_bounds() -> tuple[mathutils.Vector, float]:
    objects = [obj for obj in bpy.context.scene.objects if obj.type != "CAMERA" and obj.visible_get()]
    if not objects:
        return mathutils.Vector((0, 0, 0)), 3.0

    points: list[mathutils.Vector] = []
    for obj in objects:
        if obj.type == "MESH":
            points.extend(obj.matrix_world @ mathutils.Vector(corner) for corner in obj.bound_box)
        else:
            points.append(obj.matrix_world.translation)

    min_corner = mathutils.Vector(map(min, zip(*points)))
    max_corner = mathutils.Vector(map(max, zip(*points)))
    center = (min_corner + max_corner) * 0.5
    radius = max((point - center).length for point in points)
    return center, max(radius, 1.0)


def _create_preview_camera() -> bpy.types.Object:
    center, radius = _scene_bounds()
    camera_data = bpy.data.cameras.new("BlenderAgent_TempScreenshotCamera")
    camera = bpy.data.objects.new("BlenderAgent_TempScreenshotCamera", camera_data)
    bpy.context.collection.objects.link(camera)
    camera.location = center + mathutils.Vector((radius * 2.6, -radius * 3.0, radius * 2.0))
    direction = center - camera.location
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    camera_data.lens = 35
    camera_data.clip_end = max(1000.0, radius * 20)
    return camera


def _render_scene_screenshot(filepath: str, max_size: int, image_format: str) -> tuple[int, int]:
    scene = bpy.context.scene
    render = scene.render
    original = {
        "filepath": render.filepath,
        "resolution_x": render.resolution_x,
        "resolution_y": render.resolution_y,
        "resolution_percentage": render.resolution_percentage,
        "file_format": render.image_settings.file_format,
        "camera": scene.camera,
    }
    temp_camera = None
    try:
        if scene.camera is None:
            temp_camera = _create_preview_camera()
            scene.camera = temp_camera

        render.filepath = filepath
        render.resolution_x = max_size
        render.resolution_y = max_size
        render.resolution_percentage = 100
        render.image_settings.file_format = image_format
        bpy.ops.render.render(write_still=True)
        return _resize_saved_image(filepath, max_size, image_format)
    finally:
        render.filepath = original["filepath"]
        render.resolution_x = original["resolution_x"]
        render.resolution_y = original["resolution_y"]
        render.resolution_percentage = original["resolution_percentage"]
        render.image_settings.file_format = original["file_format"]
        scene.camera = original["camera"]
        if temp_camera:
            temp_camera_data = temp_camera.data
            bpy.data.objects.remove(temp_camera, do_unlink=True)
            bpy.data.cameras.remove(temp_camera_data, do_unlink=True)


def get_viewport_screenshot(
    max_size: int = 800,
    filepath: str | None = None,
    format: str = "png",
    preview_size: int = 256,
) -> dict[str, Any]:
    """Capture the current 3D viewport, returning a usable image payload."""
    image_format, extension, mime_type = _normalise_image_format(format)
    max_size = max(64, min(int(max_size or 800), 4096))
    preview_size = max(64, min(int(preview_size or 256), max_size))
    output_path = _screenshot_filepath(filepath, extension)
    capture_method = "viewport"

    override = _find_view3d_override()
    if override:
        try:
            with bpy.context.temp_override(**override):
                bpy.ops.screen.screenshot_area(filepath=output_path)
            width, height = _resize_saved_image(output_path, max_size, image_format)
        except RuntimeError:
            capture_method = "render"
            width, height = _render_scene_screenshot(output_path, max_size, image_format)
    else:
        capture_method = "render"
        width, height = _render_scene_screenshot(output_path, max_size, image_format)

    model_data_url, model_width, model_height = _image_data_url(output_path, max_size, image_format, mime_type)
    preview_data_url, preview_width, preview_height = _image_data_url(output_path, preview_size, image_format, mime_type)

    result: dict[str, Any] = {
        "success": True,
        "width": width,
        "height": height,
        "filepath": output_path,
        "format": extension,
        "mime_type": mime_type,
        "capture_method": capture_method,
        "model_width": model_width,
        "model_height": model_height,
        "model_data_url": model_data_url,
        "preview_size": preview_size,
        "preview_width": preview_width,
        "preview_height": preview_height,
        "preview_data_url": preview_data_url,
    }
    return result


def render_scene_image(
    max_size: int = 800,
    filepath: str | None = None,
    format: str = "png",
    preview_size: int = 256,
) -> dict[str, Any]:
    """Render the current Blender scene and return a usable image payload."""
    image_format, extension, mime_type = _normalise_image_format(format)
    max_size = max(64, min(int(max_size or 800), 4096))
    preview_size = max(64, min(int(preview_size or 256), max_size))
    output_path = _screenshot_filepath(filepath, extension)

    width, height = _render_scene_screenshot(output_path, max_size, image_format)
    model_data_url, model_width, model_height = _image_data_url(output_path, max_size, image_format, mime_type)
    preview_data_url, preview_width, preview_height = _image_data_url(output_path, preview_size, image_format, mime_type)

    return {
        "success": True,
        "width": width,
        "height": height,
        "filepath": output_path,
        "format": extension,
        "mime_type": mime_type,
        "capture_method": "render",
        "model_width": model_width,
        "model_height": model_height,
        "model_data_url": model_data_url,
        "preview_size": preview_size,
        "preview_width": preview_width,
        "preview_height": preview_height,
        "preview_data_url": preview_data_url,
    }


def execute_code(code: str) -> dict[str, Any]:
    """Execute arbitrary Blender Python code.

    This legacy alias keeps parity with Blender Lab MCP's high-power control surface. The caller
    is expected to expose this only on localhost and to make the risk clear.
    """
    namespace = {"bpy": bpy, "mathutils": mathutils}
    capture_buffer = io.StringIO()
    with redirect_stdout(capture_buffer):
        exec(code, namespace)
    return {"executed": True, "result": capture_buffer.getvalue()}


def create_primitive(
    primitive_type: str = "cube",
    name: str | None = None,
    location: list[float] | None = None,
    scale: list[float] | None = None,
) -> dict[str, Any]:
    """Convenience tool for models that avoid Python code for simple objects."""
    location = location or [0, 0, 0]
    primitive_type = primitive_type.lower()

    ops = {
        "cube": bpy.ops.mesh.primitive_cube_add,
        "sphere": bpy.ops.mesh.primitive_uv_sphere_add,
        "uv_sphere": bpy.ops.mesh.primitive_uv_sphere_add,
        "cylinder": bpy.ops.mesh.primitive_cylinder_add,
        "cone": bpy.ops.mesh.primitive_cone_add,
        "plane": bpy.ops.mesh.primitive_plane_add,
        "torus": bpy.ops.mesh.primitive_torus_add,
    }
    op = ops.get(primitive_type)
    if not op:
        raise ValueError(f"Unsupported primitive_type: {primitive_type}")

    op(location=location)
    obj = bpy.context.object
    if name:
        obj.name = name
    if scale:
        obj.scale = scale
    bpy.context.view_layer.update()
    return get_object_detail_summary(obj.name)


def set_material(
    object_name: str,
    color: list[float] | None = None,
    material_name: str | None = None,
    roughness: float = 0.5,
    metallic: float = 0.0,
) -> dict[str, Any]:
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object not found: {object_name}")

    if not hasattr(obj, "data") or not hasattr(obj.data, "materials"):
        raise ValueError(f"Object {object_name} cannot accept materials")

    color_value = color or [0.8, 0.8, 0.8, 1.0]
    if len(color_value) != 4:
        raise ValueError("color must contain exactly 4 RGBA values")

    color_value = [max(0.0, min(float(channel), 1.0)) for channel in color_value]
    roughness = max(0.0, min(float(roughness), 1.0))
    metallic = max(0.0, min(float(metallic), 1.0))

    mat_name = material_name or f"{object_name}_Material"
    mat = bpy.data.materials.get(mat_name)
    if mat is None:
        mat = bpy.data.materials.new(mat_name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    output = nodes.new(type="ShaderNodeOutputMaterial")
    output.location = (300, 0)
    bsdf = nodes.new(type="ShaderNodeBsdfPrincipled")
    bsdf.location = (0, 0)
    links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])

    bsdf.inputs["Base Color"].default_value = color_value
    bsdf.inputs["Roughness"].default_value = roughness
    bsdf.inputs["Metallic"].default_value = metallic

    while len(obj.data.materials) > 0:
        obj.data.materials.pop(index=0)
    obj.data.materials.append(mat)

    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.context.view_layer.update()

    return {
        "success": True,
        "message": f"Applied material {mat.name} to {obj.name}",
        "object": obj.name,
        "material": mat.name,
        "material_info": {
            "name": mat.name,
            "has_nodes": mat.use_nodes,
            "node_count": len(mat.node_tree.nodes),
        },
    }


def _official_prompt() -> str:
    """Load Blender Lab's prompt text without adding a YAML dependency."""
    if not OFFICIAL_PROMPTS_PATH.exists():
        return ""
    text = OFFICIAL_PROMPTS_PATH.read_text(encoding="utf-8")
    marker = "initial_instructions: |"
    if marker not in text:
        return text
    body = text.split(marker, 1)[1].split("\n", 1)[1]
    lines = []
    for line in body.splitlines():
        if line and not line.startswith(" "):
            break
        lines.append(line[2:] if line.startswith("  ") else line)
    return "\n".join(lines).strip()


def _toolcode_expand_includes(toolcode_path: Path) -> str:
    lines = toolcode_path.read_text(encoding="utf-8").splitlines(True)
    result: list[str] = []
    skip = False
    for line in lines:
        if line.startswith("# @include_begin: "):
            include_name = line[len("# @include_begin: "):].strip()
            include_path = toolcode_path.parent / include_name
            result.append(include_path.read_text(encoding="utf-8"))
            if result[-1] and not result[-1].endswith("\n"):
                result.append("\n")
            skip = True
        elif skip:
            if line.startswith("# @include_end"):
                skip = False
        else:
            result.append(line)
    return "".join(result)


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(item) for item in value]
        return repr(value)


def _as_dict(value: Any) -> Any:
    if hasattr(value, "_asdict"):
        return dict(value._asdict())
    return value


def _run_official_toolcode(tool_name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run an official ``*_toolcode.py`` module directly inside this add-on."""
    toolcode_path = OFFICIAL_TOOLS_ROOT / f"{tool_name}_toolcode.py"
    if not toolcode_path.exists():
        raise ValueError(f"Official toolcode not found: {tool_name}")

    namespace: dict[str, Any] = {"__file__": str(toolcode_path), "__name__": f"_blmcp_{tool_name}"}
    exec(_toolcode_expand_includes(toolcode_path), namespace)
    params_type = namespace.get("Params")
    main = namespace["main"]
    call_params = None if params_type is None else params_type(**(params or {}))
    value = main(call_params)
    if callable(value):
        deferred = value()
        value = deferred if deferred is not None else {"status": "deferred", "message": "Operation started in Blender"}
    return _json_safe(_as_dict(value))


def execute_blender_code(code: str) -> dict[str, Any]:
    """Execute code using Blender Lab's result-variable calling convention."""
    namespace = {"bpy": bpy, "mathutils": mathutils, "result": {}}
    stdout_buffer = io.StringIO()
    stderr_text = ""
    with redirect_stdout(stdout_buffer):
        exec(code, namespace)
    result = _json_safe(namespace.get("result", {}))
    if not isinstance(result, dict):
        result = {"value": result}
    if stdout_buffer.getvalue():
        result["stdout"] = stdout_buffer.getvalue()
    if stderr_text:
        result["stderr"] = stderr_text
    return result


def execute_blender_code_for_cli(blend_file: str, code: str) -> dict[str, Any]:
    """Execute code in a background Blender process using only stdlib subprocess."""
    blend_path = Path(blend_file).expanduser()
    if not blend_path.exists():
        raise ValueError(f"Blend file not found: {blend_file}")

    marker = "__BLENDER_AGENT_CLI_RESULT__"
    script = (
        "import json, traceback\n"
        "result = {}\n"
        "try:\n"
        f"    exec({code!r}, globals())\n"
        "    payload = {'status': 'ok', 'result': result}\n"
        "except Exception as exc:\n"
        "    payload = {'status': 'error', 'message': str(exc), 'traceback': traceback.format_exc()}\n"
        f"print({marker!r} + json.dumps(payload, default=repr))\n"
    )

    script_file = tempfile.NamedTemporaryFile("w", suffix=".py", prefix="blender_agent_cli_", delete=False, encoding="utf-8")
    try:
        script_file.write(script)
        script_file.close()
        completed = subprocess.run(
            [bpy.app.binary_path, "--background", str(blend_path), "--python", script_file.name],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    finally:
        try:
            os.remove(script_file.name)
        except OSError:
            pass

    payload_line = next((line[len(marker):] for line in reversed(completed.stdout.splitlines()) if line.startswith(marker)), None)
    if payload_line is None:
        return {
            "status": "error",
            "message": "Background Blender run did not return a result payload",
            "returncode": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        }

    payload = json.loads(payload_line)
    payload["returncode"] = completed.returncode
    if completed.stderr:
        payload["stderr"] = completed.stderr[-4000:]
    return payload


def _iter_doc_paths(scope: str) -> list[Path]:
    root = OFFICIAL_DATA_ROOT / scope
    if not root.exists():
        return []
    return sorted(root.rglob("*.rst"))


def _doc_rel(path: Path) -> str:
    return path.relative_to(OFFICIAL_DATA_ROOT).as_posix()


def _doc_paragraphs(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]


# RST directive names that define addressable API members.
_DEFINITION_DIRECTIVES = (
    "attribute", "class", "classmethod", "data", "decorator",
    "exception", "function", "method", "property", "staticmethod",
)
_DEF_RE = re.compile(
    r"^\s*\.\. (?:" + "|".join(_DEFINITION_DIRECTIVES) + r")::\s*(.+)$",
    re.MULTILINE,
)


def _rst_list_definitions(text: str) -> list[str]:
    names: list[str] = []
    for m in _DEF_RE.finditer(text):
        raw = m.group(1).strip()
        name = raw.split("(", 1)[0].strip()
        if name:
            names.append(name)
    return names


def _rst_find_definition(text: str, name: str) -> str | None:
    lines = text.split("\n")
    name_lower = name.lower()
    for i, line in enumerate(lines):
        m = _DEF_RE.match(line)
        if not m:
            continue
        sig_name = m.group(1).strip().split("(", 1)[0].strip()
        if sig_name.lower() != name_lower:
            continue
        block_start = i
        block_end = i + 1
        while block_end < len(lines):
            ln = lines[block_end]
            if not ln.strip():
                block_end += 1
                continue
            if _DEF_RE.match(ln):
                break
            if ln[0] in (" ", "\t"):
                block_end += 1
                continue
            block_end += 1
        while block_end > block_start and not lines[block_end - 1].strip():
            block_end -= 1
        return "\n".join(lines[block_start:block_end])
    return None


def _list_direct_children(api_root: Path, identifier: str) -> list[str]:
    prefix = identifier + "."
    expected_dot = prefix.count(".")
    children: list[str] = []
    for entry in api_root.iterdir():
        if not entry.name.endswith(".rst") or not entry.name.startswith(prefix):
            continue
        stem = entry.name[:-4]
        if stem.count(".") != expected_dot:
            continue
        children.append(stem)
    children.sort()
    return children


def _list_identifiers_containing(api_root: Path, identifier: str) -> list[str]:
    buckets: list[set[tuple[str, ...]]] = []
    for entry in api_root.iterdir():
        if not entry.name.endswith(".rst"):
            continue
        parts = tuple(entry.name[:-4].split("."))
        if identifier not in parts:
            continue
        while len(buckets) <= len(parts):
            buckets.append(set())
        buckets[len(parts)].add(parts)
    for depth in range(len(buckets) - 1, 1, -1):
        buckets[depth] = {
            t for t in buckets[depth]
            if not any(t[:k] in buckets[k] for k in range(1, depth))
        }
    return sorted(".".join(t) for bucket in buckets for t in bucket)


def _extract_signature(paragraph: str) -> str:
    for line in paragraph.split("\n"):
        if _DEF_RE.match(line):
            return line.strip()
    return ""


def _search_docs(scope: str, query: str, max_results: int = 20, context: int = 0, index: int | None = None, compact: bool = False) -> dict[str, Any]:
    tokens = [token.lower() for token in re.split(r"\s+", query.strip()) if token.strip()]
    if not tokens:
        return {"query": query, "scope": scope, "hits": [], "truncated": False}

    hits: list[dict[str, Any]] = []
    for path in _iter_doc_paths(scope):
        rel = _doc_rel(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        paragraphs = _doc_paragraphs(text)
        for paragraph_index, paragraph in enumerate(paragraphs):
            haystack = f"{rel}\n{paragraph}".lower()
            if not all(token in haystack for token in tokens):
                continue
            score = sum(haystack.count(token) for token in tokens)
            if compact:
                sig = _extract_signature(paragraph)
                hits.append({
                    "path": rel,
                    "signature": sig,
                    "breadcrumb": "",
                    "index": len(hits),
                    "score": score,
                })
            else:
                start = max(0, paragraph_index - max(0, context))
                end = min(len(paragraphs), paragraph_index + max(0, context) + 1)
                hits.append({
                    "path": rel,
                    "text": "\n\n".join(paragraphs[start:end]),
                    "breadcrumb": "",
                    "index": len(hits),
                    "score": score,
                })
    hits.sort(key=lambda item: item["score"], reverse=True)
    for idx, hit in enumerate(hits):
        hit["index"] = idx
    if index is not None:
        hits = [hits[index]] if 0 <= index < len(hits) else []
    truncated = len(hits) > max_results
    return {"query": query, "scope": scope, "hits": hits[:max_results], "truncated": truncated}


def search_api_docs(query: str, max_results: int = 20, context: int = 0, index: int | None = None, compact: bool = False) -> dict[str, Any]:
    return _search_docs("api", query, max_results, context, index, compact=compact)


def search_manual_docs(query: str, max_results: int = 20, context: int = 0, index: int | None = None, compact: bool = False) -> dict[str, Any]:
    return _search_docs("manual", query, max_results, context, index, compact=compact)


def get_python_api_docs(identifier: str) -> dict[str, Any]:
    api_root = OFFICIAL_DATA_ROOT / "api"
    api_resolved = api_root.resolve()
    safe = identifier.strip().strip("/").removesuffix(".rst")
    _SUMMARY_THRESHOLD = 32 * 1024

    def _resolve(stem: str) -> Path | None:
        p = (api_root / f"{stem}.rst").resolve()
        if not str(p).startswith(str(api_resolved)) or not p.exists():
            return None
        return p

    def _read_file(p: Path, stem: str) -> dict[str, Any]:
        text = p.read_text(encoding="utf-8", errors="replace")
        if len(text) <= _SUMMARY_THRESHOLD:
            return {"kind": "exact", "found": True, "identifier": safe, "path": _doc_rel(p), "text": text}
        definition = None
        parts = safe.split(".")
        for strip_count in range(1, len(parts)):
            tail = ".".join(parts[-strip_count:])
            hit = _rst_find_definition(text, tail)
            if hit is not None:
                definition = hit
                break
        if definition is not None:
            return {"kind": "definition", "found": True, "identifier": safe, "path": _doc_rel(p), "text": definition}
        defs = _rst_list_definitions(text)
        summary = (
            f"File too large to inline ({len(text) // 1024} KB, threshold {_SUMMARY_THRESHOLD // 1024} KB); "
            f"definitions listed below. Query individual members as `{stem}.<name>`:\n\n"
            + "\n".join(f"- {d}" for d in defs[:300])
        )
        return {"kind": "exact", "found": True, "identifier": safe, "path": _doc_rel(p), "text": summary}

    # 1. Exact file match.
    if (exact := _resolve(safe)) is not None:
        return _read_file(exact, safe)

    # 2. Intra-file definition lookup: strip trailing components to find
    #    parent RST, then search for the definition inside it.
    parts = safe.split(".")
    for strip_count in range(1, len(parts)):
        prefix = ".".join(parts[:-strip_count])
        tail = ".".join(parts[-strip_count:])
        parent = _resolve(prefix)
        if parent is None:
            continue
        text = parent.read_text(encoding="utf-8", errors="replace")
        hit = _rst_find_definition(text, tail)
        if hit is not None:
            return {"kind": "definition", "found": True, "identifier": safe, "path": _doc_rel(parent), "text": hit}
        defs = _rst_list_definitions(text)
        children = _list_direct_children(api_root, prefix)
        tail_chars = set(tail.lower())
        similar = [c for c in children if tail_chars.issubset(c.rsplit(".", 1)[-1].lower())]
        return {
            "kind": "partial", "found": False, "identifier": safe,
            "parent": prefix, "available": defs, "submodules": similar[:50],
        }

    # 3. Namespace: `<identifier>.<child>.rst` files exist.
    children = _list_direct_children(api_root, safe)
    if children:
        return {"kind": "namespace", "found": True, "identifier": safe, "submodules": children}

    # 4. "Did you mean" fallback.
    suggestions = _list_identifiers_containing(api_root, safe)
    if suggestions:
        return {"kind": "suggestions", "found": False, "identifier": safe, "suggestions": suggestions[:50]}

    return {"kind": "missing", "found": False, "identifier": safe}


def get_objects_summary() -> dict[str, Any]:
    return _run_official_toolcode("get_objects_summary")


def get_object_detail_summary(name: str) -> dict[str, Any]:
    return _run_official_toolcode("get_object_detail_summary", {"name": name})


def get_blendfile_summary_path_info() -> dict[str, Any]:
    return _run_official_toolcode("get_blendfile_summary_path_info")


def get_blendfile_summary_datablocks() -> dict[str, Any]:
    return _run_official_toolcode("get_blendfile_summary_datablocks")


def get_blendfile_summary_missing_files() -> dict[str, Any]:
    return _run_official_toolcode("get_blendfile_summary_missing_files")


def get_blendfile_summary_of_linked_libraries() -> dict[str, Any]:
    return _run_official_toolcode("get_blendfile_summary_of_linked_libraries")


def get_blendfile_summary_usage_guess() -> dict[str, Any]:
    return _run_official_toolcode("get_blendfile_summary_usage_guess")


def jump_to_tab_by_name(name: str) -> dict[str, Any]:
    return _run_official_toolcode("jump_to_tab_by_name", {"name": name})


def jump_to_tab_by_space_type(space_type: str, allow_edits: bool = False) -> dict[str, Any]:
    return _run_official_toolcode("jump_to_tab_by_space_type", {"space_type": space_type, "allow_edits": allow_edits})


def jump_to_view3d_object_by_name(name: str, allow_edits: bool = False) -> dict[str, Any]:
    return _run_official_toolcode("jump_to_view3d_object_by_name", {"name": name, "allow_edits": allow_edits})


def jump_to_view3d_object_data_by_name(name: str, allow_edits: bool = False) -> dict[str, Any]:
    return _run_official_toolcode("jump_to_view3d_object_data_by_name", {"name": name, "allow_edits": allow_edits})


def render_viewport_to_path(output_path: str) -> dict[str, Any]:
    return _run_official_toolcode("render_viewport_to_path", {"output_path": output_path})


def render_thumbnail_to_path(output_path: str) -> dict[str, Any]:
    return _run_official_toolcode("render_thumbnail_to_path", {"output_path": output_path})


def align_objects(
    object_names: list[str],
    axis: str,
    mode: str,
    reference: str = "selection",
) -> dict[str, Any]:
    """Align multiple objects along a specified axis."""
    return _run_official_toolcode("align_objects", {
        "object_names": object_names,
        "axis": axis,
        "mode": mode,
        "reference": reference,
    })


def distribute_objects(
    object_names: list[str],
    axis: str,
    mode: str = "even",
    gap_size: float = 0.0,
) -> dict[str, Any]:
    """Distribute multiple objects evenly along a specified axis."""
    return _run_official_toolcode("distribute_objects", {
        "object_names": object_names,
        "axis": axis,
        "mode": mode,
        "gap_size": gap_size,
    })


def snap_objects(
    object_names: list[str],
    target: str,
    snap_point: str = "center",
    axes: list[str] = ("x", "y", "z"),
) -> dict[str, Any]:
    """Snap objects to a specified target."""
    return _run_official_toolcode("snap_objects", {
        "object_names": object_names,
        "target": target,
        "snap_point": snap_point,
        "axes": axes,
    })


def get_screenshot_of_window_as_json() -> dict[str, Any]:
    return _run_official_toolcode("get_screenshot_of_window_as_json")


def get_screenshot_of_window_as_image(size_limit_in_bytes: int = 0) -> dict[str, Any]:
    del size_limit_in_bytes
    return get_viewport_screenshot()


def get_screenshot_of_area_as_image(area_ui_type: str, size_limit_in_bytes: int = 0) -> dict[str, Any]:
    del area_ui_type, size_limit_in_bytes
    return get_viewport_screenshot()


def dispatch_command(command: dict[str, Any]) -> dict[str, Any]:
    """Execute a Blender Lab MCP-style command: {"type": name, "params": {...}}."""
    cmd_type = command.get("type")
    params = command.get("params") or {}
    handler = HANDLERS.get(cmd_type)
    if not handler:
        return {"status": "error", "message": f"Unknown command type: {cmd_type}"}

    try:
        return {"status": "success", "result": handler(**params)}
    except Exception as exc:
        traceback.print_exc()
        return {"status": "error", "message": str(exc)}


HANDLERS: dict[str, ToolHandler] = {
    "execute_blender_code": execute_blender_code,
    "execute_blender_code_for_cli": execute_blender_code_for_cli,
    "get_blendfile_summary_path_info": get_blendfile_summary_path_info,
    "get_blendfile_summary_datablocks": get_blendfile_summary_datablocks,
    "get_blendfile_summary_missing_files": get_blendfile_summary_missing_files,
    "get_blendfile_summary_of_linked_libraries": get_blendfile_summary_of_linked_libraries,
    "get_blendfile_summary_usage_guess": get_blendfile_summary_usage_guess,
    "get_objects_summary": get_objects_summary,
    "get_object_detail_summary": get_object_detail_summary,
    "get_python_api_docs": get_python_api_docs,
    "search_api_docs": search_api_docs,
    "search_manual_docs": search_manual_docs,
    "get_screenshot_of_window_as_json": get_screenshot_of_window_as_json,
    "get_screenshot_of_window_as_image": get_screenshot_of_window_as_image,
    "get_screenshot_of_area_as_image": get_screenshot_of_area_as_image,
    "jump_to_tab_by_name": jump_to_tab_by_name,
    "jump_to_tab_by_space_type": jump_to_tab_by_space_type,
    "jump_to_view3d_object_by_name": jump_to_view3d_object_by_name,
    "jump_to_view3d_object_data_by_name": jump_to_view3d_object_data_by_name,
    "render_viewport_to_path": render_viewport_to_path,
    "render_thumbnail_to_path": render_thumbnail_to_path,
    "align_objects": align_objects,
    "distribute_objects": distribute_objects,
    "snap_objects": snap_objects,
    "create_primitive": create_primitive,
    # Compatibility aliases for the local UI and older saved sessions.
    "get_scene_info": get_objects_summary,
    "get_object_info": get_object_detail_summary,
    "get_viewport_screenshot": get_viewport_screenshot,
    "render_scene_image": render_scene_image,
    "execute_code": execute_blender_code,
}


def _tool(name: str, description: str, properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }


_STRING = {"type": "string"}
_BOOL = {"type": "boolean"}
_INT = {"type": "integer"}
_DOC_SEARCH_PARAMS = {
    "query": _STRING,
    "max_results": {"type": "integer", "default": 20},
    "context": {"type": "integer", "default": 0},
    "index": {"type": ["integer", "null"], "default": None},
    "compact": {"type": "boolean", "default": False},
}


OPENAI_TOOLS: list[dict[str, Any]] = [
    _tool("execute_blender_code", "Execute Python code in the active Blender session. Assign a JSON-serialisable dict to `result` to return data.", {"code": _STRING}, ["code"]),
    _tool("execute_blender_code_for_cli", "Execute Python code in a background Blender process for a specific .blend file. The blend_file must be an absolute path. Assign a JSON-serialisable dict to `result` to return data.", {"blend_file": _STRING, "code": _STRING}, ["blend_file", "code"]),
    _tool("get_blendfile_summary_path_info", "Simple/fast access to the blend file's path, save status, age, and backups."),
    _tool("get_blendfile_summary_datablocks", "Return a summary of data-block counts, active workspace, and render engine."),
    _tool("get_blendfile_summary_missing_files", "Report external file references that are missing from disk."),
    _tool("get_blendfile_summary_of_linked_libraries", "Return a tree of directly and indirectly linked library files."),
    _tool("get_blendfile_summary_usage_guess", "Guess the primary use-cases of the current blend file, scored 0-100 with certainty."),
    _tool("get_objects_summary", "Return the scene's collection hierarchy and objects: name, type, parent, data name, selection, and visibility."),
    _tool("get_object_detail_summary", "Return a structured summary of the object identified by name.", {"name": _STRING}, ["name"]),
    _tool("get_python_api_docs", "Return bundled Blender Python API docs for an identifier. Supports dotted names (e.g. bpy.ops.mesh.primitive_cube_add) - searches inside parent RST files for specific definitions.", {"identifier": _STRING}, ["identifier"]),
    _tool("search_api_docs", "Full-text search over bundled Blender Python API RST docs. Use compact=true for signature-only results (fewer tokens, more hits).", _DOC_SEARCH_PARAMS, ["query"]),
    _tool("search_manual_docs", "Full-text search over bundled Blender Manual RST docs. Use compact=true for signature-only results (fewer tokens, more hits).", _DOC_SEARCH_PARAMS, ["query"]),
    _tool("get_screenshot_of_window_as_json", "Return a JSON description of the Blender window layout, areas, active object, and selection."),
    _tool("get_screenshot_of_window_as_image", "Take a screenshot of the Blender window and return an image payload.", {"size_limit_in_bytes": _INT}),
    _tool("get_screenshot_of_area_as_image", "Take a screenshot of a single Blender area and return an image payload.", {"area_ui_type": _STRING, "size_limit_in_bytes": _INT}, ["area_ui_type"]),
    _tool("jump_to_tab_by_name", "Switch the active workspace tab to name.", {"name": _STRING}, ["name"]),
    _tool("jump_to_tab_by_space_type", "Switch to a workspace whose main area matches space_type.", {"space_type": _STRING, "allow_edits": _BOOL}, ["space_type"]),
    _tool("jump_to_view3d_object_by_name", "Move the 3D viewport to focus on an object by name.", {"name": _STRING, "allow_edits": _BOOL}, ["name"]),
    _tool("jump_to_view3d_object_data_by_name", "Move the 3D viewport to the object whose data-block name matches name (e.g. mesh data name, not object name).", {"name": _STRING, "allow_edits": _BOOL}, ["name"]),
    _tool("render_viewport_to_path", "Render the current scene to output_path using current render settings.", {"output_path": _STRING}, ["output_path"]),
    _tool("render_thumbnail_to_path", "Render a small, low-quality thumbnail to output_path.", {"output_path": _STRING}, ["output_path"]),
    _tool("align_objects", "Align multiple objects along a specified axis. Supports different alignment modes and reference points.", {
        "object_names": {"type": "array", "items": _STRING, "description": "List of object names to align"},
        "axis": {**_STRING, "description": "Axis to align along ('x', 'y', or 'z')"},
        "mode": {**_STRING, "description": "Alignment mode: 'min' (bottom/left/back), 'max' (top/right/front), 'center'"},
        "reference": {**_STRING, "default": "selection", "description": "Reference: 'selection', 'active', 'cursor', 'world_origin'"},
    }, ["object_names", "axis", "mode"]),
    _tool("distribute_objects", "Distribute multiple objects evenly along a specified axis.", {
        "object_names": {"type": "array", "items": _STRING, "description": "List of object names to distribute"},
        "axis": {**_STRING, "description": "Axis to distribute along ('x', 'y', or 'z')"},
        "mode": {**_STRING, "default": "even", "description": "Distribution mode: 'even' (equal spacing), 'gap' (specified gap)"},
        "gap_size": {"type": "number", "default": 0.0, "description": "Gap size in Blender units (only for mode='gap')"},
    }, ["object_names", "axis"]),
    _tool("snap_objects", "Snap objects to a specified target (cursor, grid, active object, world origin).", {
        "object_names": {"type": "array", "items": _STRING, "description": "List of object names to snap"},
        "target": {**_STRING, "description": "Target: 'cursor', 'active', 'grid', 'world_origin'"},
        "snap_point": {**_STRING, "default": "center", "description": "Snap point: 'center', 'min' (bottom/left/back), 'max' (top/right/front)"},
        "axes": {"type": "array", "items": _STRING, "default": ["x", "y", "z"], "description": "Axes to snap"},
    }, ["object_names", "target"]),
    _tool("create_primitive", "Create a primitive 3D object (cube, sphere, cylinder, etc.) at a specified location.", {
        "primitive_type": {**_STRING, "description": "Type: 'cube', 'sphere', 'uv_sphere', 'cylinder', 'cone', 'plane', 'torus'"},
        "name": {**_STRING, "description": "Object name (optional)"},
        "location": {"type": "array", "items": {"type": "number"}, "description": "Location [x, y, z] (default: [0, 0, 0])"},
        "scale": {"type": "array", "items": {"type": "number"}, "description": "Scale [x, y, z] (optional)"},
    }, ["primitive_type"]),
]

ASK_TOOL_NAMES = {
    "get_blendfile_summary_path_info",
    "get_blendfile_summary_datablocks",
    "get_blendfile_summary_missing_files",
    "get_blendfile_summary_of_linked_libraries",
    "get_blendfile_summary_usage_guess",
    "get_objects_summary",
    "get_object_detail_summary",
    "get_python_api_docs",
    "search_api_docs",
    "search_manual_docs",
    "get_screenshot_of_window_as_json",
    "get_screenshot_of_window_as_image",
    "get_screenshot_of_area_as_image",
}


def tools_for_mode(mode: str) -> list[dict[str, Any]]:
    if mode == "ask":
        return [tool for tool in OPENAI_TOOLS if tool["function"]["name"] in ASK_TOOL_NAMES]
    return OPENAI_TOOLS


SYSTEM_PROMPT = _official_prompt() or """You have access to Blender tools to interact with a Blender scene directly.
You are running inside Blender Agent's embedded local runtime.

IMPORTANT: Respect existing structure and naming conventions.
NEVER assume missing values - inspect the scene first.
Do not destructively modify objects without confirmation.

# Tool Priority

ALWAYS prefer dedicated tools over `execute_blender_code`:
- Creating objects → `create_primitive`
- Aligning objects → `align_objects`
- Distributing objects → `distribute_objects`
- Snapping objects → `snap_objects`
- Getting object info → `get_object_detail_summary`
- Setting materials → `set_material`

Only use `execute_blender_code` when NO dedicated tool exists for the task.

Use `execute_blender_code` only when the other Blender tools do not provide the
functionality you need. Return structured data from executed code by assigning a
JSON-serialisable dict to `result`.
"""


def tools_as_json() -> str:
    return json.dumps(OPENAI_TOOLS, indent=2)

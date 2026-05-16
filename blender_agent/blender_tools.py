"""Blender command tools adapted from Blender Lab's official blender_mcp.

The tool surface, prompt, and bundled documentation are sourced from:
https://projects.blender.org/lab/blender_mcp
"""

from __future__ import annotations

import io
import json
import base64
import re
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


def _png_dimensions(image_bytes: bytes) -> tuple[int | None, int | None]:
    if len(image_bytes) < 24 or image_bytes[:8] != b"\x89PNG\r\n\x1a\n" or image_bytes[12:16] != b"IHDR":
        return None, None
    width = int.from_bytes(image_bytes[16:20], "big")
    height = int.from_bytes(image_bytes[20:24], "big")
    return width, height


def _official_image_tool_payload(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    status = result.get("status")
    if status != "ok":
        return result

    encoded = result.get("image_base64")
    if not isinstance(encoded, str):
        return {"status": "error", "message": f"{tool_name} did not return image data"}

    image_bytes = base64.b64decode(encoded)
    width, height = _png_dimensions(image_bytes)
    data_url = f"data:image/png;base64,{encoded}"
    return {
        "success": True,
        "width": width,
        "height": height,
        "format": "png",
        "mime_type": "image/png",
        "capture_method": "window" if tool_name == "get_screenshot_of_window_as_image" else "area",
        "model_width": width,
        "model_height": height,
        "model_data_url": data_url,
        "preview_size": max(width or 0, height or 0) or None,
        "preview_width": width,
        "preview_height": height,
        "preview_data_url": data_url,
    }


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


def _search_docs(scope: str, query: str, max_results: int = 20, context: int = 0, index: int | None = None) -> dict[str, Any]:
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


def search_api_docs(query: str, max_results: int = 20, context: int = 0, index: int | None = None) -> dict[str, Any]:
    return _search_docs("api", query, max_results, context, index)


def search_manual_docs(query: str, max_results: int = 20, context: int = 0, index: int | None = None) -> dict[str, Any]:
    return _search_docs("manual", query, max_results, context, index)


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


def get_screenshot_of_window_as_json() -> dict[str, Any]:
    return _run_official_toolcode("get_screenshot_of_window_as_json")


def get_screenshot_of_window_as_image(size_limit_in_bytes: int = 0) -> dict[str, Any]:
    result = _run_official_toolcode("get_screenshot_of_window_as_image", {"size_limit_in_bytes": size_limit_in_bytes})
    return _official_image_tool_payload("get_screenshot_of_window_as_image", result)


def get_screenshot_of_area_as_image(area_ui_type: str, size_limit_in_bytes: int = 0) -> dict[str, Any]:
    result = _run_official_toolcode(
        "get_screenshot_of_area_as_image",
        {"area_ui_type": area_ui_type, "size_limit_in_bytes": size_limit_in_bytes},
    )
    return _official_image_tool_payload("get_screenshot_of_area_as_image", result)


def list_scene_objects() -> dict[str, Any]:
    """Return selectable scene instances for the local web UI."""
    active = bpy.context.view_layer.objects.active
    objects = []
    for obj in sorted(bpy.context.scene.objects, key=lambda item: item.name.lower()):
        objects.append(
            {
                "name": obj.name,
                "type": obj.type,
                "data_name": obj.data.name if getattr(obj, "data", None) else None,
                "visible": bool(obj.visible_get()),
                "selected": bool(obj.select_get()),
                "active": bool(active and obj.name == active.name),
            }
        )
    return {"objects": objects}


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
    "list_scene_objects": list_scene_objects,
    "jump_to_tab_by_name": jump_to_tab_by_name,
    "jump_to_tab_by_space_type": jump_to_tab_by_space_type,
    "jump_to_view3d_object_by_name": jump_to_view3d_object_by_name,
    "jump_to_view3d_object_data_by_name": jump_to_view3d_object_data_by_name,
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
}


OPENAI_TOOLS: list[dict[str, Any]] = [
    _tool("execute_blender_code", "Execute Python code in the active Blender session. Assign a JSON-serialisable dict to `result` to return data.", {"code": _STRING}, ["code"]),
    _tool("get_blendfile_summary_path_info", "Simple/fast access to the blend file's path, save status, age, and backups."),
    _tool("get_blendfile_summary_datablocks", "Return a summary of data-block counts, active workspace, and render engine."),
    _tool("get_blendfile_summary_missing_files", "Report external file references that are missing from disk."),
    _tool("get_blendfile_summary_of_linked_libraries", "Return a tree of directly and indirectly linked library files."),
    _tool("get_blendfile_summary_usage_guess", "Guess the primary use-cases of the current blend file, scored 0-100 with certainty."),
    _tool("get_objects_summary", "Return the scene's collection hierarchy and objects: name, type, parent, data name, selection, and visibility."),
    _tool("get_object_detail_summary", "Return a structured summary of the object identified by name.", {"name": _STRING}, ["name"]),
    _tool("get_python_api_docs", "Return bundled Blender Python API docs for an identifier. Supports dotted names (e.g. bpy.ops.mesh.primitive_cube_add) - searches inside parent RST files for specific definitions.", {"identifier": _STRING}, ["identifier"]),
    _tool("search_api_docs", "Full-text search over bundled Blender Python API RST docs.", _DOC_SEARCH_PARAMS, ["query"]),
    _tool("search_manual_docs", "Full-text search over bundled Blender Manual RST docs.", _DOC_SEARCH_PARAMS, ["query"]),
    _tool("get_screenshot_of_window_as_json", "Return a JSON description of the Blender window layout, areas, active object, and selection."),
    _tool("get_screenshot_of_window_as_image", "Take a screenshot of the Blender window and return an image payload.", {"size_limit_in_bytes": _INT}),
    _tool("get_screenshot_of_area_as_image", "Take a screenshot of a single Blender area and return an image payload.", {"area_ui_type": _STRING, "size_limit_in_bytes": _INT}, ["area_ui_type"]),
    _tool("jump_to_tab_by_name", "Switch the active workspace tab to name.", {"name": _STRING}, ["name"]),
    _tool("jump_to_tab_by_space_type", "Switch to a workspace whose main area matches space_type.", {"space_type": _STRING, "allow_edits": _BOOL}, ["space_type"]),
    _tool("jump_to_view3d_object_by_name", "Move the 3D viewport to focus on an object by name.", {"name": _STRING, "allow_edits": _BOOL}, ["name"]),
    _tool("jump_to_view3d_object_data_by_name", "Move the 3D viewport to the object whose data-block name matches name (e.g. mesh data name, not object name).", {"name": _STRING, "allow_edits": _BOOL}, ["name"]),
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
Blender must be running with the MCP add-on enabled and connected.

Use `execute_blender_code` only when the other Blender tools do not provide the
functionality you need. Return structured data from executed code by assigning a
JSON-serialisable dict to `result`.
"""


def tools_as_json() -> str:
    return json.dumps(OPENAI_TOOLS, indent=2)

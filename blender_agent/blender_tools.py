"""Blender command tools adapted from Blender Lab's official blender_mcp.

The tool surface, prompt, and bundled documentation are sourced from:
https://projects.blender.org/lab/blender_mcp
"""

from __future__ import annotations

import io
import json
import base64
import difflib
import os
import re
import sys
import traceback
import importlib.util
import types
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


def _official_rst_search(
    query: str,
    scope: str,
    max_results: int = 20,
    context: int = 0,
    index: int | None = None,
) -> dict[str, Any]:
    """Dispatch doc search to the vendored official Blender MCP implementation."""
    blmcp_root = OFFICIAL_MCP_ROOT
    helpers_root = blmcp_root / "tools_helpers"

    if "blmcp" not in sys.modules:
        pkg = types.ModuleType("blmcp")
        pkg.__path__ = [str(blmcp_root)]  # type: ignore[attr-defined]
        sys.modules["blmcp"] = pkg
    if "blmcp.tools_helpers" not in sys.modules:
        pkg = types.ModuleType("blmcp.tools_helpers")
        pkg.__path__ = [str(helpers_root)]  # type: ignore[attr-defined]
        sys.modules["blmcp.tools_helpers"] = pkg

    parse_name = "blmcp.tools_helpers.rst_parse_docs"
    if parse_name not in sys.modules:
        parse_spec = importlib.util.spec_from_file_location(
            parse_name,
            helpers_root / "rst_parse_docs.py",
        )
        if parse_spec is None or parse_spec.loader is None:
            raise ImportError("Unable to load vendored rst_parse_docs.py")
        parse_module = importlib.util.module_from_spec(parse_spec)
        sys.modules[parse_name] = parse_module
        try:
            parse_spec.loader.exec_module(parse_module)
        except ModuleNotFoundError as exc:
            if exc.name == "docutils":
                raise RuntimeError(
                    "Official Blender MCP search requires the `docutils` package in Blender's Python environment."
                ) from exc
            raise

    search_name = "blmcp.tools_helpers.rst_doc_search"
    module = sys.modules.get(search_name)
    if module is None:
        search_spec = importlib.util.spec_from_file_location(
            search_name,
            helpers_root / "rst_doc_search.py",
        )
        if search_spec is None or search_spec.loader is None:
            raise ImportError("Unable to load vendored rst_doc_search.py")
        module = importlib.util.module_from_spec(search_spec)
        sys.modules[search_name] = module
        try:
            search_spec.loader.exec_module(module)
        except ModuleNotFoundError as exc:
            if exc.name == "docutils":
                raise RuntimeError(
                    "Official Blender MCP search requires the `docutils` package in Blender's Python environment."
                ) from exc
            raise

    return module.search(
        query=query,
        scope=scope,
        max_results=max_results,
        context=context,
        index=index,
    )


def _official_rst_parse_module() -> Any:
    """Load the vendored official rst_parse_docs helper module."""
    blmcp_root = OFFICIAL_MCP_ROOT
    helpers_root = blmcp_root / "tools_helpers"

    if "blmcp" not in sys.modules:
        pkg = types.ModuleType("blmcp")
        pkg.__path__ = [str(blmcp_root)]  # type: ignore[attr-defined]
        sys.modules["blmcp"] = pkg
    if "blmcp.tools_helpers" not in sys.modules:
        pkg = types.ModuleType("blmcp.tools_helpers")
        pkg.__path__ = [str(helpers_root)]  # type: ignore[attr-defined]
        sys.modules["blmcp.tools_helpers"] = pkg

    parse_name = "blmcp.tools_helpers.rst_parse_docs"
    module = sys.modules.get(parse_name)
    if module is None:
        parse_spec = importlib.util.spec_from_file_location(
            parse_name,
            helpers_root / "rst_parse_docs.py",
        )
        if parse_spec is None or parse_spec.loader is None:
            raise ImportError("Unable to load vendored rst_parse_docs.py")
        module = importlib.util.module_from_spec(parse_spec)
        sys.modules[parse_name] = module
        try:
            parse_spec.loader.exec_module(module)
        except ModuleNotFoundError as exc:
            if exc.name == "docutils":
                raise RuntimeError(
                    "Official Blender MCP docs tools require the `docutils` package in Blender's Python environment."
                ) from exc
            raise
    return module


def _search_docs(scope: str, query: str, max_results: int = 20, context: int = 0, index: int | None = None) -> dict[str, Any]:
    return _official_rst_search(
        query=query,
        scope=scope,
        max_results=max_results,
        context=context,
        index=index,
    )


def search_api_docs(query: str, max_results: int = 20, context: int = 0, index: int | None = None) -> dict[str, Any]:
    return _search_docs("api", query, max_results, context, index)


def search_manual_docs(query: str, max_results: int = 20, context: int = 0, index: int | None = None) -> dict[str, Any]:
    return _search_docs("manual", query, max_results, context, index)


_DOC_EXT = ".rst"
_SUMMARY_CHAR_THRESHOLD = 32 * 1024
_LITERALINCLUDE_PREFIX = ".. literalinclude::"
_LINES_OPTION_PREFIX = ":lines:"


def _line_bounds_from_index(content: str, index: int) -> tuple[int, int]:
    beg = content.rfind("\n", 0, index) + 1
    end = content.find("\n", index)
    if end == -1:
        end = len(content)
    return beg, end


def _resolve_inside(api_path: str, stem: str) -> str | None:
    candidate = os.path.join(api_path, f"{stem}{_DOC_EXT}")
    candidate_path = os.path.realpath(candidate)
    if not candidate_path.startswith(api_path + os.sep):
        return None
    if not os.path.isfile(candidate_path):
        return None
    return candidate_path


def _list_direct_child_identifiers(api_path: str, identifier: str) -> list[str]:
    prefix = identifier + "."
    expected_dot_count = prefix.count(".")
    children: list[str] = []
    for name in os.listdir(api_path):
        if not name.endswith(_DOC_EXT) or not name.startswith(prefix):
            continue
        stem = name[:-len(_DOC_EXT)]
        if stem.count(".") != expected_dot_count:
            continue
        children.append(stem)
    children.sort()
    return children


def _list_identifiers_containing_component(api_path: str, identifier: str) -> list[str]:
    buckets: list[set[tuple[str, ...]]] = []
    for name in os.listdir(api_path):
        if not name.endswith(_DOC_EXT):
            continue
        parts = tuple(name[:-len(_DOC_EXT)].split("."))
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


def _summarize_rst_for_size(identifier: str, path: str, size: int, parse_module: Any) -> str:
    defs = parse_module.list_doctree_definitions(parse_module.doctree_for_path(path))
    header = (
        "File too large to inline ({:d} KB, threshold {:d} KB); "
        "definitions listed below. Query individual members as "
        "`{:s}.<name>`:\n\n"
    ).format(size // 1024, _SUMMARY_CHAR_THRESHOLD // 1024, identifier)
    if not defs:
        return header + "(no top-level definitions found)"
    return header + "\n".join(f"- {d}" for d in defs)


def _list_top_level_modules(api_path: str) -> list[str]:
    names = os.listdir(api_path)
    rst_names = {name for name in names if name.endswith(_DOC_EXT)}
    firsts = sorted({n[:-len(_DOC_EXT)].split(".", 1)[0] for n in rst_names})
    modules: list[str] = []
    for first in firsts:
        prefix = first + "."
        root_rst = first + _DOC_EXT
        if any(name != root_rst and name.startswith(prefix) for name in rst_names):
            modules.append(first)
            continue
        if root_rst not in rst_names:
            continue
        root_path = os.path.join(api_path, root_rst)
        with open(root_path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
        if head.startswith(".. module::") or "\n.. module::" in head:
            modules.append(first)
    return modules


def _filter_submodules_by_tail(submodules: list[str], tail: str) -> list[str]:
    tail_chars = set(tail)
    keep = [s for s in submodules if tail_chars.issubset(s.rsplit(".", 1)[-1])]
    keep.sort(
        key=lambda candidate: (
            -difflib.SequenceMatcher(a=tail, b=candidate.rsplit(".", 1)[-1]).ratio(),
            candidate,
        ),
    )
    return keep


def _lines_option_after(content: str, start: int) -> str | None:
    pos = start
    while pos < len(content):
        line_end = content.find("\n", pos)
        if line_end == -1:
            line_end = len(content)
        stripped = content[pos:line_end].lstrip()
        if not stripped or not stripped.startswith(":"):
            return None
        if stripped.startswith(_LINES_OPTION_PREFIX):
            return stripped[len(_LINES_OPTION_PREFIX):].strip()
        pos = line_end + 1
    return None


def _apply_lines_spec(text: str, spec: str) -> str:
    source = text.splitlines(keepends=True)
    total = len(source)
    out: list[str] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        lo, sep, hi = part.partition("-")
        try:
            lo_i = int(lo) if lo else 1
            hi_i = int(hi) if hi else total
        except ValueError:
            continue
        if not sep:
            hi_i = lo_i
        out.extend(source[max(0, lo_i - 1):hi_i])
    return "".join(out)


def _collect_examples(content: str, api_path: str) -> list[dict[str, str]]:
    examples: list[dict[str, str]] = []
    seen: set[tuple[str, str | None]] = set()
    pos = 0
    while (index := content.find(_LITERALINCLUDE_PREFIX, pos)) != -1:
        beg, end = _line_bounds_from_index(content, index)
        pos = end + 1
        if content[beg:index].strip():
            continue
        tokens = content[index + len(_LITERALINCLUDE_PREFIX):end].split()
        if not tokens:
            continue
        filepath_rel = tokens[0].removeprefix("./")
        lines_spec = _lines_option_after(content, end + 1)
        target_path = os.path.realpath(os.path.join(api_path, filepath_rel))
        if not target_path.startswith(api_path + os.sep):
            continue
        dedup_key = (target_path, lines_spec)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        try:
            with open(target_path, encoding="utf-8", errors="replace") as fh:
                example_content = fh.read()
        except OSError:
            continue
        if lines_spec is not None:
            example_content = _apply_lines_spec(example_content, lines_spec)
        examples.append({"path": filepath_rel, "content": example_content})
    return examples


def get_python_api_docs(identifier: str) -> dict[str, Any]:
    parse_module = _official_rst_parse_module()
    api_path = os.path.realpath(os.path.join(parse_module.data_dir(), "api"))
    identifier = identifier.strip()

    if identifier == "*" or identifier.endswith(".*"):
        if identifier == "*":
            submodules = _list_top_level_modules(api_path)
        else:
            submodules = _list_direct_child_identifiers(api_path, identifier[:-2])
        return {
            "kind": "namespace",
            "found": True,
            "identifier": identifier,
            "submodules": submodules,
        }

    if (candidate_path := _resolve_inside(api_path, identifier)) is not None:
        with open(candidate_path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        if len(content) > _SUMMARY_CHAR_THRESHOLD:
            return {
                "kind": "exact",
                "found": True,
                "identifier": identifier,
                "content": _summarize_rst_for_size(
                    identifier, candidate_path, len(content), parse_module,
                ),
                "examples": [],
            }
        return {
            "kind": "exact",
            "found": True,
            "identifier": identifier,
            "content": content,
            "examples": _collect_examples(content, api_path),
        }

    if (submodules := _list_direct_child_identifiers(api_path, identifier)):
        return {
            "kind": "namespace",
            "found": True,
            "identifier": identifier,
            "submodules": submodules,
        }

    parts = identifier.split(".")
    for strip_count in range(1, len(parts)):
        prefix = ".".join(parts[:-strip_count])
        tail = ".".join(parts[-strip_count:])
        prefix_path = _resolve_inside(api_path, prefix)
        if prefix_path is None:
            continue
        doctree = parse_module.doctree_for_path(prefix_path)
        rendered = parse_module.find_definition_in_doctree(doctree, tail)
        if rendered:
            return {
                "kind": "definition",
                "found": True,
                "identifier": identifier,
                "content": rendered,
                "examples": _collect_examples(rendered, api_path),
            }
        return {
            "kind": "partial",
            "found": False,
            "identifier": identifier,
            "parent": prefix,
            "available": parse_module.list_doctree_definitions(doctree),
            "submodules": _filter_submodules_by_tail(
                _list_direct_child_identifiers(api_path, prefix), tail,
            ),
        }

    if (suggestions := _list_identifiers_containing_component(api_path, identifier)):
        return {
            "kind": "suggestions",
            "found": False,
            "identifier": identifier,
            "suggestions": suggestions,
        }

    return {"kind": "missing", "found": False, "identifier": identifier}


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
    _tool("get_python_api_docs", "Return bundled Blender Python API docs for an identifier, or list modules for `*` / `X.*`. Supports exact files, intra-file definitions, namespaces, suggestions, and bundled `literalinclude` examples.", {"identifier": _STRING}, ["identifier"]),
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

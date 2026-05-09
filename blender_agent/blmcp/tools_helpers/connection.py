# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Socket client for communicating with the Blender add-on.

Used by MCP "tools" that send-code to the Blender add-on.
"""

__all__ = (
    "get_connection_params",
    "send_code",
)

import json
import os
import socket

_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = 9876
_TIMEOUT = 300.0
_RECV_BUFFER_SIZE = 65536


def get_connection_params() -> tuple[str, int]:
    host = os.environ.get("BLENDER_MCP_HOST", _DEFAULT_HOST)
    port = int(os.environ.get("BLENDER_MCP_PORT", str(_DEFAULT_PORT)))
    return host, port


def send_code(code: str, strict_json: bool) -> dict[str, object]:
    """
    Send Python code to the Blender add-on socket server for execution.

    Returns the full response dict from the add-on containing
    ``status`` (``"ok"`` or ``"error"``), ``result`` (on success),
    ``message`` (on error), and optionally ``stdout``/``stderr``
    captured during execution.

    Raises ``ConnectionError`` when Blender is unreachable or
    returns an invalid response.
    """
    host, port = get_connection_params()
    request = json.dumps({
        "type": "execute",
        "code": code,
        "strict_json": strict_json,
    }) + "\0"

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(_TIMEOUT)
            sock.connect((host, port))
            sock.sendall(request.encode("utf-8"))

            # Read response until the null byte delimiter.
            buf = bytearray()
            while True:
                chunk = sock.recv(_RECV_BUFFER_SIZE)
                if not chunk:
                    break
                buf.extend(chunk)
                if b"\0" in buf:
                    break
    except ConnectionRefusedError as ex:
        raise ConnectionError(
            "Cannot connect to Blender at {:s}:{:d}. "
            "Ensure Blender is running with the MCP addon enabled and the server started.".format(host, port)
        ) from ex
    except socket.timeout as ex:
        raise ConnectionError(
            "Blender connection timed out at {:s}:{:d}".format(host, port)
        ) from ex
    except OSError as ex:
        # NOTE: intentionally not catching `Exception` here.
        # Callers use `ConnectionError` to trigger a fallback path;
        # a broader catch would mask real bugs as connection failures.
        raise ConnectionError(
            "Socket error communicating with Blender at {:s}:{:d}: {:s}".format(host, port, str(ex))
        ) from ex

    if not buf:
        raise ConnectionError("Empty response from Blender")

    # Parse only up to the first null byte delimiter.
    line, _sep, _rest = buf.partition(b"\0")
    try:
        response: dict[str, object] = json.loads(line.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as ex:
        raise ConnectionError(
            "Invalid response from Blender at {:s}:{:d}: {:s}".format(host, port, str(ex))
        ) from ex
    return response

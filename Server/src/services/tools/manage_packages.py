from typing import Annotated, Any, Optional

from fastmcp import Context
from mcp.types import ToolAnnotations

from services.registry import mcp_for_unity_tool
from services.tools import get_unity_instance_from_context
from services.tools._wait import wait_for_async_job
from transport.unity_transport import send_with_unity_instance
from transport.legacy.unity_connection import async_send_command_with_retry

ALL_ACTIONS = [
    "list_packages", "search_packages", "get_package_info", "ping", "status",
    "add_package", "remove_package", "embed_package", "resolve_packages",
    "add_registry", "remove_registry", "list_registries",
]


async def _send_packages_command(
    ctx: Context,
    params_dict: dict[str, Any],
) -> dict[str, Any]:
    unity_instance = await get_unity_instance_from_context(ctx)
    result = await send_with_unity_instance(
        async_send_command_with_retry, unity_instance, "manage_packages", params_dict
    )
    return result if isinstance(result, dict) else {"success": False, "message": str(result)}


@mcp_for_unity_tool(
    group="core",
    description=(
        "Manage Unity packages: query, install, remove, embed, and configure registries.\n\n"
        "QUERY (read-only):\n"
        "- list_packages: List all installed packages\n"
        "- search_packages: Search Unity registry by keyword\n"
        "- get_package_info: Get details about a specific installed package\n"
        "- ping: Check package manager availability\n"
        "- status: Poll async job status (job_id required for list/search; optional for add/remove/embed)\n\n"
        "INSTALL/REMOVE:\n"
        "- add_package: Install a package (name, name@version, git URL, or file: path)\n"
        "- remove_package: Remove a package (checks dependents; use force=true to override)\n\n"
        "REGISTRIES:\n"
        "- list_registries: List all scoped registries\n"
        "- add_registry: Add a scoped registry (e.g., OpenUPM)\n"
        "- remove_registry: Remove a scoped registry\n\n"
        "UTILITY:\n"
        "- embed_package: Copy package to local Packages/ for editing\n"
        "- resolve_packages: Force re-resolution of all packages"
    ),
    annotations=ToolAnnotations(
        title="Manage Packages",
        destructiveHint=True,
        readOnlyHint=False,
    ),
)
async def manage_packages(
    ctx: Context,
    action: Annotated[str, "The package action to perform."],
    package: Annotated[Optional[str], "Package identifier (name, name@version, git URL, or file: path)."] = None,
    force: Annotated[Optional[bool], "Force removal even if other packages depend on it."] = None,
    query: Annotated[Optional[str], "Search query for search_packages."] = None,
    job_id: Annotated[Optional[str], "Job ID for polling status."] = None,
    name: Annotated[Optional[str], "Registry name for add_registry/remove_registry."] = None,
    url: Annotated[Optional[str], "Registry URL for add_registry."] = None,
    scopes: Annotated[Optional[list[str]], "Registry scopes for add_registry."] = None,
    wait_timeout: Annotated[
        Optional[int],
        "If set, wait up to this many seconds for the package job to reach a terminal "
        "state before returning. Avoids client-side poll loops. Recommended: 60 for "
        "add/remove, 120 for embed. Returns immediately when the job completes sooner; "
        "returns last status on timeout.",
    ] = None,
) -> dict[str, Any]:
    action_lower = action.lower()
    if action_lower not in ALL_ACTIONS:
        return {
            "success": False,
            "message": f"Unknown action '{action}'. Valid actions: {', '.join(ALL_ACTIONS)}",
        }

    if wait_timeout is not None and wait_timeout < 0:
        return {
            "success": False,
            "message": "wait_timeout must be a non-negative integer (seconds) or None",
        }

    params_dict: dict[str, Any] = {"action": action_lower}
    param_map = {
        "package": package,
        "force": force,
        "query": query,
        "job_id": job_id,
        "name": name,
        "url": url,
        "scopes": scopes,
    }
    for key, val in param_map.items():
        if val is not None:
            params_dict[key] = val

    response = await _send_packages_command(ctx, params_dict)

    if not wait_timeout or wait_timeout <= 0:
        return response
    if not isinstance(response, dict) or response.get("_mcp_status") != "pending":
        return response

    resolved_job_id = job_id or (
        (response.get("data") or {}).get("job_id")
        if isinstance(response.get("data"), dict)
        else None
    )
    if not resolved_job_id:
        return response

    async def _fetch_status() -> dict[str, Any]:
        return await _send_packages_command(
            ctx, {"action": "status", "job_id": resolved_job_id})

    return await wait_for_async_job(
        fetch_status=_fetch_status,
        timeout_seconds=wait_timeout,
        initial_interval=0.5,
        max_interval=3.0,
    )

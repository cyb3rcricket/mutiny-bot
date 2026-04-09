"""Morning briefing tool: local ops summary."""

import os
import platform
import shutil
from datetime import datetime

from tools.registry import ai_tool


def collect_local_system_snapshot() -> str:
    """Build a small local machine status snapshot using stdlib only."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hostname = platform.node() or "unknown-host"
    python_version = platform.python_version()
    platform_name = f"{platform.system()} {platform.release()}"

    load_text = "N/A"
    if hasattr(os, "getloadavg"):
        try:
            load1, load5, load15 = os.getloadavg()
            load_text = f"{load1:.2f}, {load5:.2f}, {load15:.2f}"
        except OSError:
            load_text = "N/A"

    try:
        disk = shutil.disk_usage("/")
        free_gb = disk.free / (1024 ** 3)
        total_gb = disk.total / (1024 ** 3)
        disk_text = f"{free_gb:.1f} GB free / {total_gb:.1f} GB total"
    except Exception:
        disk_text = "N/A"

    return (
        f"- Time: {now}\n"
        f"- Host: {hostname}\n"
        f"- Platform: {platform_name}\n"
        f"- Python: {python_version}\n"
        f"- CPU load (1m, 5m, 15m): {load_text}\n"
        f"- Disk (/): {disk_text}"
    )


@ai_tool(
    name="get_morning_briefing",
    description="Generate a daily morning operations briefing",
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
)
async def get_morning_briefing() -> str:
    """Build and return a deterministic morning ops briefing."""
    system_snapshot = collect_local_system_snapshot()
    checklist = (
        "- Confirm bot process is online\n"
        "- Review active automation schedule\n"
        "- Verify dashboard telemetry refresh\n"
        "- Check local model availability in Ollama"
    )

    return (
        "**MutinyBot Local Morning Briefing**\n\n"
        "**System Snapshot**\n"
        f"{system_snapshot}\n\n"
        "**Ops Checklist**\n"
        f"{checklist}"
    )

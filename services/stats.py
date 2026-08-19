from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from pathlib import Path
from psutil import (
    boot_time,
    cpu_count,
    cpu_percent,
    disk_usage,
    net_io_counters,
    swap_memory,
    virtual_memory,
)

from services.progress import humanbytes

BOT_START_TIME = time.time()

TOOL_COMMANDS: dict[str, tuple[list[str], str]] = {
    "python": ([sys.executable, "--version"], r"Python ([\d.]+)"),
    "yt-dlp": (["yt-dlp", "--version"], r"([\d.]+)"),
    "gallery-dl": (["gallery-dl", "--version"], r"([\d.]+)"),
    "ffmpeg": (["ffmpeg", "-version"], r"ffmpeg version ([\d.]+(-\w+)?).*"),
    "aria2": (["aria2c", "--version"], r"aria2 version ([\d.]+)"),
    "7z": (["7z", "i"], r"7-Zip ([\d.]+)"),
}


def get_readable_time(seconds: float | int) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return "".join(parts)


async def _run_command(cmd: list[str]) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return (stdout or stderr).decode(errors="ignore").strip()
    except Exception:
        return ""


async def _get_version(command: list[str], regex_pattern: str) -> str:
    output = await _run_command(command)
    if not output:
        return "N/A"
    match = re.search(regex_pattern, output)
    return match.group(1) if match else "N/A"


async def _get_git_commit() -> str:
    if not Path(".git").exists():
        return "No Git Repository"
    output = await _run_command(["git", "log", "-1", "--date=short", "--pretty=format:%cd From %cr"])
    return output or "Unknown"


async def get_system_stats() -> str:
    # 1. System & Resource Metrics
    total_disk, used_disk, free_disk, disk_pct = disk_usage("/")
    swap = swap_memory()
    memory = virtual_memory()
    net = net_io_counters()

    # Hitung CPU tanpa block async loop
    per_cpu = await asyncio.to_thread(cpu_percent, 0.5, True)
    total_cpu = await asyncio.to_thread(cpu_percent, 0.1, False)
    per_cpu_str = " | ".join([f"CPU{i+1}: {round(p)}%" for i, p in enumerate(per_cpu)])

    # 2. Package Versions & Git Commit
    version_tasks = [
        _get_version(cmd, pattern) for cmd, pattern in TOOL_COMMANDS.values()
    ]
    tool_versions = await asyncio.gather(*version_tasks)
    commit_date = await _get_git_commit()

    tool_lines = [
        f"<b>{name}:</b> {ver}"
        for name, ver in zip(TOOL_COMMANDS.keys(), tool_versions)
    ]
    tools_str = "\n".join(tool_lines)

    # 3. Format Output
    return f"""
<b>Commit Date:</b> {commit_date}

<b>Bot Uptime:</b> {get_readable_time(time.time() - BOT_START_TIME)}
<b>OS Uptime:</b> {get_readable_time(time.time() - boot_time())}

<b>Total Disk Space:</b> {humanbytes(total_disk)}
<b>Used:</b> {humanbytes(used_disk)} | <b>Free:</b> {humanbytes(free_disk)}

<b>Upload:</b> {humanbytes(net.bytes_sent)}
<b>Download:</b> {humanbytes(net.bytes_recv)}

<b>CPU:</b> {total_cpu}%
<b>CPU Cores:</b>
{per_cpu_str}

<b>RAM:</b> {memory.percent}%
<b>DISK:</b> {disk_pct}%

<b>Physical Cores:</b> {cpu_count(logical=False)}
<b>Total Cores:</b> {cpu_count()}
<b>SWAP:</b> {humanbytes(swap.total)} | <b>Used:</b> {swap.percent}%

<b>Memory Total:</b> {humanbytes(memory.total)}
<b>Memory Free:</b> {humanbytes(memory.available)}
<b>Memory Used:</b> {humanbytes(memory.used)}

{tools_str}
""".strip()
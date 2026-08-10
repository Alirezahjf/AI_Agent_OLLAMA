"""System monitoring actions using psutil.

Provides real-time CPU, RAM, disk, and network statistics plus
per-process resource usage.  All actions are SAFE (read-only).
"""

from __future__ import annotations

import platform
from typing import Any

from ..core.errors import DependencyMissing
from .registry import ActionContext, ActionRegistry, Risk, risk


def register_system_monitor(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="system_monitor",
        description=(
            "آمار لحظه‌ای سیستم: CPU%, RAM, Disk, Network. "
            "اختیاری: top_processes برای N پروسس پرمصرف. SAFE."
        ),
        parameters={
            "top_processes": {"type": "integer", "description": "تعداد پروسس‌های پرمصرف (0=بدون پروسس)"},
        },
    )(system_monitor)

    registry.decorator(
        name="process_list",
        description=(
            "لیست پروسس‌های فعال با فیلتر نام و مرتب‌سازی (cpu/memory/name). SAFE."
        ),
        parameters={
            "filter": {"type": "string", "description": "فیلتر نام پروسس"},
            "sort": {"type": "string", "enum": ["cpu", "memory", "name", "pid"]},
            "limit": {"type": "integer", "description": "حداکثر تعداد (پیش‌فرض 20)"},
        },
    )(process_list)

    registry.decorator(
        name="disk_usage",
        description="اطلاعات فضای دیسک برای همهٔ partition ها یا یک مسیر خاص. SAFE.",
        parameters={
            "path": {"type": "string", "description": "مسیر (اختیاری، پیش‌فرض: همهٔ partition ها)"},
        },
    )(disk_usage)

    registry.decorator(
        name="network_stats",
        description="آمار شبکه: bytes sent/received, connections count. SAFE.",
        parameters={},
    )(network_stats)


def _get_psutil():
    try:
        import psutil
        return psutil
    except ImportError:
        raise DependencyMissing(
            "psutil is not installed",
            install_hint="برای نصب: pip install psutil",
        )


def _format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


@risk(Risk.SAFE)
def system_monitor(*, top_processes: int = 0, context: ActionContext) -> str:
    psutil = _get_psutil()
    lines = [f"📊 آمار سیستم ({platform.system()} {platform.release()})"]

    # CPU
    cpu_percent = psutil.cpu_percent(interval=0.5)
    cpu_count = psutil.cpu_count()
    cpu_freq = psutil.cpu_freq()
    freq_str = f" @ {cpu_freq.current:.0f}MHz" if cpu_freq else ""
    lines.append(f"  CPU: {cpu_percent:.1f}% ({cpu_count} cores{freq_str})")

    # Per-CPU
    per_cpu = psutil.cpu_percent(interval=0, percpu=True)
    if per_cpu and len(per_cpu) <= 16:
        lines.append(f"  CPU cores: {' | '.join(f'{p:.0f}%' for p in per_cpu)}")

    # Memory
    mem = psutil.virtual_memory()
    lines.append(f"  RAM: {mem.percent:.1f}% ({_format_bytes(mem.used)}/{_format_bytes(mem.total)})")

    # Swap
    swap = psutil.swap_memory()
    if swap.total > 0:
        lines.append(f"  Swap: {swap.percent:.1f}% ({_format_bytes(swap.used)}/{_format_bytes(swap.total)})")

    # Disk (root/system)
    try:
        disk = psutil.disk_usage("/")
        lines.append(f"  Disk: {disk.percent:.1f}% ({_format_bytes(disk.used)}/{_format_bytes(disk.total)})")
    except OSError:
        pass

    # Load average (Linux/macOS only)
    try:
        load1, load5, load15 = psutil.getloadavg()
        lines.append(f"  Load avg: {load1:.2f} / {load5:.2f} / {load15:.2f}")
    except (OSError, AttributeError):
        pass

    # Boot time
    boot = psutil.boot_time()
    from datetime import datetime
    boot_dt = datetime.fromtimestamp(boot)
    uptime = datetime.now() - boot_dt
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes = remainder // 60
    lines.append(f"  Uptime: {hours}h {minutes}m (boot: {boot_dt:%Y-%m-%d %H:%M})")

    # Top processes
    top_n = max(0, int(top_processes or 0))
    if top_n > 0:
        procs = []
        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                info = proc.info
                procs.append(info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        procs.sort(key=lambda p: p.get("cpu_percent", 0) or 0, reverse=True)
        lines.append(f"\n  🔝 {top_n} پروسس پرمصرف:")
        for p in procs[:top_n]:
            name = p.get("name", "?")[:25]
            cpu = p.get("cpu_percent", 0) or 0
            mem = p.get("memory_percent", 0) or 0
            lines.append(f"    PID {p.get('pid', '?')} | {name:25s} | CPU: {cpu:5.1f}% | MEM: {mem:5.1f}%")

    return "\n".join(lines)


@risk(Risk.SAFE)
def process_list(*, filter: str = "", sort: str = "cpu",
                 limit: int = 20, context: ActionContext) -> str:
    psutil = _get_psutil()
    procs = []
    query = str(filter or "").lower()
    for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "status"]):
        try:
            info = proc.info
            if query and query not in str(info.get("name", "")).lower():
                continue
            procs.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    sort_key = {
        "cpu": lambda p: p.get("cpu_percent", 0) or 0,
        "memory": lambda p: p.get("memory_percent", 0) or 0,
        "name": lambda p: str(p.get("name", "")).lower(),
        "pid": lambda p: p.get("pid", 0),
    }.get(sort, lambda p: p.get("cpu_percent", 0) or 0)

    reverse = sort in ("cpu", "memory")
    procs.sort(key=sort_key, reverse=reverse)

    max_items = max(1, min(int(limit or 20), 100))
    lines = [f"تعداد {len(procs)} پروسس (مرتب: {sort}):"]
    for p in procs[:max_items]:
        name = p.get("name", "?")[:30]
        pid = p.get("pid", "?")
        cpu = p.get("cpu_percent", 0) or 0
        mem = p.get("memory_percent", 0) or 0
        status = p.get("status", "?")
        lines.append(f"  PID {pid:>7} | {name:30s} | CPU: {cpu:5.1f}% | MEM: {mem:5.1f}% | {status}")
    if len(procs) > max_items:
        lines.append(f"  … و {len(procs) - max_items} پروسس دیگر")
    return "\n".join(lines)


@risk(Risk.SAFE)
def disk_usage(*, path: str = "", context: ActionContext) -> str:
    psutil = _get_psutil()
    if path:
        try:
            usage = psutil.disk_usage(str(path))
            return (
                f"💾 فضای {path}:\n"
                f"  کل: {_format_bytes(usage.total)}\n"
                f"  استفاده‌شده: {_format_bytes(usage.used)} ({usage.percent:.1f}%)\n"
                f"  آزاد: {_format_bytes(usage.free)}"
            )
        except OSError as exc:
            return f"❌ خطا: {exc}"

    lines = ["💾 فضای دیسک:"]
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            lines.append(
                f"  {part.mountpoint:20s} [{part.fstype}] "
                f"{usage.percent:.1f}% ({_format_bytes(usage.used)}/{_format_bytes(usage.total)})"
            )
        except (OSError, PermissionError):
            lines.append(f"  {part.mountpoint:20s} [{part.fstype}] (غیرقابل‌دسترسی)")
    return "\n".join(lines)


@risk(Risk.SAFE)
def network_stats(*, context: ActionContext) -> str:
    psutil = _get_psutil()
    net_io = psutil.net_io_counters()
    lines = [
        "🌐 آمار شبکه:",
        f"  ارسالی: {_format_bytes(net_io.bytes_sent)} ({net_io.packets_sent} packets)",
        f"  دریافتی: {_format_bytes(net_io.bytes_recv)} ({net_io.packets_recv} packets)",
        f"  خطا (in/out): {net_io.errin}/{net_io.errout}",
        f"  drop (in/out): {net_io.dropin}/{net_io.dropout}",
    ]
    # Connections count
    try:
        conns = psutil.net_connections(kind="inet")
        established = sum(1 for c in conns if c.status == "ESTABLISHED")
        listening = sum(1 for c in conns if c.status == "LISTEN")
        lines.append(f"  اتصالات: {established} established, {listening} listening")
    except (psutil.AccessDenied, OSError):
        pass
    return "\n".join(lines)

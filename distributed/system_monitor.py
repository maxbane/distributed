from __future__ import annotations

import sys
from collections import deque
from typing import Any

import psutil

import dask

from distributed.compatibility import WINDOWS
from distributed.diagnostics import nvml
from distributed.metrics import monotonic, time


class SystemMonitor:
    proc: psutil.Process
    maxlen: int | None
    count: int
    last_time: float
    quantities: dict[str, deque[float]]

    monitor_net_io: bool
    monitor_disk_io: bool
    monitor_host_cpu: bool
    _last_net_io_counters: Any  # psutil namedtuple
    _last_disk_io_counters: Any  # psutil namedtuple
    _last_host_cpu_counters: Any  # dynamically-defined psutil namedtuple

    gpu_name: str | None
    gpu_memory_total: int

    # Defaults to 1h capture time assuming the default
    # distributed.admin.system_monitor.interval = 500ms
    def __init__(
        self,
        maxlen: int | None = 7200,
        monitor_disk_io: bool | None = None,
        monitor_host_cpu: bool | None = None,
    ):
        self.proc = psutil.Process()
        self.count = 0
        self.maxlen = maxlen
        self.last_time = monotonic()

        self.quantities = {
            "cpu": deque(maxlen=maxlen),
            "memory": deque(maxlen=maxlen),
            "time": deque(maxlen=maxlen),
        }

        try:
            self._last_net_io_counters = psutil.net_io_counters()
        except Exception:
            # FIXME is this possible?
            self.monitor_net_io = False  # pragma: nocover
        else:
            self.monitor_net_io = True
            self.quantities["host_net_io.read_bps"] = deque(maxlen=maxlen)
            self.quantities["host_net_io.write_bps"] = deque(maxlen=maxlen)

        if monitor_disk_io is None:
            monitor_disk_io = dask.config.get("distributed.admin.system-monitor.disk")
        if monitor_disk_io:
            try:
                disk_ioc = psutil.disk_io_counters()
            except Exception:
                # FIXME is this possible?
                monitor_disk_io = False  # pragma: nocover
            else:
                if disk_ioc is None:  # pragma: nocover
                    # diskless machine
                    # FIXME https://github.com/python/typeshed/pull/8829
                    monitor_disk_io = False  # type: ignore[unreachable]
                else:
                    self._last_disk_io_counters = disk_ioc
                    self.quantities["host_disk_io.read_bps"] = deque(maxlen=maxlen)
                    self.quantities["host_disk_io.write_bps"] = deque(maxlen=maxlen)
        self.monitor_disk_io = monitor_disk_io

        if monitor_host_cpu is None:
            monitor_host_cpu = dask.config.get(
                "distributed.admin.system-monitor.host-cpu"
            )
        self.monitor_host_cpu = monitor_host_cpu
        if monitor_host_cpu:
            self._last_host_cpu_counters = hostcpu_c = psutil.cpu_times()
            # This is a namedtuple whose fields change based on OS and kernel version
            for k in hostcpu_c._fields:
                self.quantities["host_cpu." + k] = deque(maxlen=maxlen)

        if not WINDOWS:
            self.quantities["num_fds"] = deque(maxlen=maxlen)

        if nvml.device_get_count() > 0:
            gpu_extra = nvml.one_time()
            self.gpu_name = gpu_extra["name"]
            self.gpu_memory_total = gpu_extra["memory-total"]
            self.quantities["gpu_utilization"] = deque(maxlen=maxlen)
            self.quantities["gpu_memory_used"] = deque(maxlen=maxlen)
        else:
            self.gpu_name = None
            self.gpu_memory_total = -1

        self.update()

    def recent(self) -> dict[str, Any]:
        return {k: v[-1] for k, v in self.quantities.items()}

    def get_process_memory(self) -> int:
        """Sample process memory, as reported by the OS.
        This one-liner function exists so that it can be easily mocked in unit tests,
        as the OS allocating and releasing memory is highly volatile and a constant
        source of flakiness.
        """
        return self.proc.memory_info().rss

    def update(self) -> dict[str, Any]:
        now = time()
        now_mono = monotonic()
        duration = (now_mono - self.last_time) or 0.001
        self.last_time = now_mono

        self.count += 1

        with self.proc.oneshot():
            result = {
                "cpu": self.proc.cpu_percent(),
                "memory": self.get_process_memory(),
                "time": now,
            }

        if self.monitor_net_io:
            net_ioc = psutil.net_io_counters()
            last = self._last_net_io_counters
            result["host_net_io.read_bps"] = (
                net_ioc.bytes_recv - last.bytes_recv
            ) / duration
            result["host_net_io.write_bps"] = (
                net_ioc.bytes_sent - last.bytes_sent
            ) / duration
            self._last_net_io_counters = net_ioc

        if self.monitor_disk_io:
            disk_ioc = psutil.disk_io_counters()
            last_disk = self._last_disk_io_counters
            result["host_disk_io.read_bps"] = (
                disk_ioc.read_bytes - last_disk.read_bytes
            ) / duration
            result["host_disk_io.write_bps"] = (
                disk_ioc.write_bytes - last_disk.write_bytes
            ) / duration
            self._last_disk_io_counters = disk_ioc

        if self.monitor_host_cpu:
            host_cpu = psutil.cpu_times()
            last_cpu = self._last_host_cpu_counters
            for k in host_cpu._fields:
                delta = getattr(host_cpu, k) - getattr(last_cpu, k)
                # cpu_times() has a precision of 2 decimals; suppress noise
                result["host_cpu." + k] = round(delta / duration, 2)
            self._last_host_cpu_counters = host_cpu

        # Note: WINDOWS constant doesn't work with `mypy --platform win32`
        if sys.platform != "win32":
            result["num_fds"] = self.proc.num_fds()

        if self.gpu_name:
            gpu_metrics = nvml.real_time()
            result["gpu_utilization"] = gpu_metrics["utilization"]
            result["gpu_memory_used"] = gpu_metrics["memory-used"]

        for name, v in result.items():
            if name != "count":
                self.quantities[name].append(v)

        return result

    def __repr__(self) -> str:
        return "<SystemMonitor: cpu: %d memory: %d MB fds: %s>" % (
            self.quantities["cpu"][-1],
            self.quantities["memory"][-1] / 1e6,
            "N/A" if WINDOWS else self.quantities["num_fds"][-1],
        )

    def range_query(self, start: int) -> dict[str, list]:
        if start >= self.count:
            return {k: [] for k in self.quantities}

        istart = min(-1, max(-len(self.quantities["cpu"]), start - self.count))

        return {
            k: [v[i] if -i <= len(v) else None for i in range(istart, 0)]
            for k, v in self.quantities.items()
        }

#!/usr/bin/env python3
"""SysMon - System Monitoring Web Server"""

import json
import time
import socket
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import psutil
import os
import platform
import datetime
import subprocess

ROOT = Path(__file__).parent

# ─── Power config (mutable, guarded by a lock) ────────────────────────────────
_power_lock = threading.Lock()
_power_config = {"rate_eur_per_kwh": 0.35}   # German avg; user-editable
_power_cache  = {"watts": None, "source": "unavailable", "ts": 0}
_win_batt_lock = threading.Lock()
_win_batt_state = {
    "full_charge_mwh": None,
    "last_percent": None,
    "last_ts": None,
}

# ─── Network throughput state ──────────────────────────────────────────────────
_net_prev_lock = threading.Lock()
_net_prev_snap: dict = {}   # {nic: (bytes_sent, bytes_recv)}
_net_prev_ts: list   = []   # [timestamp]

# ─── RAPL helpers ─────────────────────────────────────────────────────────────
RAPL_BASE = Path("/sys/class/powercap/intel-rapl")

def _rapl_domains():
    """Return list of (name, energy_uj_path, max_range_uj_path) tuples."""
    domains = []
    if not RAPL_BASE.exists():
        return domains
    for d in sorted(RAPL_BASE.iterdir()):
        uj  = d / "energy_uj"
        max_uj = d / "max_energy_range_uj"
        name_f = d / "name"
        if uj.exists() and name_f.exists():
            try:
                name = name_f.read_text().strip()
                domains.append((name, uj, max_uj))
            except Exception:
                pass
    return domains

def _read_uj(path):
    try:
        return int(path.read_text())
    except Exception:
        return None

def _sample_watts(interval=1.0):
    """Return total package watts averaged over `interval` seconds, or None."""
    domains = _rapl_domains()
    if not domains:
        return None, "no_rapl"

    readable = [(name, uj, max_uj) for name, uj, max_uj in domains
                if _read_uj(uj) is not None]
    if not readable:
        return None, "permission_denied"

    before = {name: _read_uj(uj) for name, uj, _ in readable}
    time.sleep(interval)
    after  = {name: _read_uj(uj) for name, uj, _ in readable}

    total_w = 0.0
    for name, uj, max_uj_path in readable:
        e1, e2 = before[name], after[name]
        if e1 is None or e2 is None:
            continue
        delta = e2 - e1
        if delta < 0:                       # counter wrap
            max_range = _read_uj(max_uj_path) or 262_143_328_850
            delta += max_range
        total_w += (delta / 1e6) / interval  # µJ → J → W

    return round(total_w, 1), "rapl"


def _sample_watts_windows():
    """Return Windows power in watts.

    Priority:
    1) BatteryStatus.DischargeRate (mW) from WMI.
    2) Battery runtime + capacity estimate when discharging.
    3) Battery percent delta over time estimate when discharging.

    Returns (None, ...) when no reliable telemetry exists.
    """
    # 1) Try battery discharge rate via WMI (available on many laptops).
    try:
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            "(Get-CimInstance -Namespace root/wmi -ClassName BatteryStatus "
            "-ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty DischargeRate)"
        ]
        out = subprocess.check_output(cmd, text=True, timeout=1.5).strip()
        if out:
            mw = int(float(out))
            if mw > 0:
                return round(mw / 1000.0, 1), "battery_discharge_rate"
    except Exception:
        pass

    b = None
    try:
        b = psutil.sensors_battery()
    except Exception:
        b = None

    if not b or b.power_plugged:
        return None, "no_windows_power_sensor"

    # 2) Runtime-based estimate if full-charge capacity and secsleft are available.
    full_mwh = _get_windows_full_charge_capacity_mwh()
    if full_mwh and b.secsleft not in (None, psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) and b.secsleft > 0:
        remaining_wh = (full_mwh / 1000.0) * (max(0.0, min(float(b.percent), 100.0)) / 100.0)
        watts = remaining_wh / (b.secsleft / 3600.0)
        if watts > 0:
            return round(watts, 1), "battery_runtime_estimate"

    # 3) Delta-based fallback from battery percent change over time.
    with _win_batt_lock:
        now = time.time()
        prev_percent = _win_batt_state["last_percent"]
        prev_ts = _win_batt_state["last_ts"]
        _win_batt_state["last_percent"] = float(b.percent)
        _win_batt_state["last_ts"] = now

    if full_mwh and prev_percent is not None and prev_ts is not None:
        dt_h = max((time.time() - prev_ts) / 3600.0, 0.0)
        drop_pct = max(0.0, float(prev_percent) - float(b.percent))
        if dt_h > 0 and drop_pct > 0:
            wh_used = (full_mwh / 1000.0) * (drop_pct / 100.0)
            watts = wh_used / dt_h
            if watts > 0:
                return round(watts, 1), "battery_delta_estimate"

    return None, "no_windows_power_sensor"


def _get_windows_full_charge_capacity_mwh():
    """Return total battery full-charge capacity (mWh) from WMI, cached."""
    with _win_batt_lock:
        cached = _win_batt_state.get("full_charge_mwh")
    if cached is not None:
        return cached

    cap_mwh = None
    try:
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            "(Get-CimInstance -Namespace root/wmi -ClassName BatteryFullChargedCapacity "
            "-ErrorAction SilentlyContinue | Measure-Object -Property FullChargedCapacity -Sum).Sum"
        ]
        out = subprocess.check_output(cmd, text=True, timeout=1.5).strip()
        if out:
            val = int(float(out))
            if val > 0:
                cap_mwh = val
    except Exception:
        cap_mwh = None

    with _win_batt_lock:
        _win_batt_state["full_charge_mwh"] = cap_mwh
    return cap_mwh


def _get_windows_temperatures():
    """Return psutil-compatible temperature payload using Windows WMI when available."""
    temps = {}
    try:
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
            "-ErrorAction SilentlyContinue | Select-Object InstanceName,CurrentTemperature "
            "| ConvertTo-Json -Compress"
        ]
        out = subprocess.check_output(cmd, text=True, timeout=1.5).strip()
        if not out:
            return temps

        payload = json.loads(out)
        rows = payload if isinstance(payload, list) else [payload]
        entries = []
        for row in rows:
            cur = row.get("CurrentTemperature")
            if cur is None:
                continue
            # WMI temp is in tenths of Kelvin.
            celsius = (float(cur) / 10.0) - 273.15
            if celsius < -30 or celsius > 130:
                continue
            entries.append({
                "label": row.get("InstanceName") or "acpi",
                "current": round(celsius, 1),
                "high": None,
                "critical": None,
            })

        if entries:
            temps["acpi"] = entries
    except Exception:
        pass
    return temps


def _estimate_windows_cpu_temp(cpu_percent):
    """Return a conservative estimated CPU temp when hardware sensors are unavailable."""
    pct = max(0.0, min(float(cpu_percent or 0.0), 100.0))
    # Typical idle/load envelope for consumer desktops and laptops.
    est = 36.0 + (pct * 0.40)   # 36C idle -> ~76C at 100%
    return round(est, 1)


def _power_sampler_loop():
    """Background thread: update _power_cache every ~3 s."""
    while True:
        try:
            if platform.system() == "Windows":
                watts, source = _sample_watts_windows()
            else:
                watts, source = _sample_watts(interval=2.0)
            with _power_lock:
                _power_cache["watts"]  = watts
                _power_cache["source"] = source
                _power_cache["ts"]     = time.time()
        except Exception:
            pass
        time.sleep(1.0)   # next sample starts 1 s after the 2 s measure window


def get_power_stats():
    with _power_lock:
        watts  = _power_cache["watts"]
        source = _power_cache["source"]
        rate   = _power_config["rate_eur_per_kwh"]

    cost = {}
    if watts is not None:
        wh_per_hour  = watts
        cost["per_hour"]  = round((wh_per_hour  / 1000) * rate, 4)
        cost["per_day"]   = round((wh_per_hour  * 24 / 1000) * rate, 3)
        cost["per_month"] = round((wh_per_hour  * 24 * 30 / 1000) * rate, 2)
        cost["per_year"]  = round((wh_per_hour  * 24 * 365 / 1000) * rate, 2)

    return {
        "watts":  watts,
        "source": source,
        "rate_eur_per_kwh": rate,
        "cost":   cost,
    }


def _safe_getloadavg():
    """Return [1m, 5m, 15m] load averages or a neutral fallback on Windows."""
    if hasattr(os, "getloadavg"):
        try:
            return list(os.getloadavg())
        except Exception:
            pass
    return [0.0, 0.0, 0.0]


# ─── System stats ──────────────────────────────────────────────────────────────
def get_system_stats():
    is_windows = platform.system() == "Windows"
    cpu_interval = 0.15 if is_windows else None
    cpu_per_core = psutil.cpu_percent(percpu=True, interval=cpu_interval)
    cpu_freq = psutil.cpu_freq()
    mem  = psutil.virtual_memory()
    swap = psutil.swap_memory()

    disk_parts = psutil.disk_partitions()
    disks = []
    for p in disk_parts:
        try:
            usage = psutil.disk_usage(p.mountpoint)
            disks.append({
                "device": p.device, "mountpoint": p.mountpoint,
                "fstype": p.fstype, "total": usage.total,
                "used": usage.used, "free": usage.free, "percent": usage.percent,
            })
        except PermissionError:
            pass

    net = psutil.net_io_counters(pernic=True)
    net_stats = {
        nic: {"bytes_sent": v.bytes_sent, "bytes_recv": v.bytes_recv,
              "packets_sent": v.packets_sent, "packets_recv": v.packets_recv,
              "errin": v.errin, "errout": v.errout}
        for nic, v in net.items()
    }

    # Per-NIC link info (speed in Mbps, isup) for bottleneck calc
    net_if_stats = {}
    try:
        for nic, s in psutil.net_if_stats().items():
            net_if_stats[nic] = {
                "isup": bool(s.isup),
                "speed_mbps": int(s.speed) if s.speed else 0,
                "mtu": int(s.mtu) if s.mtu else 0,
            }
    except Exception:
        pass

    # Compute per-NIC throughput (bytes/sec) using delta from last call
    now_t = time.time()
    with _net_prev_lock:
        prev_snap = _net_prev_snap.copy()
        prev_ts   = _net_prev_ts[0] if _net_prev_ts else now_t
        # Update stored snapshot
        _net_prev_snap.clear()
        for nic, v in net.items():
            _net_prev_snap[nic] = (v.bytes_sent, v.bytes_recv)
        _net_prev_ts.clear()
        _net_prev_ts.append(now_t)

    dt = max(now_t - prev_ts, 0.1)
    net_throughput = {}
    for nic, v in net.items():
        if nic in prev_snap:
            tx = max(0.0, (v.bytes_sent - prev_snap[nic][0]) / dt)
            rx = max(0.0, (v.bytes_recv - prev_snap[nic][1]) / dt)
        else:
            tx, rx = 0.0, 0.0
        net_throughput[nic] = {"tx_bps": round(tx), "rx_bps": round(rx)}

    temps = {}
    try:
        if not is_windows:
            raw_temps = psutil.sensors_temperatures()
            for name, entries in raw_temps.items():
                temps[name] = [{"label": e.label, "current": e.current,
                                "high": e.high, "critical": e.critical} for e in entries]
    except Exception:
        pass
    if is_windows and not temps:
        temps = _get_windows_temperatures()

    cpu_percent = (round(sum(cpu_per_core) / len(cpu_per_core), 1)
                   if (is_windows and cpu_per_core)
                   else psutil.cpu_percent(interval=None))

    if is_windows and not temps:
        temps = {
            "estimated": [{
                "label": "CPU (estimated)",
                "current": _estimate_windows_cpu_temp(cpu_percent),
                "high": None,
                "critical": None,
            }]
        }

    battery = None
    try:
        b = psutil.sensors_battery()
        if b:
            battery = {"percent": b.percent, "power_plugged": b.power_plugged,
                       "secsleft": b.secsleft}
    except Exception:
        pass

    procs = []
    if platform.system() == "Windows":
        # Keep the endpoint responsive on Windows: status/threads/memory queries can be slow.
        proc_info = [p.info for p in psutil.process_iter(['pid', 'name', 'username'])]
        for info in sorted(proc_info, key=lambda x: x.get('pid') or 0, reverse=True)[:30]:
            procs.append({
                "pid": info.get('pid'),
                "name": info.get('name'),
                "user": info.get('username'),
                "status": "unknown",
                "cpu": 0.0,
                "mem": 0.0,
                "threads": 0,
                "started": "—",
            })
    else:
        for p in sorted(psutil.process_iter(['pid', 'name', 'username', 'status',
                                              'cpu_percent', 'memory_percent',
                                              'num_threads', 'create_time']),
                        key=lambda x: x.info.get('cpu_percent') or 0, reverse=True)[:30]:
            try:
                procs.append({
                    "pid": p.info['pid'], "name": p.info['name'],
                    "user": p.info['username'], "status": p.info['status'],
                    "cpu": round(p.info['cpu_percent'] or 0, 1),
                    "mem": round(p.info['memory_percent'] or 0, 2),
                    "threads": p.info['num_threads'],
                    "started": datetime.datetime.fromtimestamp(
                        p.info['create_time']).strftime("%H:%M:%S"),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    services = []
    if platform.system() == "Linux":
        try:
            import subprocess
            out = subprocess.check_output(
                ["systemctl", "list-units", "--type=service", "--no-pager",
                 "--no-legend", "--all"],
                text=True, timeout=3
            )
            for line in out.strip().splitlines():
                parts = line.split()
                if len(parts) >= 4:
                    services.append({
                        "unit": parts[0], "load": parts[1],
                        "active": parts[2], "sub": parts[3],
                        "desc": " ".join(parts[4:]) if len(parts) > 4 else "",
                    })
        except Exception:
            pass

    boot_time  = psutil.boot_time()
    uptime_sec = time.time() - boot_time
    uptime_str = str(datetime.timedelta(seconds=int(uptime_sec)))

    return {
        "timestamp": datetime.datetime.now().isoformat(),
        "hostname":  socket.gethostname(),
        "platform":  platform.platform(),
        "uptime":    uptime_str,
        "cpu": {
            "percent":        cpu_percent,
            "per_core":       cpu_per_core,
            "cores_logical":  psutil.cpu_count(logical=True),
            "cores_physical": psutil.cpu_count(logical=False),
            "freq_mhz":       {"current": cpu_freq.current if cpu_freq else None,
                               "min":     cpu_freq.min     if cpu_freq else None,
                               "max":     cpu_freq.max     if cpu_freq else None},
            "load_avg":       _safe_getloadavg(),
        },
        "memory": {
            "total": mem.total, "available": mem.available,
            "used":  mem.used,  "percent":   mem.percent,
            "cached":  getattr(mem, 'cached', 0),
            "buffers": getattr(mem, 'buffers', 0),
        },
        "swap": {
            "total": swap.total, "used": swap.used,
            "free":  swap.free,  "percent": swap.percent,
        },
        "disks":        disks,
        "network":      net_stats,
        "net_throughput": net_throughput,
        "net_if_stats": net_if_stats,
        "temperatures": temps,
        "battery":      battery,
        "processes":    procs,
        "services":     services,
        "power":        get_power_stats(),
    }


# ─── HTTP handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress access log spam

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            self._serve_file('index.html', 'text/html')
        elif self.path == '/api/stats':
            self._json_response(get_system_stats())
        elif self.path == '/api/power-config':
            with _power_lock:
                self._json_response(dict(_power_config))
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == '/api/power-config':
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length)
            try:
                payload = json.loads(body)
                rate = float(payload.get("rate_eur_per_kwh", 0))
                if rate <= 0:
                    raise ValueError("rate must be positive")
                with _power_lock:
                    _power_config["rate_eur_per_kwh"] = round(rate, 4)
                self._json_response({"ok": True, "rate_eur_per_kwh": round(rate, 4)})
            except Exception as e:
                self._json_response({"ok": False, "error": str(e)}, 400)
        else:
            self.send_error(404)

    def _serve_file(self, name, mime):
        path = ROOT / name
        if not path.exists():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)


# ─── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    # Prime CPU percent measurement
    psutil.cpu_percent(percpu=True, interval=0.1)

    # Start background power sampler
    t = threading.Thread(target=_power_sampler_loop, daemon=True)
    t.start()
    print("Power sampler thread started.")

    host = '0.0.0.0'
    port = 3200
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"SysMon running at http://{socket.gethostname()}:{port}")
    print(f"Also reachable at http://0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")

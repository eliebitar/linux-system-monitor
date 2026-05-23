#!/usr/bin/env python3
"""SysMon - System Monitoring Web Server"""

import json
import time
import socket
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import psutil
import os
import platform
import datetime

ROOT = Path(__file__).parent

# ─── Power config (mutable, guarded by a lock) ────────────────────────────────
_power_lock = threading.Lock()
_power_config = {"rate_eur_per_kwh": 0.35}   # German avg; user-editable
_power_cache  = {"watts": None, "source": "unavailable", "ts": 0}

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


def _power_sampler_loop():
    """Background thread: update _power_cache every ~3 s."""
    while True:
        try:
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


# ─── System stats ──────────────────────────────────────────────────────────────
def get_system_stats():
    cpu_per_core = psutil.cpu_percent(percpu=True, interval=None)
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

    temps = {}
    try:
        raw_temps = psutil.sensors_temperatures()
        for name, entries in raw_temps.items():
            temps[name] = [{"label": e.label, "current": e.current,
                            "high": e.high, "critical": e.critical} for e in entries]
    except Exception:
        pass

    battery = None
    try:
        b = psutil.sensors_battery()
        if b:
            battery = {"percent": b.percent, "power_plugged": b.power_plugged,
                       "secsleft": b.secsleft}
    except Exception:
        pass

    procs = []
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
    try:
        import subprocess
        out = subprocess.check_output(
            ["systemctl", "list-units", "--type=service", "--no-pager",
             "--no-legend", "--all"],
            text=True, timeout=5
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
            "percent":        psutil.cpu_percent(interval=None),
            "per_core":       cpu_per_core,
            "cores_logical":  psutil.cpu_count(logical=True),
            "cores_physical": psutil.cpu_count(logical=False),
            "freq_mhz":       {"current": cpu_freq.current if cpu_freq else None,
                               "min":     cpu_freq.min     if cpu_freq else None,
                               "max":     cpu_freq.max     if cpu_freq else None},
            "load_avg":       list(os.getloadavg()),
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
    server = HTTPServer((host, port), Handler)
    print(f"SysMon running at http://{socket.gethostname()}:{port}")
    print(f"Also reachable at http://0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")

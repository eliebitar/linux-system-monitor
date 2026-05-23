# SysMon Dashboard

A lightweight, cross-platform system monitoring web dashboard built with the
Python stdlib + [`psutil`](https://pypi.org/project/psutil/). Runs on
**Linux and Windows**.

## Features

- **Real-time monitoring** — auto-refreshes every 3 seconds
- **CPU** — overall usage, per-core grid, frequency, load averages (Linux/macOS)
- **Memory & Swap** — usage bars with colour-coded thresholds
- **Disks** — all mounted partitions with usage stats
- **Network** — per-NIC I/O counters and live throughput
- **Processes** — top 30 by CPU usage (Linux) or recent PIDs (Windows)
- **Services** — systemd service list (Linux only)
- **Power** — watts measurement + electricity cost projections (€/hr/day/month/year)
  - Linux: Intel RAPL (`/sys/class/powercap/intel-rapl/*/energy_uj`)
  - Windows: WMI `BatteryStatus.DischargeRate`, with runtime / delta fallbacks
- **Temperatures**
  - Linux: `psutil.sensors_temperatures()`
  - Windows: WMI `MSAcpi_ThermalZoneTemperature` (tenths of Kelvin), with a
    clearly-labeled CPU-load-based estimate as last-resort fallback
- **Dark-themed UI** — colour-coded gauges (green / yellow / orange / red)

## Requirements

- Python 3.8+
- `psutil` (see `requirements.txt`)

Install dependencies in a virtual env:

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt    # Linux/macOS
venv\Scripts\pip.exe install -r requirements.txt   # Windows
```

## Usage

### Linux / macOS

```bash
bash start.sh
```

### Windows

```cmd
start.bat
```

Then open **http://localhost:3200** (or `http://<your-hostname>:3200` from any
LAN device).

### Configuration

| Env var       | Default   | Description              |
|---------------|-----------|--------------------------|
| `SYSMON_HOST` | `0.0.0.0` | Bind address             |
| `SYSMON_PORT` | `3200`    | TCP port                 |

## API

| Endpoint            | Method | Description                                  |
|---------------------|--------|----------------------------------------------|
| `/`                 | GET    | Dashboard UI                                 |
| `/api/stats`        | GET    | JSON system stats                            |
| `/api/power-config` | GET    | Current electricity rate                     |
| `/api/power-config` | POST   | Update rate `{"rate_eur_per_kwh": 0.35}`     |

## Power Monitoring

### Linux (RAPL)

RAPL (Running Average Power Limit) measures real CPU/package power draw. The
energy counters are root-readable by default; grant read access with:

```bash
sudo chmod o+r /sys/class/powercap/intel-rapl/*/energy_uj
```

If RAPL is unavailable (AMD without rapl support, VMs, containers), the
dashboard shows "unavailable" gracefully.

### Windows

Windows has no first-party CPU-package power counter, so SysMon falls back to
battery telemetry:

1. **`BatteryStatus.DischargeRate`** (WMI, mW) — sum across all batteries.
2. **Runtime estimate** — `full_charge_Wh × percent% / secs_remaining`.
3. **Delta estimate** — battery percent drop over time × capacity.

On AC-only desktops without a battery there is no software path to a watt
reading; SysMon reports `no_windows_power_sensor`. For desktop wattage on
Windows you would need a hardware monitor such as
[LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor).

## Temperatures on Windows

`MSAcpi_ThermalZoneTemperature` is exposed by most ACPI BIOSes but is sometimes
restricted to administrators. If no sensor is readable, SysMon shows a
clearly-labeled `CPU (estimated)` value derived from CPU load — this is a
heuristic, not a measurement.

## License

MIT

# SysMon Dashboard

A lightweight, zero-dependency system monitoring web dashboard built with Python stdlib + psutil.

## Features

- **Real-time monitoring** — auto-refreshes every 3 seconds
- **CPU** — overall usage, per-core grid, frequency, load averages
- **Memory & Swap** — usage bars with colour-coded thresholds
- **Disks** — all mounted partitions with usage stats
- **Network** — per-NIC I/O counters
- **Processes** — top 30 by CPU usage
- **Services** — systemd service list
- **Power** — RAPL-based watt measurement + electricity cost projections (€/hr/day/month/year)
- **Dark-themed UI** — colour-coded gauges (green / yellow / orange / red)

## Requirements

- Python 3.8+
- `psutil` (`sudo apt install python3-psutil` or `pip install psutil`)

## Usage

```bash
# Start the server
bash start.sh

# Or directly
python3 server.py
```

Then open **http://localhost:3200** (or `http://<your-hostname>:3200` from any LAN device).

## API

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Dashboard UI |
| `/api/stats` | GET | JSON system stats |
| `/api/power-config` | GET | Current electricity rate |
| `/api/power-config` | POST | Update rate `{"rate_eur_per_kwh": 0.35}` |

## Power Monitoring

RAPL (Running Average Power Limit) is used to measure real CPU/package power draw. To enable:

```bash
sudo chmod o+r /sys/class/powercap/intel-rapl/*/energy_uj
```

If unavailable, the dashboard shows "unavailable" gracefully.

## License

MIT

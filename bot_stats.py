import json
import os
import threading
from datetime import datetime

_STATS_FILE = "data/stats.json"
_lock = threading.Lock()

_start_time = datetime.utcnow()

_defaults = {
    "total_success": 0,
    "total_failure": 0,
    "total_retries": 0,
}

def _load():
    if os.path.exists(_STATS_FILE):
        try:
            with open(_STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {k: data.get(k, v) for k, v in _defaults.items()}
        except Exception:
            pass
    return _defaults.copy()

_counters = _load()
_active_tasks = 0

def _save():
    os.makedirs(os.path.dirname(_STATS_FILE), exist_ok=True)
    with open(_STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(_counters, f, ensure_ascii=False, indent=2)

def increment_success():
    with _lock:
        _counters["total_success"] += 1
        _save()

def increment_failure():
    with _lock:
        _counters["total_failure"] += 1
        _save()

def increment_retry():
    with _lock:
        _counters["total_retries"] += 1
        _save()

def increment_active():
    global _active_tasks
    with _lock:
        _active_tasks += 1

def decrement_active():
    global _active_tasks
    with _lock:
        _active_tasks = max(0, _active_tasks - 1)

def get_all():
    with _lock:
        return {
            "start_time": _start_time,
            "total_success": _counters["total_success"],
            "total_failure": _counters["total_failure"],
            "total_retries": _counters["total_retries"],
            "active_tasks": _active_tasks,
        }

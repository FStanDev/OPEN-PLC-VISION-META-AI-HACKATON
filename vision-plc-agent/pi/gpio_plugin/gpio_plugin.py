"""OpenPLC Runtime v4 Python plugin: bench GPIO + PCA9685 servo.

Loaded by path from ``plugins.conf`` (module name ``gpio_plugin``); the
loader puts this directory on ``sys.path``. Only stdlib imports at module
level — everything else is lazy inside :func:`init` so a failed import
becomes a loud ``False`` instead of a dead runtime.

Polling design: Python plugins get no per-scan hook (``cycle_start/end``
are native-only), so :func:`start_loop` runs a 10 ms thread calling
:mod:`io_bridge` ``sync()``. ``stop_loop``/``cleanup`` drive hardware to
``io_map.safe_state``.
"""

import os
import sys
import threading
import time
import traceback

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)  # agent.io_map
sys.path.insert(0, os.path.join(_REPO_ROOT, "pi"))  # pi.gpio, pi.pca9685

POLL_S = 0.01

_bridge = None
_buffers = None
_log_access = None
_stop = threading.Event()
_thread = None
_last_error_log = 0.0


def _log(level, message):
    line = f"[gpio_plugin] {message}"
    if _log_access is not None and _log_access.is_valid:
        try:
            log = getattr(_log_access, f"log_{level}", None)
            if log is not None and log(line)[0]:
                return
        except Exception:
            pass
    print(line, flush=True)


def init(runtime_args_capsule):
    """Build the bridge (io_map + GPIO + servo). Returns True when live."""
    global _bridge, _buffers, _log_access
    try:
        shared_dir = os.environ.get("OPENPLC_SHARED_DIR")
        if shared_dir:
            sys.path.insert(0, shared_dir)
        try:
            from shared import (  # type: ignore[import-not-found]
                SafeBufferAccess,
                SafeLoggingAccess,
                safe_extract_runtime_args_from_capsule,
            )
        except ImportError:
            _log(
                "error",
                "cannot import runtime 'shared' module; install the .pth "
                "from pi/gpio_plugin/README.md or set OPENPLC_SHARED_DIR",
            )
            return False

        runtime_args, err = safe_extract_runtime_args_from_capsule(runtime_args_capsule)
        if runtime_args is None:
            _log("error", f"no runtime args: {err}")
            return False
        _log_access = SafeLoggingAccess(runtime_args)
        _buffers = SafeBufferAccess(runtime_args)
        if not _buffers.is_valid:
            _log("error", f"no buffer access: {_buffers.error_msg}")
            return False

        from io_bridge import IoBridge  # sibling file; loader puts plugindir on sys.path

        from agent.io_map import load_io_map
        from pi.gpio import GpioZeroBackend
        from pi.pca9685 import Pca9685

        io_map_path = os.environ.get("VISION_PLC_IO_MAP", os.path.join(_REPO_ROOT, "io_map.yaml"))
        io_map = load_io_map(io_map_path)
        _log("info", f"loaded {io_map_path} ({len(io_map.signals)} signals)")
        try:
            pwm = Pca9685()
        except Exception as exc:
            _log("warn", f"servo disabled (optional): {exc}")
            pwm = None
        _bridge = IoBridge(io_map, GpioZeroBackend(), pwm)
        if _bridge.skipped:
            _log("warn", f"skipped: {', '.join(_bridge.skipped)}")
        _log("info", f"live: {len(_bridge.bindings)} bindings, poll {POLL_S * 1000:.0f} ms")
        return True
    except Exception:
        _log("error", f"init failed:\n{traceback.format_exc()}")
        return False


def start_loop():
    """Start the 10 ms sync thread."""
    global _thread
    _log("info", "start_loop")
    _stop.clear()
    _thread = threading.Thread(target=_run, name="gpio_plugin", daemon=True)
    _thread.start()
    return 0


def stop_loop():
    """Stop syncing and drive hardware to the safe state."""
    global _thread
    _log("info", "stop_loop")
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=2.0)
        _thread = None
    if _bridge is not None:
        try:
            _bridge.apply_safe_state()
            _log("info", "safe state applied")
        except Exception as exc:
            _log("error", f"safe state failed: {exc}")


def cleanup():
    """Release GPIO on unload."""
    global _bridge, _buffers, _log_access
    _log("info", "cleanup")
    stop_loop()
    if _bridge is not None:
        try:
            _bridge.close()
        except Exception:
            pass
    _bridge = None
    _buffers = None
    _log_access = None


def _run():
    global _last_error_log
    while not _stop.is_set():
        try:
            _bridge.sync(_buffers)
        except Exception as exc:
            now = time.monotonic()
            if now - _last_error_log >= 1.0:  # throttle a sick bus to 1 line/s
                _last_error_log = now
                _log("error", f"sync: {exc}")
        _stop.wait(POLL_S)


if __name__ == "__main__":
    print("OpenPLC plugin entry: loaded by the runtime, not run directly.")
    print("Expected functions: init(capsule), start_loop(), stop_loop(), cleanup().")

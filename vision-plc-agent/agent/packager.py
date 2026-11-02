"""Builds program.zip for OpenPLC Runtime v4 (see docs/SPEC.md).

The layout mirrors the OpenPLC Editor's ``composeRuntimeV4Bundle``: the
concatenated ``program.st`` plus the STruC++ artefacts (``generated.cpp`` /
``generated.hpp``), ``defines.h`` (``PROGRAM_MD5``), a ``c_blocks.h`` stub,
``strucpp_runtime/include/*.hpp`` and ``conf/*.json``.

Two deliberate deviations from the Editor bundle:

- ``generated_debug.cpp`` is an empty debug table. The STruC++ CLI does not
  emit the debugger pointer tables (only the ``strucpp`` npm library does),
  and the agent loop never uses the debug protocol. Scan execution,
  located I/O and Modbus are unaffected: they use ``ConfigurationInstance``
  and ``locatedVars``, not this table.
- No ``debug-map.json``: nothing on the runtime side consumes it.

The ``conf/`` directory is load-bearing, not optional: on upload the runtime
enables exactly the plugins with a matching ``conf/<name>.json`` and disables
everything else. The bundle therefore always ships ``conf/gpio_plugin.json``
(keeps our I/O plugin enabled) and ``conf/modbus_slave.json`` (tag readback).
"""

import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path

from agent.strucpp import StrucppNotFoundError, compile_st

DEFAULT_TASK_INTERVAL_MS = 20
DEFAULT_MODBUS_HOST = "0.0.0.0"
DEFAULT_MODBUS_PORT = 5020

# Buffer sizes matching the runtime's fixed BUFFER_SIZE image tables
# (1024 bytes/registers, 8192 bits) and the Modbus plugin defaults.
MODBUS_BUFFER_MAPPING = {
    "holding_registers": {"qw_count": 1024, "mw_count": 1024, "md_count": 1024, "ml_count": 1024},
    "coils": {"qx_bits": 8192, "mx_bits": 0},
    "discrete_inputs": {"ix_bits": 8192},
    "input_registers": {"iw_count": 1024},
}

# Placeholder config for our GPIO plugin, which never reads it. The runtime
# copies this file over the plugin's config_path on upload; it must stay
# byte-identical to pi/gpio_plugin/gpio_plugin.json so uploads do not dirty
# the checkout. (The 6-field plugins.conf line needs a non-empty 5th field
# for the venv path to be parsed; see THIRD_PARTY.md / runtime docs.)
GPIO_PLUGIN_CONF = (
    '{"_note": "Placeholder: the runtime\'s plugins.conf parser (strtok) cannot handle '
    "an empty 5th field, so this file must exist for the 6th field (venv_path) to be "
    'read. The plugin never reads this file."}\n'
)

CONFIGURATION_TEMPLATE = """CONFIGURATION Config0
\tRESOURCE Res0 ON PLC
\t\tTASK Task0(INTERVAL := T#{interval_ms}ms, PRIORITY := 1);

\t\tPROGRAM Inst0 WITH Task0 : {program_name};
\tEND_RESOURCE
END_CONFIGURATION
"""

DEBUG_TABLE_STUB = """// Minimal debug table: no debugger leaves (the STruC++ CLI does not emit
// the debugger pointer tables, and the agent loop never uses the debug
// protocol). With zero arrays every debug-table query safely returns
// "not found". Scan execution, located I/O and Modbus are unaffected.
#include "debug_table.hpp"

namespace strucpp { namespace debug {

const Entry* const debug_arrays[] STRUCPP_DEBUG_FLASH = { nullptr };
const uint16_t debug_array_counts[] STRUCPP_DEBUG_FLASH = { 0 };
const uint8_t debug_array_count = 0;

const RetainVar retain_vars[] STRUCPP_DEBUG_FLASH = {};
const uint16_t retain_var_count = 0;
const uint32_t retain_layout_hash = 0;

} } // namespace strucpp::debug
"""


class PackagerError(RuntimeError):
    """The bundle could not be built (ST error, missing headers, ...)."""


def build_program_st(body_st: str, *, interval_ms: int = DEFAULT_TASK_INTERVAL_MS) -> str:
    """Append the default CONFIGURATION block (editor-style Config0/Res0/Task0).

    The program type name is parsed from the first ``PROGRAM <name>`` in the
    body. A body that already declares a CONFIGURATION is returned unchanged.
    """
    if re.search(r"(?im)^\s*CONFIGURATION\b", body_st):
        return body_st
    match = re.search(r"(?im)^\s*PROGRAM\s+(\w+)", body_st)
    program_name = match.group(1) if match else "Main"
    configuration = CONFIGURATION_TEMPLATE.format(
        interval_ms=interval_ms, program_name=program_name
    )
    return body_st.rstrip("\n") + "\n\n" + configuration


def find_runtime_include(strucpp: str = "strucpp") -> Path:
    """Locate the strucpp_runtime headers shipped with the STruC++ binary."""
    exe = shutil.which(strucpp)
    if exe is None:
        raise StrucppNotFoundError(
            f"strucpp binary not found: {strucpp!r} "
            "(install the STruC++ prebuilt binary, see docs/SPEC.md)"
        )
    include_dir = Path(exe).resolve().parent / "runtime" / "include"
    if not (include_dir / "iec_std_lib.hpp").is_file():
        raise PackagerError(f"strucpp runtime headers not found at {include_dir}")
    return include_dir


def modbus_slave_conf(host: str = DEFAULT_MODBUS_HOST, port: int = DEFAULT_MODBUS_PORT) -> str:
    """Render conf/modbus_slave.json (editor schema, fixed-image sizes)."""
    return json.dumps(
        {
            "network_configuration": {"host": host, "port": port},
            "buffer_mapping": MODBUS_BUFFER_MAPPING,
        },
        indent=2,
    )


def package(
    st_path: str | Path,
    out_zip: str | Path,
    *,
    strucpp: str = "strucpp",
    runtime_include: str | Path | None = None,
    modbus_host: str = DEFAULT_MODBUS_HOST,
    modbus_port: int = DEFAULT_MODBUS_PORT,
    extra_conf: Mapping[str, str] | None = None,
    interval_ms: int = DEFAULT_TASK_INTERVAL_MS,
) -> Path:
    """Build program.zip from an ST program; return the zip path.

    Args:
        st_path: ST source (a body without CONFIGURATION is fine).
        out_zip: destination ``program.zip`` (parent dirs are created).
        strucpp: STruC++ binary (PATH name or file path).
        runtime_include: strucpp_runtime headers dir (auto-detected by default).
        modbus_host/port: bind address for the runtime's Modbus slave.
        extra_conf: extra ``conf/<name>.json`` entries (merged over defaults).
        interval_ms: scan task interval for the appended CONFIGURATION.
    """
    st_path = Path(st_path)
    if not st_path.is_file():
        raise FileNotFoundError(f"ST file not found: {st_path}")
    include_dir = Path(runtime_include) if runtime_include else find_runtime_include(strucpp)
    if not (include_dir / "iec_std_lib.hpp").is_file():
        raise PackagerError(f"strucpp runtime headers not found at {include_dir}")

    program_st = build_program_st(st_path.read_text(encoding="utf-8"), interval_ms=interval_ms)
    md5 = hashlib.md5(program_st.encode("utf-8")).hexdigest()

    out_zip = Path(out_zip)
    out_zip.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="program-zip-") as tmp:
        tmpdir = Path(tmp)
        (tmpdir / "program.st").write_text(program_st, encoding="utf-8")
        result = compile_st(tmpdir / "program.st", tmpdir / "generated.cpp", strucpp=strucpp)
        if not result.ok:
            raise PackagerError(f"strucpp failed:\n{result.stderr.strip()}")
        generated_cpp = (tmpdir / "generated.cpp").read_bytes()
        generated_hpp = (tmpdir / "generated.hpp").read_bytes()
        if b"Configuration_" not in generated_cpp:
            raise PackagerError("strucpp output has no Configuration (need CONFIGURATION block)")

        conf: dict[str, str] = {
            "gpio_plugin.json": GPIO_PLUGIN_CONF,
            "modbus_slave.json": modbus_slave_conf(modbus_host, modbus_port),
        }
        conf.update(extra_conf or {})

        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("program.st", program_st)
            zf.writestr("generated.cpp", generated_cpp)
            zf.writestr("generated.hpp", generated_hpp)
            zf.writestr("generated_debug.cpp", DEBUG_TABLE_STUB)
            zf.writestr("defines.h", f'#pragma once\n// Program MD5\n#define PROGRAM_MD5 "{md5}"\n')
            zf.writestr("c_blocks.h", "// Empty file\n")
            for header in sorted(include_dir.glob("*.hpp")):
                zf.write(header, f"strucpp_runtime/include/{header.name}")
            for name, content in conf.items():
                zf.writestr(f"conf/{name}", content)

    return out_zip


if __name__ == "__main__":  # pragma: no cover - thin CLI for manual deploys
    import argparse

    parser = argparse.ArgumentParser(description="Build program.zip for OpenPLC Runtime v4")
    parser.add_argument("st", help="ST source file")
    parser.add_argument("-o", "--out", default="program.zip", help="destination zip")
    parser.add_argument("--strucpp", default="strucpp", help="strucpp binary")
    parser.add_argument("--modbus-host", default=DEFAULT_MODBUS_HOST)
    parser.add_argument("--modbus-port", type=int, default=DEFAULT_MODBUS_PORT)
    args = parser.parse_args()
    try:
        package(
            args.st,
            args.out,
            strucpp=args.strucpp,
            modbus_host=args.modbus_host,
            modbus_port=args.modbus_port,
        )
    except (PackagerError, StrucppNotFoundError, FileNotFoundError) as exc:
        parser.error(str(exc))
    print(f"Wrote {args.out}")

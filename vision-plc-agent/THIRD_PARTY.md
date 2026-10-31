# Third-party dependencies

Every dependency and its licence. Licences verified from installed package
metadata (exact resolved versions are pinned in `uv.lock`).

## Runtime

| Package | Constraint | Licence | Use |
| --- | --- | --- | --- |
| pydantic | >=2.0 | MIT | Task spec + I/O map schemas |
| PyYAML | >=6.0 | MIT | YAML parsing |
| httpx | >=0.28 | BSD-3-Clause | OpenPLC REST client |
| pymodbus | >=3.0 | BSD-3-Clause | Tag readback over Modbus TCP |

## Development

| Package | Constraint | Licence | Use |
| --- | --- | --- | --- |
| pytest | >=8.0 | MIT | Tests |
| ruff | >=0.8 | MIT | Lint + format |

## Build

| Package | Licence | Use |
| --- | --- | --- |
| hatchling | MIT | Build backend |

"""Runtime configuration loader.

Single source of truth: ``config/config.yaml``. There are no fallbacks,
no overlays, no per-environment shadow files. If the file is missing or
the file's selected ``type:`` section is missing, this module raises on
import --- the application refuses to start with a misconfigured runtime
rather than booting with stubs.

To populate the file:

    cp config/config.yaml.example config/config.yaml
    # then edit config/config.yaml in place; every required field must
    # be set before the stack will start.
"""

from pathlib import Path

from dynaconf import Dynaconf

_repo_root = Path(__file__).parents[1]
_config_file = _repo_root / "config" / "config.yaml"

if not _config_file.is_file():
    raise RuntimeError(
        f"missing {_config_file}; "
        f"run `cp config/config.yaml.example config/config.yaml` and "
        f"populate every required field before starting the stack"
    )

_raw = Dynaconf(settings_files=[str(_config_file)])

if "type" not in _raw or _raw.type not in _raw:
    raise RuntimeError(
        f"{_config_file} must declare a top-level `type:` field naming a "
        f"section defined in the same file (e.g. `type: development`)"
    )

config = _raw[_raw.type]

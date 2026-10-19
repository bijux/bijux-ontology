"""Stdlib-only ownership envelope around the actual separately installed CLI."""

import errno
import importlib.metadata
import json
import os
import runpy
import sys
from pathlib import Path

REQUIRED = {
    "morph-kgc": "2.10.0",
    "rdflib": "7.2.1",
    "pyoxigraph": "0.3.22",
    "pandas": "2.3.3",
    "sqlalchemy": "2.1.5",
}


def main() -> None:
    """Bound actual file growth; never synthesize the materializer's RDF output."""
    request_path, receipt_path = Path(sys.argv[1]), Path(sys.argv[2])
    cap = int(sys.argv[3])
    if cap < 512 or request_path.parent != receipt_path.parent:
        raise ValueError("Invalid owned materialization bounds")
    with request_path.open("rb") as reader:
        raw = reader.read(cap + 1)
    if len(raw) > cap:
        raise ValueError("Oversized native control request")
    request = json.loads(raw)
    receipt: dict[str, object] = {"status": "unverified"}
    try:
        versions = {name: importlib.metadata.version(name) for name in REQUIRED}
        receipt["versions"] = versions
        receipt["python"] = list(sys.version_info[:3])
        if versions != REQUIRED or os.name != "posix":
            raise ValueError("Unqualified installed Morph-KGC distribution profile")
        if request == {"action": "capability"}:
            receipt["status"] = "completed"
        elif (
            set(request) == {"action", "config"} and request["action"] == "materialize"
        ):
            import resource

            config = Path(request["config"])
            if config.parent != request_path.parent or config.is_symlink():
                raise ValueError("Materialization config is not an owned local file")
            resource.setrlimit(resource.RLIMIT_FSIZE, (cap, cap))
            sys.argv = ["morph_kgc", str(config)]
            runpy.run_module("morph_kgc", run_name="__main__")
            receipt["status"] = "completed"
        else:
            raise ValueError("Unsupported native control request")
    except MemoryError:
        receipt["status"] = "exhausted"
    except OSError as error:
        receipt["status"] = "exhausted" if error.errno == errno.EFBIG else "unverified"
        receipt["error"] = type(error).__name__
    except Exception as error:
        receipt["error"] = type(error).__name__
    encoded = json.dumps(receipt, separators=(",", ":")).encode()
    if len(encoded) > cap:
        raise ValueError("Oversized native completion receipt")
    with receipt_path.open("xb") as stream:
        stream.write(encoded)


if __name__ == "__main__":
    main()

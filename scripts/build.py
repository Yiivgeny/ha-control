"""Build a deployable archive; copy the single tool contract into the HA adapter."""

import argparse
import hashlib
import json
import shutil
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-rootfs", type=Path, help="Exported official Python image root filesystem for the target architecture")
    args = parser.parse_args()
    if args.offline_rootfs and not args.offline_rootfs.is_file():
        parser.error("--offline-rootfs must name an existing tar.gz file")
    contract = ROOT / "addon/ha_control/contracts.json"
    tools = json.loads(contract.read_text())
    assert len(tools) == 5
    size = len(json.dumps({"tools": tools}, ensure_ascii=False, separators=(",", ":")).encode())
    assert size <= 8192, size
    shutil.copy2(contract, ROOT / "custom_components/ha_control/contracts.json")
    translations = ROOT / "custom_components/ha_control/translations"
    translations.mkdir(exist_ok=True)
    shutil.copy2(ROOT / "custom_components/ha_control/strings.json", translations / "en.json")
    (ROOT / "dist").mkdir(exist_ok=True)
    destination = ROOT / "dist" / ("ha-control-offline-amd64.tar.gz" if args.offline_rootfs else "ha-control.tar.gz")
    if not list((ROOT / "addon/wheels").glob("mcp-2.0.0-*.whl")):
        parser.error("Download the locked runtime wheelhouse into addon/wheels before building")
    with tarfile.open(destination, "w:gz") as archive:
        for folder in ["addon", "custom_components"]:
            for path in sorted((ROOT / folder).rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    if args.offline_rootfs and path == ROOT / "addon/Dockerfile":
                        continue
                    archive.add(path, arcname=path.relative_to(ROOT))
        if args.offline_rootfs:
            archive.add(ROOT / "addon/Dockerfile.offline", arcname="addon/Dockerfile")
            archive.add(args.offline_rootfs, arcname="addon/python-rootfs.tar.gz")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(destination.suffix + ".sha256").write_text(f"{digest}  {destination.name}\n")
    print(json.dumps({"archive": str(destination), "tools": len(tools), "schema_bytes": size, "sha256": digest}))


if __name__ == "__main__":
    main()

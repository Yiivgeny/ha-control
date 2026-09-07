"""Build the HACS release using only standard Python; include no local state."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version')
    args = parser.parse_args()
    integration = ROOT / 'custom_components/ha_control'
    manifest = json.loads((integration / 'manifest.json').read_text())
    version = manifest['version']
    if args.version and args.version != version:
        raise SystemExit('Tag and integration version differ')
    addon = (ROOT / 'addon/config.yaml').read_text()
    assert re.search(r'^version: "' + re.escape(version) + r'"$', addon, re.M)
    contract = ROOT / 'addon/ha_control/contracts.json'
    tools = json.loads(contract.read_text())
    size = len(json.dumps({'tools': tools}, ensure_ascii=False, separators=(',', ':')).encode())
    assert len(tools) == 5 and size <= 8192
    shutil.copy2(contract, integration / 'contracts.json')
    (integration / 'translations').mkdir(exist_ok=True)
    shutil.copy2(integration / 'strings.json', integration / 'translations/en.json')
    output = ROOT / 'dist/ha_control.zip'
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(integration.rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts:
                archive.write(path, path.relative_to(integration))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(f'{digest}  {output.name}\n')
    print(json.dumps({'version': version, 'tools': 5, 'contract_bytes': size, 'archive': str(output)}))


if __name__ == '__main__':
    main()

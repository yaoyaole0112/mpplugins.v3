"""Build the ResourceFlow release asset consumed by MoviePilot v3."""

import argparse
import ast
import json
from pathlib import Path
import re
import subprocess
import zipfile


def release_metadata(root):
    plugin = root / "plugins.v3/resourceflow"
    tree = ast.parse((plugin / "__init__.py").read_text(encoding="utf-8"))
    declaration = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ResourceFlow")
    version = next(ast.literal_eval(node.value) for node in declaration.body
                   if isinstance(node, ast.Assign) and any(
                       isinstance(target, ast.Name) and target.id == "plugin_version" for target in node.targets))
    if not re.fullmatch(r"[1-9]\d?\.(?:0|[1-9]\d?)\.[1-9]\d?", version):
        raise ValueError("Invalid plugin version")
    manifest = json.loads((root / "package.v3.json").read_text(encoding="utf-8"))["ResourceFlow"]
    package = json.loads((plugin / "package.json").read_text(encoding="utf-8"))
    if version != manifest["version"] or version != package["version"]:
        raise ValueError("Backend, marketplace and frontend versions must agree")
    if manifest.get("release") is not True:
        raise ValueError("Marketplace must declare release=true")
    notes = manifest.get("history", {}).get(f"v{version}", "").strip()
    if not notes:
        raise ValueError("Missing release notes")
    tag = f"ResourceFlow_v{version}"
    return {"version": version, "tag": tag, "asset": f"{tag.lower()}.zip", "notes": notes}


def build_release(root, output):
    metadata = release_metadata(root)
    plugin = root / "plugins.v3/resourceflow"
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "plugins.v3/resourceflow"], cwd=root).decode().split("\0")
    files = []
    for filename in filter(None, tracked):
        path = root / filename
        relative = path.relative_to(plugin)
        if path.is_symlink():
            raise ValueError(f"Release cannot contain symlinks: {relative}")
        if (len(relative.parts) == 1 and (path.suffix == ".py" or relative.name in {
                "requirements.txt", "README.md", "package.json"})) or (
                relative.parts[0] == "dist" and path.suffix in {".js", ".css", ".svg", ".png", ".jpeg", ".webp"}):
            files.append((path, relative.as_posix()))
    names = {name for _, name in files}
    required = {"__init__.py", "models.py", "channel_dedup.py", "active_dedup.py", "resource_dedup.py",
                "shared_protocol.py", "shared_coordinator.py", "shared_plugin.py", "shared_models.py", "telegram_rpc.py",
                "dist/assets/remoteEntry.js"}
    if not required.issubset(names):
        raise ValueError("Release is missing runtime modules or the built frontend")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / metadata["asset"]
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path, name in sorted(files, key=lambda item: item[1]):
            bundle.write(path, name)
    (output / "notes.md").write_text(metadata["notes"] + "\n", encoding="utf-8")
    (output / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    print(build_release(arguments.root.resolve(), arguments.output.resolve()))


if __name__ == "__main__":
    main()

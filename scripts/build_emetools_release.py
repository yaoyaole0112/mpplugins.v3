"""Build the EmeTools asset accepted by MoviePilot v3's official release installer."""

import argparse
import ast
import json
from pathlib import Path
import re
import subprocess
import zipfile


def release_metadata(root):
    plugin = root / "plugins.v3/emetools"
    tree = ast.parse((plugin / "__init__.py").read_text(encoding="utf-8"))
    declaration = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "EmeTools")
    version = next(ast.literal_eval(node.value) for node in declaration.body
                   if isinstance(node, ast.Assign) and any(
                       isinstance(target, ast.Name) and target.id == "plugin_version" for target in node.targets))
    if not re.fullmatch(r"[1-9]\d?\.(?:0|[1-9]\d?)\.[1-9]\d?", version):
        raise ValueError("插件版本必须为三段数字，每段不超过99，末位从1开始")
    manifest = json.loads((root / "package.v3.json").read_text(encoding="utf-8"))["EmeTools"]
    package = json.loads((plugin / "package.json").read_text(encoding="utf-8"))
    if version != manifest["version"] or version != package["version"]:
        raise ValueError("插件、市场清单与前端版本号不一致")
    if manifest.get("release") is not True:
        raise ValueError("市场清单必须声明 release=true")
    notes = manifest.get("history", {}).get(f"v{version}", "").strip()
    if not notes:
        raise ValueError("缺少当前版本更新说明")
    tag = f"EmeTools_v{version}"
    return {"version": version, "tag": tag, "asset": f"{tag.lower()}.zip", "notes": notes}


def build_release(root, output):
    metadata = release_metadata(root)
    plugin = root / "plugins.v3/emetools"
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "plugins.v3/emetools"], cwd=root).decode().split("\0")
    files = []
    for filename in filter(None, tracked):
        path = root / filename
        relative = path.relative_to(plugin)
        if path.is_symlink():
            raise ValueError(f"发布包不允许符号链接：{relative}")
        if (len(relative.parts) == 1 and (path.suffix == ".py" or relative.name in {
                "requirements.txt", "icon.jpeg", "README.md", "package.json"})) or (
                relative.parts[0] == "dist" and path.suffix in {".js", ".css", ".svg", ".png", ".jpeg", ".webp"}):
            files.append((path, relative.as_posix()))
    names = {name for path, name in files}
    if not {"__init__.py", "requirements.txt", "dist/assets/remoteEntry.js"}.issubset(names):
        raise ValueError("发布包缺少插件入口、依赖或已构建的远程组件入口")
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

"""Download and verify pinned local wake/command/face model artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from app.config import BASE_DIR


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact(spec: dict[str, Any]) -> tuple[bool, str]:
    target = (BASE_DIR / spec["target"]).resolve()
    if not target.exists():
        return False, "missing"
    if spec["kind"] == "file":
        actual = sha256_file(target)
        return actual == spec["sha256"], actual
    missing = [relative for relative in spec.get("required", []) if not (target / relative).is_file()]
    return not missing, f"missing required files: {', '.join(missing)}" if missing else "present"


def provision(spec: dict[str, Any]) -> None:
    target = (BASE_DIR / spec["target"]).resolve()
    ok, detail = verify_artifact(spec)
    if ok:
        print(f"verified {spec['name']}: {detail}")
        return
    if target.exists():
        raise RuntimeError(f"Refusing to overwrite unverified model target {target}; move it aside and retry")

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reachy-model-") as temporary:
        temp_dir = Path(temporary)
        archive = temp_dir / "download"
        print(f"downloading {spec['name']}")
        urllib.request.urlretrieve(spec["url"], archive)
        actual = sha256_file(archive)
        if actual != spec["sha256"]:
            raise RuntimeError(
                f"Checksum mismatch for {spec['name']}: expected {spec['sha256']}, got {actual}"
            )

        if spec["kind"] == "file":
            shutil.move(str(archive), target)
        else:
            extract_root = temp_dir / "extract"
            extract_root.mkdir()
            if spec["kind"] == "zip_directory":
                with zipfile.ZipFile(archive) as bundle:
                    _validate_members(extract_root, bundle.namelist())
                    bundle.extractall(extract_root)
            elif spec["kind"] == "tar_bz2_directory":
                with tarfile.open(archive, "r:bz2") as bundle:
                    _validate_members(extract_root, [member.name for member in bundle.getmembers()])
                    bundle.extractall(extract_root, filter="data")
            else:
                raise RuntimeError(f"Unsupported artifact kind: {spec['kind']}")
            source = extract_root / spec["archive_directory"]
            if not source.is_dir():
                raise RuntimeError(f"Expected archive directory is missing: {source.name}")
            shutil.move(str(source), target)

    ok, detail = verify_artifact(spec)
    if not ok:
        raise RuntimeError(f"Provisioned artifact failed verification: {spec['name']} ({detail})")
    print(f"verified {spec['name']}: {detail}")


def _validate_members(root: Path, members: list[str]) -> None:
    resolved_root = root.resolve()
    for member in members:
        destination = (root / member).resolve()
        if not destination.is_relative_to(resolved_root):
            raise RuntimeError(f"Unsafe archive member: {member}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    manifest_path = BASE_DIR / "model-assets.json"
    if not manifest_path.exists():
        manifest_path = BASE_DIR / "app" / "assets" / "model-assets.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failures = []
    for spec in manifest["artifacts"]:
        if args.verify_only:
            ok, detail = verify_artifact(spec)
            print(f"{'ok' if ok else 'failed'} {spec['name']}: {detail}")
            if not ok:
                failures.append(spec["name"])
        else:
            provision(spec)
    if failures:
        raise SystemExit(f"Model verification failed: {', '.join(failures)}")


if __name__ == "__main__":
    main()

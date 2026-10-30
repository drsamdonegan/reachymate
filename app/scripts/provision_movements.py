"""Provision pinned Hugging Face motion JSON, optionally including all tagged community sets."""

import argparse
import hashlib
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.config import Settings
from app.services.reachy.motion.community import COMMUNITY_TAG, MAX_RECORDING_BYTES, recording_id
from app.services.reachy.motion.expressions import LIBRARIES, MotionLibrary


def provision_community(root: Path) -> dict:
    from huggingface_hub import HfApi, hf_hub_download

    api = HfApi(token=False)
    official = {repo for repo, _ in LIBRARIES.values()}
    datasets = [d for d in api.list_datasets(filter=COMMUNITY_TAG, full=True) if d.id not in official]
    destination = root / "community"
    destination.mkdir(parents=True, exist_ok=True)
    moves, skipped, errors, sources = {}, [], [], []

    def install(repo, revision, filename, license_name):
        cached = Path(hf_hub_download(repo, filename, repo_type="dataset", revision=revision, token=False))
        if cached.stat().st_size > MAX_RECORDING_BYTES:
            return None, "recording_too_large"
        content = cached.read_bytes()
        try:
            data = json.loads(content)
            MotionLibrary._validate_recording(data)
        except (ValueError, KeyError, TypeError, IndexError):
            return None, "unsupported_recording_format"
        name = recording_id(repo, filename)
        shutil.copyfile(cached, destination / f"{name}.json")
        return (
            name,
            {
                "repo": repo,
                "revision": revision,
                "source_file": filename,
                "license": license_name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "title": Path(filename).stem[:120],
                "description": " ".join(str(data.get("description", "")).split())[:160],
                "duration_seconds": float(data["time"][-1]),
            },
        ), None

    # Pin the discovered revision before downloading. Only JSON is read; no
    # dataset scripts, pickles, audio tracks or robot commands are executed.
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = {}
        for dataset in datasets:
            try:
                info = api.dataset_info(dataset.id, revision=dataset.sha, files_metadata=True)
                license_name = next((t[8:] for t in info.tags if t.startswith("license:")), "unspecified")
                files = [f for f in info.siblings if f.rfilename.endswith(".json")]
                sources.append(
                    {
                        "repo": dataset.id,
                        "revision": dataset.sha,
                        "json_files": len(files),
                        "license": license_name,
                    }
                )
                for file in files:
                    if file.size and file.size > MAX_RECORDING_BYTES:
                        skipped.append(
                            {"repo": dataset.id, "file": file.rfilename, "reason": "recording_too_large"}
                        )
                        continue
                    pending[pool.submit(install, dataset.id, dataset.sha, file.rfilename, license_name)] = (
                        dataset.id,
                        file.rfilename,
                    )
            except Exception as exc:
                errors.append({"repo": dataset.id, "reason": type(exc).__name__})
        for index, future in enumerate(as_completed(pending), 1):
            repo, filename = pending[future]
            try:
                result, reason = future.result()
                if result:
                    name, entry = result
                    moves[name] = entry
                else:
                    skipped.append({"repo": repo, "file": filename, "reason": reason})
            except Exception as exc:
                errors.append({"repo": repo, "file": filename, "reason": type(exc).__name__})
            if index % 100 == 0:
                print(f"Community: {index}/{len(pending)} files processed, {len(moves)} moves", flush=True)
    manifest = {
        "tag": COMMUNITY_TAG,
        "sources": sources,
        "moves": moves,
        "skipped": skipped,
        "errors": errors,
    }
    temporary = root / "community-manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    os.replace(temporary, root / "community-manifest.json")
    print(
        f"Community: {len(moves)} moves from {len(sources)} datasets; "
        f"{len(skipped)} incompatible files; {len(errors)} download errors",
        flush=True,
    )
    if errors:
        raise RuntimeError("Some datasets could not be downloaded; rerun provisioning to finish")
    return manifest


def main():
    from huggingface_hub import snapshot_download

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--community", action="store_true", help="Include every public dataset tagged for Reachy Mini moves"
    )
    args = parser.parse_args()
    root = Path(Settings().motion_assets_dir)
    manifest = {}
    for kind, (repo, revision) in LIBRARIES.items():
        destination = root / kind
        snapshot_download(
            repo,
            repo_type="dataset",
            revision=revision,
            local_dir=destination,
            allow_patterns=["*.json", "README.md", "LICENSE*"],
            token=False,
        )
        manifest[kind] = {
            "repo": repo,
            "revision": revision,
            "files": {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(destination.glob("*.json"))
            },
        }
        print(f"{kind}: {len(manifest[kind]['files'])} pinned movements")
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if args.community:
        provision_community(root)


if __name__ == "__main__":
    main()

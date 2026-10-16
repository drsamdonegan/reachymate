"""Download pinned official Pollen motion data without code or sound playback."""

import hashlib
import json
from pathlib import Path

from app.config import Settings
from app.services.reachy.motion.expressions import LIBRARIES


def main():
    from huggingface_hub import snapshot_download

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


if __name__ == "__main__":
    main()

#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
task_identity=${SIGNING_IDENTITY:?Set SIGNING_IDENTITY to a stable local signing certificate}
task_bundle="$task_root/deploy/macos/build/Reachy Concierge.app"
mkdir -p "$task_bundle/Contents/MacOS"
cp "$task_root/deploy/macos/Info.plist" "$task_bundle/Contents/Info.plist"
swiftc "$task_root/deploy/macos/ReachyConcierge.swift" -o "$task_bundle/Contents/MacOS/ReachyConcierge" -framework AppKit -framework AVFoundation
codesign --force --options runtime --sign "$task_identity" "$task_bundle"
codesign --verify --deep --strict "$task_bundle"

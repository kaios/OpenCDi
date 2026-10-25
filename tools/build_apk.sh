#!/bin/sh
# Build the Android app: tools/build_apk.sh [debug|release]  ->  dist/opencdi-<version>-arm64-v8a.apk
#
# Needs buildozer in a virtualenv (default ~/.venvs/opencdi-android, override
# with OPENCDI_ANDROID_VENV) and the build packages listed in android/README.md.
# The first run downloads the Android SDK and NDK (several GB, ~/.buildozer)
# and builds Python, pygame and numpy for ARM64, which takes a long time;
# later runs only repackage.  No BIOS is ever included.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
AND=$ROOT/android
MODE=${1:-debug}
VENV=${OPENCDI_ANDROID_VENV:-$HOME/.venvs/opencdi-android}
[ -x "$VENV/bin/buildozer" ] || { echo "buildozer not found in $VENV (see android/README.md)" >&2; exit 1; }
export PATH="$VENV/bin:$PATH" VIRTUAL_ENV="$VENV"     # buildozer installs into the venv only when it sees VIRTUAL_ENV
VERSION=$(sed -n "s/^__version__ = '\(.*\)'/\1/p" "$ROOT/opencdi/__init__.py")
API=29                                   # android.ndk_api in buildozer.spec

# 1. stage the sources: the opencdi package and main.py, nothing else
SRC=$AND/build/src
rm -rf "$SRC"
mkdir -p "$SRC"
cp -r "$ROOT/opencdi" "$SRC/"
cp "$AND/main.py" "$SRC/"
find "$SRC" -name __pycache__ -prune -exec rm -rf {} +
rm -f "$SRC"/opencdi/native/_m68k_* "$SRC"/opencdi/native/prebuilt.json

ndk() { ls -d "$HOME"/.buildozer/android/platform/android-ndk-*/ 2>/dev/null | sort | tail -1 | sed "s:/$::"; }
if [ -z "$(ndk)" ]; then
    echo "first build: buildozer installs the Android SDK and NDK first"
    (cd "$AND" && buildozer android "$MODE") || true
fi
NDK=$(ndk)
[ -n "$NDK" ] || { echo "Android NDK not found under ~/.buildozer" >&2; exit 1; }

# 2. cross-compile the native CPU core for arm64 and record it for the app
CC=$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android$API-clang
NAT=$SRC/opencdi/native
GEN=$(mktemp -d)
trap 'rm -rf "$GEN"' EXIT
SO=$(cd "$ROOT" && python3 -m opencdi.m68k_native --sources "$GEN")
echo "compiling $SO for arm64-v8a"
"$CC" -O2 -fwrapv -fno-strict-aliasing -shared -fPIC -o "$NAT/$SO" "$GEN/core.c" "$GEN/video.c"
(cd "$SRC" && python3 -m opencdi.m68k_native --record "$NAT" >/dev/null)
find "$SRC" -name __pycache__ -prune -exec rm -rf {} +

# 3. package
(cd "$AND" && buildozer android "$MODE")
APK=$(ls -t "$AND"/bin/*.apk | head -1)
mkdir -p "$ROOT/dist"
OUT=$ROOT/dist/opencdi-$VERSION-arm64-v8a-$MODE.apk
cp "$APK" "$OUT"
echo "built $OUT"

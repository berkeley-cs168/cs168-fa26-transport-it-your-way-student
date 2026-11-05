#!/usr/bin/env bash
# Download, check, and build the pinned ns-3 release, then build the
# transportlab ns-3 backend (transportlab/_tlsim) against this computer's
# python3. `make setup` runs this.
#
# Everything goes in the repo's build/ folder, which git ignores. Running it
# again only rebuilds what is missing. TRANSPORTLAB_CACHE puts ns-3 somewhere
# else instead; the lab VM and Gradescope images use it to ship ns-3 prebuilt.
#
# Needs: a C++ compiler, cmake, and python3 with its headers. No root.
#
#     tools/setup-ns3.sh             ns-3 and the backend
#     tools/setup-ns3.sh --ns3-only  just ns-3, for the lab VM image
set -euo pipefail

NS3_ONLY=0
[ "${1:-}" = "--ns3-only" ] && NS3_ONLY=1

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CACHE="${TRANSPORTLAB_CACHE:-$REPO/build}"
PYTHON="${PYTHON:-python3}"

# The pin: version, download URL, checksum.
VERSION=$(grep '^version=' "$REPO/tools/ns3-version.txt" | cut -d= -f2)
URL=$(grep '^url=' "$REPO/tools/ns3-version.txt" | cut -d= -f2)
SHA256=$(grep '^sha256=' "$REPO/tools/ns3-version.txt" | cut -d= -f2)

TARBALL="$CACHE/download/ns-$VERSION.tar.bz2"
SRC="$CACHE/ns-$VERSION/src"
BUILD="$CACHE/ns-$VERSION/build"
# ns-3 writes headers and libraries under its source tree's build/ folder.
NS3_OUT="$SRC/build"

say() { printf '\n== %s\n' "$*"; }
fail() { printf '\nsetup failed: %s\n' "$*" >&2; exit 1; }

# Parallel compile jobs. Each ns-3 job can take 600 MB of memory, so cap it.
if command -v nproc >/dev/null; then
    JOBS=$(nproc)
else
    JOBS=$(sysctl -n hw.ncpu)
fi
if [ "$JOBS" -gt 6 ]; then
    JOBS=6
fi

# Ninja if it is installed, otherwise make. Either works.
GENERATOR=()
if command -v ninja >/dev/null; then
    GENERATOR=(-G Ninja)
fi

say "checking tools"
command -v cmake >/dev/null || fail "cmake is missing. see README.md, Setup"
# ns-3.48 needs CMake 3.25 or newer. Ubuntu 22.04 has 3.22; pip has a newer one.
CMAKE_VERSION=$(cmake --version | head -1 | awk '{print $3}')
if [ "$(printf '%s\n3.25\n' "$CMAKE_VERSION" | sort -V | head -1)" != "3.25" ]; then
    fail "cmake $CMAKE_VERSION is too old, ns-3 needs 3.25 or newer. try: uv pip install cmake"
fi
command -v c++ >/dev/null || fail "no C++ compiler. see README.md, Setup"
# A compiler that cannot find its own standard library headers makes ns-3's
# configure fail with a misleading "clang version is not compatible" error.
# Stale Command Line Tools on macOS do this; a fresh install fixes it.
echo '#include <iostream>' | c++ -std=c++20 -x c++ -c - -o /dev/null 2>/dev/null \
    || fail "c++ cannot compile a program that includes <iostream>. on macOS: sudo rm -rf /Library/Developer/CommandLineTools && xcode-select --install"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 10)' \
    || fail "$PYTHON is older than 3.10"
INCLUDE=$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["include"])')
[ -f "$INCLUDE/Python.h" ] \
    || fail "no Python headers in $INCLUDE. on Ubuntu or WSL: sudo apt install python3-dev"
"$PYTHON" -c 'import yaml' 2>/dev/null \
    || fail "$PYTHON has no yaml module. install it: uv pip install pyyaml"
echo "python  $("$PYTHON" -c 'import sys; print(sys.version.split()[0])') ($(command -v "$PYTHON"))"
echo "cmake   $(cmake --version | head -1)"
echo "jobs    $JOBS"

# 1. Download the release once, and check it is the file we pinned.
if [ ! -f "$TARBALL" ]; then
    say "downloading ns-$VERSION"
    mkdir -p "$CACHE/download"
    curl -fL --retry 3 -o "$TARBALL.part" "$URL"
    mv "$TARBALL.part" "$TARBALL"
fi
if command -v sha256sum >/dev/null; then
    GOT=$(sha256sum "$TARBALL" | cut -d' ' -f1)
else
    GOT=$(shasum -a 256 "$TARBALL" | cut -d' ' -f1)
fi
if [ "$GOT" != "$SHA256" ]; then
    rm -f "$TARBALL"
    fail "checksum mismatch for $TARBALL (got $GOT). deleted it; run make setup again"
fi

# 2. Unpack and build ns-3, only the modules the harness uses.
if [ ! -f "$NS3_OUT/include/ns3/core-module.h" ] || ! ls "$NS3_OUT"/lib/libns"$VERSION"-monolib.* >/dev/null 2>&1; then
    say "building ns-$VERSION (a few minutes, once)"
    rm -rf "$CACHE/ns-$VERSION"
    mkdir -p "$CACHE/ns-$VERSION"
    tar -xjf "$TARBALL" -C "$CACHE/ns-$VERSION"
    mv "$CACHE/ns-$VERSION/ns-$VERSION" "$SRC"

    # Release build without -march=native, and without fused multiply-add
    # (-ffp-contract=off), so every machine computes the same numbers.
    # INT128 time arithmetic for the same reason. One shared library.
    cmake -S "$SRC" -B "$BUILD" ${GENERATOR[@]+"${GENERATOR[@]}"} \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_CXX_FLAGS="-ffp-contract=off" \
        -DNS3_ENABLED_MODULES="internet;point-to-point" \
        -DNS3_INT64X64=INT128 \
        -DNS3_NATIVE_OPTIMIZATIONS=OFF \
        -DNS3_MONOLIB=ON \
        -DNS3_EXAMPLES=OFF -DNS3_TESTS=OFF -DNS3_PYTHON_BINDINGS=OFF \
        -DNS3_MPI=OFF -DNS3_GTK3=OFF -DNS3_SQLITE=OFF -DNS3_EIGEN=OFF -DNS3_GSL=OFF \
        -DNS3_EMU=OFF -DNS3_TAP=OFF -DNS3_VISUALIZER=OFF \
        -DNS3_CCACHE=OFF -DNS3_WARNINGS_AS_ERRORS=OFF \
        > "$CACHE/ns-$VERSION/configure.log" 2>&1 \
        || fail "ns-3 configure failed, see $CACHE/ns-$VERSION/configure.log"
    cmake --build "$BUILD" -j "$JOBS" > "$CACHE/ns-$VERSION/build.log" 2>&1 \
        || fail "ns-3 build failed, see the end of $CACHE/ns-$VERSION/build.log"
fi
echo "ns-3    $NS3_OUT"
if [ "$NS3_ONLY" -eq 1 ]; then
    exit 0
fi

# 3. Build transportlab/_tlsim for this python, in this clone's build/
# folder. ns-3 is shared between clones; this part isn't. A different python
# version needs its own build, so the folder is named after it.
PYTAG=$("$PYTHON" -c 'import sys; print(f"py{sys.version_info[0]}{sys.version_info[1]}")')
say "building the transportlab ns-3 backend for $PYTAG"
TLSIM_BUILD="$REPO/build/tlsim-$VERSION-$PYTAG"
mkdir -p "$REPO/build"
cmake -S "$REPO/sim" -B "$TLSIM_BUILD" ${GENERATOR[@]+"${GENERATOR[@]}"} \
    -DPython_EXECUTABLE="$(command -v "$PYTHON")" \
    -DNS3_BUILD="$NS3_OUT" -DNS3_VERSION="$VERSION" \
    > "$REPO/build/configure.log" 2>&1 \
    || fail "configure failed, see build/configure.log"
cmake --build "$TLSIM_BUILD" -j "$JOBS" > "$REPO/build/build.log" 2>&1 \
    || fail "build failed, see build/build.log"

# 4. Check it imports.
cd "$REPO"
"$PYTHON" -c 'from transportlab import _tlsim; print("backend", _tlsim.__file__)' \
    || fail "transportlab/_tlsim does not import"
say "done. next: make doctor"

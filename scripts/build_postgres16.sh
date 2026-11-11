#!/usr/bin/env bash
# Optional unprivileged source installation when Docker/PG16 packages are absent.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
VERSION=16.15
SHA256=4f200ca23dfb120ff9838f13ce06014aad1d3c432d16ee9f93ab2000c0eeef7b
BUILD="$ROOT/.local/pg-build"
PREFIX="$ROOT/.local/postgresql16"
MAKE=${MAKE:-make}
CC=${CC:-cc}
# This optional cached compiler is detected, not installed or modified.
if ! command -v "$CC" >/dev/null && [[ -x "$HOME/.local/bin/dbagent-gcc" ]]; then
  CC="$HOME/.local/bin/dbagent-gcc"
fi
if ! command -v "$MAKE" >/dev/null && [[ -x "$HOME/.local/share/dbagent-toolchain/usr/bin/make" ]]; then
  MAKE="$HOME/.local/share/dbagent-toolchain/usr/bin/make"
fi
mkdir -p "$BUILD" "$ROOT/artifacts/environment"
if [[ ! -f "$BUILD/postgresql-$VERSION.tar.gz" ]]; then
  curl -fL --retry 0 --max-time 180 -o "$BUILD/postgresql-$VERSION.tar.gz" \
    "https://ftp.postgresql.org/pub/source/v$VERSION/postgresql-$VERSION.tar.gz"
fi
printf '%s  %s\n' "$SHA256" "$BUILD/postgresql-$VERSION.tar.gz" | sha256sum -c -
if [[ ! -d "$BUILD/postgresql-$VERSION" ]]; then
  tar -xzf "$BUILD/postgresql-$VERSION.tar.gz" -C "$BUILD"
fi
cd "$BUILD/postgresql-$VERSION"
./configure --prefix="$PREFIX" --without-icu --without-readline --without-zlib \
  CC="$CC" CFLAGS='-O2 -std=gnu17' > "$ROOT/artifacts/environment/postgresql-configure.log" 2>&1
"$MAKE" -j"${AGENT_BUILD_JOBS:-4}" > "$ROOT/artifacts/environment/postgresql-build.log" 2>&1
"$MAKE" install > "$ROOT/artifacts/environment/postgresql-install.log" 2>&1
"$PREFIX/bin/postgres" --version

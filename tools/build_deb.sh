#!/bin/sh
# Build a Debian package: tools/build_deb.sh [output dir]  ->  opencdi_<version>_all.deb
#
# The package installs the emulator only.  No BIOS is included; users select
# their own CD-i system ROM in the launcher.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:-$ROOT/dist}
PKG=$ROOT/packaging/debian
VERSION=$(sed -n "s/^__version__ = '\(.*\)'/\1/p" "$ROOT/opencdi/__init__.py")
[ -n "$VERSION" ] || { echo "cannot read __version__ from opencdi/__init__.py" >&2; exit 1; }
MAINTAINER=${DEBFULLNAME:-$(git -C "$ROOT" config user.name || echo OpenCDi)}
EMAIL=${DEBEMAIL:-$(git -C "$ROOT" config user.email || echo opencdi@localhost)}

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
D=$STAGE/opencdi

# program files
SITE=$D/usr/lib/python3/dist-packages
mkdir -p "$SITE"
cp -r "$ROOT/opencdi" "$SITE/"
find "$SITE" -name __pycache__ -prune -exec rm -rf {} +
find "$SITE" -name '*.pyc' -delete
install -D -m 755 "$PKG/opencdi.sh" "$D/usr/bin/opencdi"
install -D -m 644 "$PKG/opencdi.desktop" "$D/usr/share/applications/opencdi.desktop"
for icon in "$PKG"/icons/*/opencdi.png; do
    install -D -m 644 "$icon" "$D/usr/share/icons/hicolor/$(basename "$(dirname "$icon")")/apps/opencdi.png"
done

# documentation
DOC=$D/usr/share/doc/opencdi
install -D -m 644 "$ROOT/README.md" "$DOC/README.md"
cat > "$DOC/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: OpenCDi
Comment: No CD-i BIOS/system ROM is included. Mono-I ROMs (cdi220b.rom,
 cdi200.rom) can be downloaded from
 https://archive.org/download/philips-cd-i-misc-bios

Files: *
Copyright: OpenCDi contributors
License: GPL-3+
 On Debian systems the full text of the GNU General Public License version 3
 can be found in /usr/share/common-licenses/GPL-3.
EOF
printf 'opencdi (%s) unstable; urgency=low\n\n  * Release %s.\n\n -- %s <%s>  %s\n' \
    "$VERSION" "$VERSION" "$MAINTAINER" "$EMAIL" "$(date -R)" | gzip -9n > "$DOC/changelog.Debian.gz"
chmod 644 "$DOC/changelog.Debian.gz"

# control files
mkdir -p "$D/DEBIAN"
SIZE=$(du -sk --exclude=DEBIAN "$D" | cut -f1)
cat > "$D/DEBIAN/control" <<EOF
Package: opencdi
Version: $VERSION
Architecture: all
Maintainer: $MAINTAINER <$EMAIL>
Installed-Size: $SIZE
Depends: python3 (>= 3.9), python3-numpy (>= 1:1.21), python3-pygame (>= 2.1)
Section: games
Priority: optional
Description: Philips CD-i (Mono-I) emulator
 OpenCDi emulates the Philips CD-i Mono-I player (SCC68070 CPU, MCD212 video,
 CDIC CD controller) and runs .cue/.bin and .iso disc images. Keyboard, mouse
 and any SDL-supported game controller can drive the CD-i pointer.
 .
 A CD-i system ROM (BIOS) is required and is not included; select it in the
 launcher after installation.
EOF
cat > "$D/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if command -v py3compile >/dev/null 2>&1; then
    py3compile -p opencdi
fi
if [ "$1" = configure ] && command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
fi
EOF
cat > "$D/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if command -v py3clean >/dev/null 2>&1; then
    py3clean -p opencdi
else
    find /usr/lib/python3/dist-packages/opencdi -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
fi
EOF
chmod 755 "$D/DEBIAN/postinst" "$D/DEBIAN/prerm"

find "$D" -path "$D/DEBIAN" -prune -o -type d -exec chmod 755 {} +
find "$SITE" -type f -exec chmod 644 {} +

mkdir -p "$OUT"
DEB=$OUT/opencdi_${VERSION}_all.deb
dpkg-deb --root-owner-group -Zxz --build "$D" "$DEB"
echo "built $DEB"

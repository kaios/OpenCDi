# Android build of OpenCDi.  Use tools/build_apk.sh, which stages the sources
# into android/build/src (only the opencdi package and main.py, never a BIOS),
# cross-compiles the native CPU core and then runs buildozer with this file.

[app]
title = OpenCDi
package.name = opencdi
package.domain = org.opencdi
source.dir = build/src
source.include_exts = py,png,bin,so,c,json
version.regex = __version__ = '(.*)'
version.filename = %(source.dir)s/opencdi/__init__.py
requirements = python3,pygame,numpy,pyjnius,android
icon.filename = %(source.dir)s/opencdi/data/opencdi.png
orientation = landscape
fullscreen = 1

# Android 10 (API 29) and later, 64-bit ARM
android.minapi = 29
android.api = 33
android.ndk_api = 29
android.archs = arm64-v8a
android.accept_sdk_license = True
android.wakelock = True
# disc images and the BIOS are read from shared storage: on Android 10 with
# the legacy storage flag, on Android 11+ with "all files access"
android.permissions = READ_EXTERNAL_STORAGE, WRITE_EXTERNAL_STORAGE, MANAGE_EXTERNAL_STORAGE
android.extra_manifest_application_arguments = manifest_app.xml
android.allow_backup = False
p4a.bootstrap = sdl2
# pygame 2.6.1 instead of python-for-android's 2.1.0 (see recipes/pygame)
p4a.local_recipes = recipes
# keep the .py files: the prebuilt native core is matched to m68k.py by hash
android.no-byte-compile-python = True

[buildozer]
log_level = 2
warn_on_root = 1

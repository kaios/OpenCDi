# OpenCDi for Android

An Android 10+ (API 29) app for 64-bit ARM phones and tablets, built from the
same sources as the desktop version with python-for-android.

## Installing

Copy `opencdi-<version>-arm64-v8a-debug.apk` to the phone and open it (allow
installing from that source when Android asks), or with USB debugging on:

```sh
adb install -r dist/opencdi-0.1.6-arm64-v8a-debug.apk
```

On the first start Android asks for **access to all files** (Android 11 and
later) or **storage** (Android 10). Allow it, then go back to the app. That lets
the launcher open the BIOS and disc images wherever you keep them, for example
in `Download/`. Without it, only the app's own folder
`Android/data/org.opencdi.opencdi/files/` can be read.

No BIOS is included. Copy `cdi220b.rom` (see the main README) to the phone
and pick it under **BIOS** in the launcher. Discs can be `.cue`/`.bin` or `.chd`;
`.chd` takes much less space on the phone. Zstandard-compressed CHDs (`cdzs`)
do not open in the app (its Python has no zstd module); the default `chdman`
codecs do.

## Controls

| | Touch |
|---|---|
| Move the pointer | drag anywhere outside the buttons (like a laptop touchpad) |
| Button 1 | tap, or the **1** button (hold it to drag things) |
| Button 2 | the **2** button |
| Both buttons | hold **1** and **2** together |
| Launcher menu | **MENU**, or the Android back button |
| Save / load state | **MENU**, then **Save state** or **Load state** |

In the launcher: tap an item to choose it; in the file browser tap a file twice
to open it, and drag the list to scroll.  In the save state screen tap a slot to
see its picture, and tap it again to save or load.  States are kept in the app's
private folder (`files/.opencdi/states/`), so uninstalling the app deletes them. Game controllers (Bluetooth or USB) and
keyboards work as on the desktop.

The picture fills the screen height. On phones the buttons sit in the black
margins beside it; on tablets with a squarer screen they are drawn see-through
over the picture. The app defaults to a 60 Hz NTSC player; PAL pictures are
scaled down to fit.

## Building

On Debian/Ubuntu (tested on Debian 12):

```sh
sudo apt install git zip unzip openjdk-17-jdk-headless autoconf automake libtool \
    pkg-config cmake zlib1g-dev libffi-dev libssl-dev libncurses-dev lld python3-venv
python3 -m venv ~/.venvs/opencdi-android
~/.venvs/opencdi-android/bin/pip install buildozer "cython<3.1"
tools/build_apk.sh            # -> dist/opencdi-<version>-arm64-v8a-debug.apk
```

The first build downloads the Android SDK and NDK (about 5 GB, into
`~/.buildozer`) and builds Python, SDL2, pygame and numpy for ARM64. That takes
a long time. Later builds only repackage the app.

`tools/build_apk.sh` stages only the `opencdi` package and `android/main.py`
into `android/build/src`, so no BIOS or other files from the source folder
end up in the app. It cross-compiles the native CPU core with the NDK's clang
(`opencdi/native`, see `opencdi/m68k_native.py`), records it in
`prebuilt.json` so the phone never needs a compiler, and runs buildozer with
`android/buildozer.spec`.

## Logs

The debug log is in the app's private folder. With USB debugging:

```sh
adb logcat -s python:* SDL:*                   # Python output and errors
adb shell run-as org.opencdi.opencdi cat files/.opencdi/debug.log
```

`run-as` works with the debug build. `native CPU core loaded from ...` near
the top of the log means the fast core is running.

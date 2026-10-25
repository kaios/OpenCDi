# OpenCDi

Stand-alone open source Philips CD-i emulator, written in Python with a native (C) CPU core.

It emulates the CD-i **Mono-I** board (Philips CD-i 220 F2 / Magnavox CD-i 200):

| Part | Chip | Module |
|---|---|---|
| CPU | Philips SCC68070 (68000 core, 15 MHz) + on-chip timer, UART, interrupt controller, DMA | `m68k.py`, `m68k_native.py`, `native/core.c`, `scc68070.py` |
| Video | MCD212 VDSC: ICA/DCA display programs, CLUT8/7/4, RL7/RL3 run-length, DYUV, RGB555, mosaic, mattes, transparency, plane mixing, hardware cursor, interlace | `mcd212.py`, `native/video.c` |
| CD drive | CDIC: Mode 1/Mode 2 sector reads, TOC, CD-DA playback, CD-ROM XA ADPCM audio (4/8-bit, 37.8/18.9 kHz, mono/stereo) | `cdic.py` |
| Input/system | SLAVE controller (pointer device, audio attenuation, front panel) | `slave.py` |
| NVRAM | MK48T08 timekeeper (saved between runs) | `nvram.py` |

## Architecture

```
 main thread                         emulation thread                 audio-feeder thread
 -----------                         ----------------                 -------------------
 window events, launcher             Machine.run_frame() at 60.05 Hz  tops up the sound card
 newest picture -> window   <------  (NTSC) / 50.08 Hz (PAL), paced   from the audio buffer
 (unchanged pictures not sent)       to the sound card's clock  ----> every 5 ms
```

* **CPU** (`m68k.py`): the 68000 instruction set is described once, as Python templates
  generated per opcode.  `m68k_native.py` translates those templates to C, and
  `native/core.c` adds the run loop, exceptions and memory access.  The result is compiled
  into `opencdi/native/_m68k_<hash>.so` and loaded with `ctypes`, which releases the GIL
  while the CPU runs.  RAM is shared with the Python devices without copying; device
  registers are reached through callbacks into Python.  If no C compiler is available, or
  with `OPENCDI_CPU=python`, the same templates run as a Python core.  Both cores give
  identical results.
* **Video** (`mcd212.py`): display programs and line timing are Python; pixels are made once
  per frame with numpy, and DYUV decoding and plane mixing use C (`native/video.c`) when
  the native library is loaded.  Video lines run on a fixed 960-cycle timeline.
* **Devices** (`cdic.py`, `slave.py`, `scc68070.py`, `nvram.py`) are Python, driven by a
  cycle-based scheduler (`sched.py`).
* **Frontend** (`frontend.py`, `launcher.py`, `inputs.py`): pygame window, launcher, input
  and the audio stream (a ~150 ms buffer with seamless resampling, `audio.py`).  On Android
  `touch.py` adds the touch controls and `android_support.py` the storage access.

## Requirements

* Python 3.9+
* `pip install -r requirements.txt` (numpy, pygame 2)
* A C compiler (`gcc`; on Debian/Ubuntu `sudo apt install build-essential`) when running
  from source.  The native core is compiled once, on first start (about 15 s), into
  `opencdi/native/` or, if that is not writable, `~/.cache/opencdi/`, and rebuilt
  automatically when the CPU code changes.  Without a compiler the emulator uses its
  Python CPU core, which is several times slower (`debug.log` says which core runs).
  No Python headers are needed; the library is plain C.  The `.deb` already contains the
  compiled core, so installing it needs no compiler.
* **A CD-i system ROM (BIOS) is required.** It is not included with the emulator. Download it from
  <https://archive.org/download/philips-cd-i-misc-bios>, save it anywhere, and select it in the
  launcher (or pass `--bios PATH`).
  The Mono-I ROMs from that set work:
  `cdi220b.rom` (Philips CD-i 220 F2, recommended) or `cdi200.rom` (Magnavox CD-i 200).
  `cdi220.rom` is detected but does not boot (MAME reports the same).
  `cdi490a.rom` is a Mono-IV board ROM, which is different hardware and not supported.

## Installing on Debian / Ubuntu

Build a `.deb` package and install it (this pulls in `python3-numpy` and `python3-pygame`):

```sh
tools/build_deb.sh                       # writes dist/opencdi_<version>_<arch>.deb
sudo apt install ./dist/opencdi_*.deb
```

Since 0.1.3 the package is built for the architecture of the machine that builds it
(`opencdi_0.1.3_amd64.deb` on a PC) instead of `_all`, because it contains the compiled
CPU core.  Building it needs `gcc`.  To install on another architecture, run
`tools/build_deb.sh` there.

This installs the `opencdi` command and an "OpenCDi" entry in the desktop's Games menu.
The package does not contain a BIOS; pick one in the launcher on first start.
Remove it with `sudo apt remove opencdi`.

## Android

An Android 10+ app for 64-bit ARM phones and tablets is built with `tools/build_apk.sh`
(`dist/opencdi-<version>-arm64-v8a-debug.apk`).  It runs the same code with touch controls
(drag to move the pointer, tap to click, on-screen 1 / 2 / MENU buttons) and the native
CPU core compiled for ARM.  See [android/README.md](android/README.md) for installing,
controls and building.

## Running

```sh
python -m opencdi
```

This opens the launcher:

* **BIOS** opens a file browser to pick the CD-i system ROM you downloaded
  (`cdi220b.rom` or `cdi200.rom`).  The file is checked straight away and the
  launcher tells you whether it will work.  No BIOS ships with the emulator.
* **Disc** picks a `.cue`, `.iso`, `.bin` or `.img` image (optional; without a disc the
  CD-i player menu starts).  **Eject disc** removes it.
* **Video** switches between an NTSC (60 Hz, the default) and a PAL (50 Hz) player.
* **Start** boots the machine.

The launcher works with the mouse, the keyboard or a game controller.  Your choices are
saved in `~/.opencdi/config.json`, so the next start has them selected already.  While a
game is running, press **Esc**, **F2** or the controller's **Back/Select** button to return
to the launcher. From there you can resume, change the BIOS or disc and restart, or quit.

To skip the launcher, pass the BIOS on the command line:

```sh
python -m opencdi game.cue --bios /path/to/cdi220b.rom
python -m opencdi game.cue        # opens the launcher with this disc already selected
```

Options: `--bios PATH` (start immediately), `--ntsc` / `--pal` (60 or 50 Hz player),
`--scale N` (window size as a multiple of the picture), `--fullscreen`,
`--no-audio`, `--serial` (print the 68070 serial console; lines typed on stdin are sent to it),
`--debug` (detailed debug log), `--headless FRAMES --screenshot out.png` (run without a window;
needs `--bios`).

A debug log is written to `~/.opencdi/debug.log` on every run. Emulator faults are always
logged there; `--debug` adds per-event detail. What each message means, and every change
made in each release, is described in `docs/debug/`.

Disc images: `.cue/.bin` (MODE1/2048, MODE1/2352, MODE2/2352, MODE2/2336, CDI/2352, CDI/2336, AUDIO; multi-track and
multi-file), `.iso`/`.bin`/`.img` single files.  Raw 2352-byte sectors are best because they
keep the CD-i subheaders; 2048-byte "cooked" ISOs get a synthesized data subheader, which is
enough for the file system but loses audio/video interleaving information.

## Controls

The CD-i uses a pointing device with two buttons; everything is mapped onto it.

| | Keyboard | Game controller (any SDL-supported pad) | Mouse |
|---|---|---|---|
| Move pointer | arrows / WASD | left or right stick, d-pad | move (click window to capture) |
| Button 1 | Z, Space, J | A / Cross, Y / Triangle | left button |
| Button 2 | X, K | B / Circle, X / Square | right button |
| Both buttons | C, L | Start | middle button |
| Fast pointer | Shift | shoulder buttons / triggers | |

Controllers are hot-pluggable. Pads in SDL's controller database (Xbox, PlayStation,
Switch, 8BitDo, most USB pads) use the standard layout; anything else falls back to raw
axes, hat and buttons 0-3.

Hotkeys: Esc/F2 launcher menu, F1 help, F3 reset, F10 release mouse, F11 fullscreen,
F12 screenshot, P pause, hold Tab to fast-forward.

## Status and performance

Verified: the player shell boots and is fully usable (menus, settings, info), audio CDs are
recognised and play, and CD-i discs are recognised and their application loads and runs
(tested with the LGPL homebrew *CD-i Stub*); *Alice in Wonderland* boots and plays its
intro and first scenes.  Both CPU cores pass the SingleStepTests 68000 suite
(`tools/run_sst.py`) apart from address-error cases it skips.
`tools/check_fixes.py` runs regression checks for the fixes since 0.1.1 without a BIOS.

Speed: with the native core, the player shell and *Alice in Wonderland* run at full
speed (60 fps).  On a 2-core machine, Alice's animated scenes take 6.5-7.4 ms per frame
headless, against a 16.7 ms budget; the Python core needs about 36 ms.  A picture that
has not changed is reused, and when the host falls behind, the picture of a frame is
skipped (never the emulation) so the sound keeps playing.  Over SSH X forwarding every
window pixel crosses the network each frame: keep `--scale 1` and don't go fullscreen
there.  Only one commercial title has been tried, so expect bugs in others.
Digital Video (MPEG) cartridge titles are not supported.

## Credits

The hardware behaviour is ported from MAME's CD-i driver (`cdi.cpp`, `mcd212.cpp`,
`cdicdic.cpp`, `cdislavehle.cpp`, `scc68070.cpp`; BSD-3-Clause, Ryan Holtz, Vincent Halver
and the MAME team) and its Musashi 68000 core; `opencdi/data/cycles070.bin` is the 68070
cycle table extracted from MAME by `tools/extract_cycles.py`.  BCD flag behaviour follows
flamewing's 68000 research.

License: GPL-3.0 (see LICENSE).

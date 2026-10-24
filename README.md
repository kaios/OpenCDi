# OpenCDi

Stand-alone open source Philips CD-i emulator, written in Python.

It emulates the CD-i **Mono-I** board (Philips CD-i 220 F2 / Magnavox CD-i 200):

| Part | Chip | Module |
|---|---|---|
| CPU | Philips SCC68070 (68000 core, 15 MHz) + on-chip timer, UART, interrupt controller, DMA | `m68k.py`, `scc68070.py` |
| Video | MCD212 VDSC: ICA/DCA display programs, CLUT8/7/4, RL7/RL3 run-length, DYUV, RGB555, mosaic, mattes, transparency, plane mixing, hardware cursor, interlace | `mcd212.py` |
| CD drive | CDIC: Mode 1/Mode 2 sector reads, TOC, CD-DA playback, CD-ROM XA ADPCM audio (4/8-bit, 37.8/18.9 kHz, mono/stereo) | `cdic.py` |
| Input/system | SLAVE controller (pointer device, audio attenuation, front panel) | `slave.py` |
| NVRAM | MK48T08 timekeeper (saved between runs) | `nvram.py` |

## Requirements

* Python 3.9+
* `pip install -r requirements.txt` (numpy, pygame 2)
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
tools/build_deb.sh                       # writes dist/opencdi_<version>_all.deb
sudo apt install ./dist/opencdi_*_all.deb
```

This installs the `opencdi` command and an "OpenCDi" entry in the desktop's Games menu.
The package does not contain a BIOS; pick one in the launcher on first start.
Remove it with `sudo apt remove opencdi`.

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
* **Video** switches between a PAL (50 Hz) and NTSC (60 Hz) player.
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

Options: `--bios PATH` (start immediately), `--ntsc`, `--scale N`, `--fullscreen`,
`--no-audio`, `--serial` (print the 68070 serial console),
`--headless FRAMES --screenshot out.png` (run without a window; needs `--bios`).

Disc images: `.cue/.bin` (MODE1/2352, MODE2/2352, MODE2/2336, AUDIO; multi-track and
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
(tested with the LGPL homebrew *CD-i Stub*).  The CPU core passes the SingleStepTests
68000 suite (`tools/run_sst.py`) apart from address-error cases it skips.

Speed: everything is pure Python plus numpy.  The CPU core is generated per opcode at
start-up, and pixels are produced once per frame with numpy.  On a typical desktop the
player shell runs at full speed, and CPU-heavy code runs at roughly 70-100 % of real
hardware speed.  It has not been tested with commercial titles, so expect bugs there.
Digital Video (MPEG) cartridge titles are not supported.

## Credits

The hardware behaviour is ported from MAME's CD-i driver (`cdi.cpp`, `mcd212.cpp`,
`cdicdic.cpp`, `cdislavehle.cpp`, `scc68070.cpp`; BSD-3-Clause, Ryan Holtz, Vincent Halver
and the MAME team) and its Musashi 68000 core; `opencdi/data/cycles070.bin` is the 68070
cycle table extracted from MAME by `tools/extract_cycles.py`.  BCD flag behaviour follows
flamewing's 68000 research.

License: GPL-3.0 (see LICENSE).

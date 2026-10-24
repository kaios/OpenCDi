"""Command line entry point: python -m opencdi [disc image] [options]

Without --bios the launcher opens so the BIOS (CD-i system ROM) and disc can
be picked in the UI.  No BIOS is bundled; the choice is remembered in
~/.opencdi/config.json.
"""

import argparse
import sys
import time
from pathlib import Path

BIOS_URL = 'https://archive.org/download/philips-cd-i-misc-bios'


def build_machine(bios, disc_path, ntsc, log):
    from .disc import Disc
    from .machine import Machine
    disc = Disc(disc_path) if disc_path else None
    if disc:
        print(f'disc: {disc_path}: {disc.describe()}')
    nvram = Path.home() / '.opencdi' / f'{Path(bios).stem}.nvram'
    m = Machine(bios, disc=disc, nvram_path=nvram, ntsc=ntsc, log=log)
    print(f'BIOS: {bios} ({m.rom_name})')
    return m


def main(argv=None):
    ap = argparse.ArgumentParser(prog='opencdi', description='Philips CD-i (Mono-I) emulator')
    ap.add_argument('disc', nargs='?', help='disc image (.cue/.bin or .iso)')
    ap.add_argument('--bios', help='CD-i system ROM; when given, starts immediately instead of showing the launcher')
    ap.add_argument('--ntsc', action='store_true', help='report an NTSC player (60 Hz) instead of PAL')
    ap.add_argument('--scale', type=float, default=1.0, help='window scale factor (default 1 = 768x560)')
    ap.add_argument('--fullscreen', action='store_true')
    ap.add_argument('--no-audio', action='store_true')
    ap.add_argument('--serial', action='store_true', help='print the 68070 serial port output')
    ap.add_argument('--headless', type=int, metavar='FRAMES', help='run FRAMES frames without a window (needs --bios)')
    ap.add_argument('--screenshot', help='with --headless: save the last frame to this PNG')
    args = ap.parse_args(argv)

    from .machine import check_bios

    def log(text):
        if args.serial:
            print(text, flush=True)

    if args.bios:
        ok, info = check_bios(args.bios)
        if not ok:
            sys.exit(f'{args.bios}: {info}\nMono-I ROMs (cdi220b.rom, cdi200.rom) can be downloaded from {BIOS_URL}')
    if args.disc and not Path(args.disc).exists():
        sys.exit(f'disc image not found: {args.disc}')

    if args.headless:
        if not args.bios:
            sys.exit(f'--headless needs --bios PATH (download a BIOS from {BIOS_URL})')
        m = build_machine(args.bios, args.disc, args.ntsc, log)
        m.audio_enabled = False
        t0 = time.time()
        for _ in range(args.headless):
            m.run_frame()
        dt = time.time() - t0
        print(f'{args.headless} frames in {dt:.1f}s ({args.headless / dt:.1f} fps)')
        if args.screenshot and m.frame is not None:
            import os
            os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
            from .frontend import save_png
            save_png(m.frame, args.screenshot)
            print(f'saved {args.screenshot}')
        m.save()
        return

    from .frontend import HELP, App, Emulator
    from .launcher import Launcher, load_config, save_config

    cfg = load_config()
    if args.bios:
        cfg['bios'] = str(Path(args.bios).resolve())
    if args.disc:
        cfg['disc'] = str(Path(args.disc).resolve())
    if args.ntsc:
        cfg['ntsc'] = True
    save_config(cfg)

    app = App(scale=args.scale, fullscreen=args.fullscreen, audio=not args.no_audio)
    print(HELP)
    machine = None
    emulator = None
    choice = ('start', cfg['bios'], cfg.get('disc'), bool(cfg.get('ntsc'))) if args.bios else None
    message = ''
    try:
        while True:
            if choice is None:
                choice = Launcher(app, cfg, can_resume=machine is not None, message=message).run()
                message = ''
            if choice[0] == 'quit':
                break
            if choice[0] == 'start':
                _, bios, disc, ntsc = choice
                if machine is not None:
                    machine.save()
                try:
                    machine = build_machine(bios, disc, ntsc, log)
                except Exception as e:  # noqa: BLE001
                    message = f'Could not start: {e}'
                    print(message)
                    machine = None
                    choice = None
                    continue
                title = 'OpenCDi' + (f' - {Path(disc).name}' if disc else '')
                emulator = Emulator(app, machine, title)
            if choice[0] in ('start', 'resume') and emulator is not None:
                if emulator.run() == 'quit':
                    break
            choice = None
    finally:
        if machine is not None:
            machine.save()
        app.quit()


if __name__ == '__main__':
    main()

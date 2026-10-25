"""Command line entry point: python -m opencdi [disc image] [options]

Without --bios the launcher opens so the BIOS (CD-i system ROM) and disc can
be picked in the UI.  No BIOS is bundled; the choice is remembered in
~/.opencdi/config.json.  A debug log is written to ~/.opencdi/debug.log.
"""

import argparse
import os
import queue
import sys
import threading
import time
from pathlib import Path

BIOS_URL = 'https://archive.org/download/philips-cd-i-misc-bios'


def build_machine(bios, disc_path, ntsc, log, serial_input=None):
    from .disc import Disc
    from .machine import Machine
    disc = Disc(disc_path) if disc_path else None
    if disc:
        print(f'disc: {disc_path}: {disc.describe()}')
    nvram = Path.home() / '.opencdi' / f'{Path(bios).stem}.nvram'
    try:
        m = Machine(bios, disc=disc, nvram_path=nvram, ntsc=ntsc, log=log)
    except Exception:
        if disc:
            disc.close()
        raise
    if serial_input is not None:
        m.serial_input = serial_input
    print(f'BIOS: {bios} ({m.rom_name})')
    return m


def start_serial_reader():
    """Read lines typed on stdin and queue them for the 68070 UART (OS-9 console)."""
    q = queue.SimpleQueue()

    def reader():
        # os.read takes no Python-level lock, so a thread still blocked here
        # cannot stall interpreter shutdown (sys.stdin.buffer would)
        try:
            fd = sys.stdin.fileno()
        except (AttributeError, OSError, ValueError):
            return
        while True:
            try:
                data = os.read(fd, 4096)
            except OSError:
                return
            if not data:
                return
            q.put(data.replace(b'\r\n', b'\r').replace(b'\n', b'\r'))

    threading.Thread(target=reader, name='serial-stdin', daemon=True).start()
    return q


def main(argv=None):
    ap = argparse.ArgumentParser(prog='opencdi', description='Philips CD-i (Mono-I) emulator')
    ap.add_argument('disc', nargs='?', help='disc image (.cue/.bin, .chd or .iso)')
    ap.add_argument('--bios', help='CD-i system ROM; when given, starts immediately instead of showing the launcher')
    region = ap.add_mutually_exclusive_group()
    region.add_argument('--ntsc', action='store_true', help='NTSC player, 60 Hz video (the default)')
    region.add_argument('--pal', action='store_true', help='PAL player, 50 Hz video')
    ap.add_argument('--scale', type=float, default=1.0,
                    help='window size as a multiple of the picture (default 1 = 768x480 NTSC, 768x560 PAL)')
    ap.add_argument('--fullscreen', action='store_true')
    ap.add_argument('--no-audio', action='store_true')
    ap.add_argument('--serial', action='store_true',
                    help='print the 68070 serial port output; lines typed on stdin are sent to it')
    ap.add_argument('--debug', action='store_true', help='write detailed messages to ~/.opencdi/debug.log')
    ap.add_argument('--headless', type=int, metavar='FRAMES', help='run FRAMES frames without a window (needs --bios)')
    ap.add_argument('--screenshot', help='with --headless: save the last frame to this PNG')
    ap.add_argument('--load-state', metavar='FILE', help='with --headless: start from this save state (a slotN.state file)')
    ap.add_argument('--save-state', metavar='FILE', help='with --headless: save the state at the end to FILE')
    args = ap.parse_args(argv)

    from . import debuglog
    from .machine import check_bios

    log_path = debuglog.setup(debug=args.debug)
    if args.debug and log_path:
        print(f'debug log: {log_path}')
    serial_input = start_serial_reader() if args.serial else None

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
        m = build_machine(args.bios, args.disc, not args.pal, log, serial_input)
        m.audio_enabled = False
        if args.load_state:
            from .savestate import StateError, load
            try:
                load(m, args.load_state)
            except StateError as e:
                m.close()
                sys.exit(f'{args.load_state}: {e}')
            print(f'loaded {args.load_state}')
        t0 = time.time()
        for _ in range(args.headless):
            m.run_frame()
        dt = time.time() - t0
        print(f'{args.headless} frames in {dt:.1f}s ({args.headless / dt:.1f} fps)')
        if args.screenshot and m.frame is not None:
            os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
            from .frontend import save_png
            save_png(m.frame, args.screenshot)
            print(f'saved {args.screenshot}')
        if args.save_state:
            from .savestate import save
            save(m, args.save_state)
            print(f'saved {args.save_state}')
        m.close()
        return

    from .frontend import HELP, App, Emulator
    from .launcher import Launcher, load_config, save_config, use_ntsc

    cfg = load_config()
    if args.bios:
        cfg['bios'] = str(Path(args.bios).resolve())
    if args.disc:
        cfg['disc'] = str(Path(args.disc).resolve())
    if args.ntsc or args.pal:
        cfg['ntsc'] = args.ntsc
    save_config(cfg)

    app = App(scale=args.scale, fullscreen=args.fullscreen, audio=not args.no_audio)
    print(HELP)
    machine = None
    emulator = None
    choice = ('start', cfg['bios'], cfg.get('disc'), use_ntsc(cfg)) if args.bios else None
    message = ''
    try:
        while True:
            if choice is None:
                choice = Launcher(app, cfg, can_resume=machine is not None, message=message,
                                  state_id=emulator.disc_id if emulator is not None else None).run()
                message = ''
            if choice[0] == 'quit':
                break
            if choice[0] == 'save':
                if not emulator.state_action('save', choice[1]):
                    message = emulator.osd[0]
                    choice = None
                    continue
                choice = ('resume',)
            load_slot = None
            if choice[0] == 'load':
                _, load_slot, bios, disc, ntsc, into_running = choice
                choice = ('resume',) if into_running else ('start', bios, disc, ntsc)
            if choice[0] == 'start':
                _, bios, disc, ntsc = choice
                if machine is not None:
                    machine.close()
                    machine = None
                try:
                    machine = build_machine(bios, disc, ntsc, log, serial_input)
                except Exception as e:  # noqa: BLE001
                    message = f'Could not start: {e}'
                    print(message)
                    machine = None
                    emulator = None
                    choice = None
                    continue
                title = 'OpenCDi' + (f' - {Path(disc).name}' if disc else '')
                emulator = Emulator(app, machine, title)
            if load_slot is not None and emulator is not None:
                if not emulator.state_action('load', load_slot):
                    message = emulator.osd[0]
                    if choice[0] == 'start':      # nothing was running: do not start a game that was not asked for
                        machine.close()
                        machine = emulator = None
                    choice = None
                    continue
            if choice[0] in ('start', 'resume') and emulator is not None:
                if emulator.run() == 'quit':
                    break
            choice = None
    finally:
        if machine is not None:
            machine.close()
        app.quit()


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Build opencdi/data/cycles070.bin from MAME's generated Musashi opcode table.

Usage: extract_cycles.py path/to/m68kops.cpp

The output is a zlib-compressed array of 65536 bytes holding the SCC68070
cycle count of every opcode (0 marks an opcode the 68070 does not implement).
Cycle data comes from MAME (BSD-3-Clause, Karl Stenerud / MAME developers).
"""
import re
import sys
import zlib
from pathlib import Path

CPU_070 = 1  # column order in the table is '071234fc'


def main():
    src = Path(sys.argv[1]).read_text()
    start = src.index('m68k_opcode_table[] =')
    entry = re.compile(r'\{\s*(0x[0-9a-f]+),\s*(0x[0-9a-f]+),\s*\{([^}]*)\}\}')
    cycles = bytearray(65536)
    for m in entry.finditer(src, start):
        match, mask = int(m.group(1), 16), int(m.group(2), 16)
        if mask == 0:
            break
        c = int(m.group(3).split(',')[CPU_070])
        if c == 255:
            continue
        # enumerate every opcode covered by match/mask, like m68ki_build_opcode_table
        extra = 0
        while True:
            cycles[match | extra] = max(c, 1)
            extra = ((extra | mask) + 1) & ~mask & 0xffff
            if not extra:
                break
    out = Path(__file__).resolve().parent.parent / 'opencdi' / 'data' / 'cycles070.bin'
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(zlib.compress(bytes(cycles), 9))
    print(f'{sum(1 for c in cycles if c)} legal opcodes written to {out}')


if __name__ == '__main__':
    main()

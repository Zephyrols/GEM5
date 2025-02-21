from collections import Counter
import sqlite3 as sql
import argparse
import numpy as np
import subprocess
from tqdm import tqdm


StageNameShort = ['f', 'd', 'r', 'D', 'i', 'a', 'g', 'e', 'b', 'w', 'c']
StageNameLong = ['fetch', 'decode', 'rename', 'dispatch', 'issue',
                 'arb', 'read', 'execute', 'bypass', 'writeback', 'commit']


def DisAssemble(val):
    # print(val)
    hex_val = hex(val).lower()
    command = f'echo "DASM({hex_val})" | spike-dasm'
    asm = subprocess.run(command, shell=True, capture_output=True,
                         text=True, check=True).stdout.strip()
    # print(val, hex_val, asm)
    return asm


def ReadDB(sqldb, start_tick, end_tick, period, inter_gap, inner_gap):
    inst_pos_clock = []
    inst_records = []
    inst_translate_map = {}

    with sql.connect(sqldb) as con:
        cur = con.cursor()

        clock_pick_cmd = f"WHERE AtCommit >= {start_tick} "
        if end_tick >= start_tick:
            clock_pick_cmd += f"AND AtCommit <= {end_tick} "

        cur.execute(
            f"SELECT * FROM LifeTimeCommitTrace {clock_pick_cmd} ORDER BY ID ASC")
        col_name = [i[0].lower() for i in cur.description[1:]]
        rows = cur.fetchall()

    for row in tqdm(rows, desc='Reading DB'):
        row = row[1:]
        pos_clock_cycles = []
        pos_index = 0
        pc = None
        asm = None

        for val in row:
            # print(f"{col_name[pos_index]}, {val}")
            if col_name[pos_index].startswith('at'):
                pos_clock_cycles.append(float(val//period))
            elif col_name[pos_index].startswith('pc'):
                if val < 0:
                    # pc is unsigned, but sqlite3 only supports signed integer [-2^63, 2^63-1]
                    # if real pc > 2^63-1, it will be stored as negative number (real pc - 2^64)
                    # when read a negtive pc, real pc = negtive pc + 2^64
                    val = val + (1 << 64)
                pc = hex(val)
            elif col_name[pos_index].startswith('disasm'):
                if args.platform == 'rtl':
                    if val not in inst_translate_map:
                        inst_translate_map[val] = DisAssemble(val)
                    asm = inst_translate_map[val]
                else:
                    asm = val
            pos_index += 1

        inst_pos_clock.append(pos_clock_cycles)
        inst_records.append(tuple([pc, asm]))

    if inter_gap:
        inst_pos_clock = np.diff(inst_pos_clock, axis=0)
    elif inner_gap:
        inst_pos_clock = np.diff(inst_pos_clock, axis=1)

    inst_records = inst_records[len(inst_records) - len(inst_pos_clock):]

    inst_clock_info: dict = {}
    for inst_index in range(len(inst_records)):
        if inst_records[inst_index] not in inst_clock_info:
            inst_clock_info[inst_records[inst_index]] = []
        inst_clock_info[inst_records[inst_index]].append(
            inst_pos_clock[inst_index])

    inst_avg_clock_info: dict = {}
    for key in inst_clock_info.keys():
        inst_avg_clock_info[key] = np.mean(inst_clock_info[key], axis=0)

    return inst_records, inst_pos_clock, inst_avg_clock_info


def IsBranchInst(instr: str) -> bool:
    branch_instructions = ['beq', 'bne', 'blt', 'bge', 'bltu',
                           'bgeu', 'beqz', 'bnez', 'j', 'jal', 'jalr', 'ret']
    branch_instructions += ['c_beqz', 'c_bnez',
                            'c_j', 'c_jal', 'c_jr', 'c_jalr']
    return any(instr.split()[0].startswith(branch) for branch in branch_instructions)


def ExtractBasicBlocks(pc_inst_list: tuple[str, str]) -> Counter:
    basic_blocks = Counter()
    current_block = []

    for i, (pc, inst) in tqdm(enumerate(pc_inst_list), desc='Analyzing Traces'):
        current_block.append((pc, inst))
        if IsBranchInst(inst) and current_block:
            basic_blocks[tuple(current_block)] += 1
            current_block = []
    if current_block:
        basic_blocks[tuple(current_block)] += 1

    return basic_blocks


def bbl_main(inst_info, inst_avg_clock_info, inter_gap, inner_gap):
    basic_blocks = ExtractBasicBlocks(inst_info)

    mode_str = "inter-gap" if inter_gap else "inner-gap" if inner_gap else "normal"

    print(f"Top 10 most common basic blocks (mode:{mode_str}):")
    for block, count in basic_blocks.most_common(10):
        print()
        print(f"Count: {count}")
        print("Instructions:")
        base_header = "  PC        : Instruction"
        clock_head = "               : " + \
            " ".join([f"{stage_name:>9}" for _,
                     stage_name in enumerate(StageNameLong)])

        if inter_gap or inner_gap:
            header = base_header + clock_head
        else:
            header = base_header

        print(header)

        for pc, instr in block:
            print(f"  {pc:10}: {instr:25}", end=' ')
            if inter_gap or inner_gap:
                formatted_clock = [
                    f"{clock:9.2f}" for clock in inst_avg_clock_info[(pc, instr)]]
                formatted_clock = " ".join(formatted_clock)
                print(f": {formatted_clock}", end=' ')
            print()


def perfcct_main(inst_info, inst_pos_clock_info, start_pc, end_pc, attention_pc: list[str]):

    for i in len(inst_info):
        pc, asm = inst_info[i]
        pos = inst_pos_clock_info[i]

        if int(pc, 16) < int(start_pc, 16) \
                or int(pc, 16) > int(end_pc, 16):
            continue

        print(f"{pc:8}:{asm:25}:", end=' ')

        for j in range(len(pos)):
            print(f'{StageNameShort(j)}{pos[j]}', end=' ')

        if pc in attention_pc:
            print("<<====", end=' ')

        print()
        if pc == end_pc:
            print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('sqldb', action='store',
                        help='Path to the SQLite database')
    parser.add_argument('-p', '--period', action='store',
                        type=int,
                        default=333,
                        help='Number of ticks per clock cycle')
    parser.add_argument('--platform', action='store',
                        choices=['gem5', 'rtl'],
                        default='gem5',
                        help='Platform to analyze')
    parser.add_argument('-s', '--start-tick', action='store',
                        type=int,
                        default=0,
                        help='Start tick to analyze')
    parser.add_argument('-e', '--end-tick', action='store',
                        type=int,
                        default=-1,
                        help='End tick to analyze')
    parser.add_argument('-n', '--num-insts', action='store',
                        type=int,
                        default=-1,
                        help='MAX Number of instructions to analyze')
    parser.add_argument('--inter-gap', action='store_true',
                        default=False,
                        help='Analyze inter-gap')
    parser.add_argument('--inner-gap', action='store_true',
                        default=False,
                        help='Analyze inner-gap')
    parser.add_argument('--tool', action='store',
                        choices=['perfcct', 'bbl'],
                        default= 'bbl',
                        help='Mode to analyze')
    parser.add_argument('--attention-pc', action='store',
                        nargs='+',
                        default=[],
                        help='Attention PC')
    parser.add_argument('--start-pc', action='store',
                        type=str,
                        default=0,
                        help='Start PC to analyze, a hex value start with 0x')
    parser.add_argument('--end-pc', action='store',
                        type=str,
                        default=0, help='End PC to analyze, a hex value start with 0x')

    args = parser.parse_args()

    if args.platform.lower() == 'rtl':
        args.period = 1

    if args.inter_gap and args.inner_gap:
        raise ValueError("Cannot set both inter-gap and inner-gap to True")

    inst_info, inst_pos_clock_info, inst_avg_clock_info = ReadDB(
        args.sqldb, args.start_tick, args.end_tick, args.period, args.inter_gap, args.inner_gap)

    if args.tool == 'perfcct':
        perfcct_main(inst_info, inst_pos_clock_info, args.start_pc, args.end_pc, args.attention_pc)
    elif args.tool == 'bbl':
        bbl_main(inst_info, inst_avg_clock_info, args.inter_gap, args.inner_gap)

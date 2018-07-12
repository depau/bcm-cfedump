#!/usr/bin/python3
# -*- coding: utf-8 -*-
import argparse
import re
import sys
import time
import traceback
from typing import Generator, TextIO

import serial

MAX_RETRIES = 5
NAND_SIZE = 524288 * 1024
BLOCK_SIZE = 128 * 1024
PAGE_SIZE = 2048

line_regex = re.compile(r'(?P<addr>[0-9a-fA-F]{8}):(?P<data>(?: [0-9a-fA-F]{8}){4})\s+.{16}')


def parse_hex_byte_string(hexbytes: str) -> bytes:
    assert len(hexbytes) % 2 == 0
    return int(hexbytes, 16).to_bytes(len(hexbytes) // 2, 'big')


def parse_serial_line(line: str) -> Generator[bytes, None, None]:
    m = line_regex.match(line)

    for chunk in m.group('data').split():
        yield parse_hex_byte_string(chunk)


def format_size(size: int) -> str:
    units = ('K', 'M', 'G', 'T')
    count = 0

    while size > 1500:
        size /= 1024
        count += 1

    return "{}{}B".format(round(size, 1), units[count])


def format_time(time: int) -> str:
    if time < 60:
        return "{}s".format(time)

    s = time % 60
    time //= 60
    if time < 60:
        return "{}m {}s".format(time, s)

    m = time % 60
    time //= 60
    if time < 60:
        return "{}h {}m {}s".format(time, m, s)

    h = time % 24
    time //= 24
    return "{}d {}h {}m {}s".format(time, h, m, s)


class PrettyPrinter:
    def __init__(self, out: TextIO):
        self.out = out
        self._lastline_len = 0

    def clear_line(self):
        print('\r' + ' ' * self._lastline_len, file=self.out)

    def print(self, string):
        if len(string) > 100000:
            print(string, file=self.out, end='')
        else:
            lines = string.split("\n")
            self._lastline_len = len(lines[-1])

            for l in lines[:-1]:
                print(string, file=self.out)

            if lines[-1] != '':
                print(string, file=self.out, end='')

        self.out.flush()

    def msg(self, string):
        self.clear_line()
        self.print(string)
        print('\n\n', end='', file=self.out)

    def error(self, msg):
        self.msg(msg)

    def exc(self):
        string = traceback.format_exc()
        self.error(string)


class ProgressPrinter(PrettyPrinter):
    chars = "⡏⠟⠻⢹⣸⣴⣦⣇"

    def __init__(self, out: TextIO, item_size: int, item_name: str):
        super().__init__(out)
        self.item_size = item_size
        self.item_name = item_name
        self._chars_step = 0
        self._last_done = -1
        self._last_total = -1
        self._clean = True
        self._last_time = -1

    def clear_line(self):
        super().clear_line()
        self._clean = True

    def print_progress(self, done, total):
        if self._last_total != total:
            self.clear_line()

        string = "\r {} ".format(self.chars[self._chars_step])

        string += "[{}/{} {}] ".format(done, total, self.item_name)
        string += "[{}/{}] ".format(format_size(done * self.item_size), format_size(total * self.item_size))

        if self._last_time > 0:
            delta_t = time.time() - self._last_time
            delta_b = done - self._last_done
            speed = delta_b / delta_t

            string += "[{}/s] ".format(format_size(speed))

            remaining = total - done
            eta = int(remaining // speed)

            string += "[ETA: {}]".format(format_time(eta))

        self._chars_step = (self._chars_step + 1) % len(self.chars)
        self._last_done = done
        self._last_total = total
        self._last_time = time.time()

        self.print(string)


class CFECommunicator:
    # noinspection PyShadowingNames
    def __init__(self, serial: serial.Serial, block_size: int = BLOCK_SIZE, page_size: int = PAGE_SIZE,
                 nand_size: int = NAND_SIZE, max_retries: int = MAX_RETRIES, printer: PrettyPrinter = None):
        self.max_retries = max_retries
        self.block_size = block_size
        self.page_size = page_size
        self.nand_size = nand_size
        self.ser = serial
        self.printer = printer or PrettyPrinter(sys.stdout)

    def eat_junk(self) -> None:
        while self.ser.read(1):
            pass

    def wait_for_prompt(self) -> None:
        self.printer.msg("Waiting for a prompt...")
        while True:
            self.ser.write(b"\x03")
            if self.ser.read(1) == b'C' and self.ser.read(1) == b'F' \
                    and self.ser.read(1) == b'E' and self.ser.read(1) == b'>':
                self.eat_junk()
                return

    def read_page(self, block: int, page: int) -> bytes:
        buf = b''
        main_area_read = False

        self.ser.write("dn {block} {page} 1\r\n".format(block=block, page=page).encode())
        self.ser.readline()  # remove echo

        while True:
            line = self.ser.readline().strip()

            if line.startswith(b"-----"):
                if main_area_read:
                    break
                main_area_read = True
                continue

            if len(line) == 0:
                continue

            try:
                for b in parse_serial_line(line.decode()):
                    buf += b
            except UnicodeDecodeError:
                traceback.print_exc()

        if len(buf) != self.page_size:
            raise IOError("Read page size ({}) different from expected size ({})"
                          .format(len(buf), self.page_size))

        self.eat_junk()

        return buf

    def read_pages(self, block: int, page_start: int, number: int) -> Generator[bytes, None, None]:
        for page in range(page_start, page_start + number):
            retries = 0

            while retries < self.max_retries:
                try:
                    yield self.read_page(block, page)
                    break
                except Exception:
                    print("Block {} page {} read failed, retrying.".format(block, page))
                    self.printer.exc()
            else:
                raise IOError("Max number of page read retries exceeded")

    def read_block(self, block: int) -> Generator[bytes, None, None]:
        count = 0
        for i in self.read_pages(block, 0, self.block_size // self.page_size):
            yield i
            count += 1

        expected = self.block_size // self.page_size
        if count != expected:
            raise IOError("Read block size ({}) different from expected size ({})"
                          .format(count, expected))

    def read_blocks(self, block: int, number: int) -> Generator[bytes, None, None]:
        for block in range(block, block + number):
            yield from self.read_block(block)

    def read_nand(self) -> Generator[bytes, None, None]:
        for block in range(NAND_SIZE // BLOCK_SIZE):
            yield from self.read_block(block)


def main():
    parser = argparse.ArgumentParser(description="Broadcom CFE dumper")
    parser.add_argument('-N', '--nand-size', type=int, help="NAND size", default=NAND_SIZE)
    parser.add_argument('-B', '--block-size', type=int, help="Block size", default=BLOCK_SIZE)
    parser.add_argument('-P', '--page-size', type=int, help="Page size", default=PAGE_SIZE)
    parser.add_argument('-D', '--device', type=str, help="Serial port", required=True)
    parser.add_argument('-b', '--baudrate', type=str, help="Baud rate", default=115200)
    parser.add_argument('-t', '--timeout', type=float, help="Serial port timeout", default=0.1)
    parser.add_argument('-O', '--output', type=str, help="Output file, '-' for stdout", default='-')
    parser.add_argument('-r', '--max-retries', type=int, help="Max retries per page on failure", default=MAX_RETRIES)

    subparsers = parser.add_subparsers(help="Available commands", dest='command')

    readpage_parser = subparsers.add_parser('page', help="Read one or more pages")
    readpage_parser.add_argument('block', type=int, help="Block to read pages from")
    readpage_parser.add_argument('page', type=int, help="Page to read")
    readpage_parser.add_argument('number', type=int, help="Number of subsequent pages to read (if more than 1)",
                                 default=1)

    readblock_parser = subparsers.add_parser('block', help="Read one or more blocks")
    readblock_parser.add_argument('block', type=int, help="Block to read")
    readblock_parser.add_argument('number', type=int, help="Number of subsequent blocks to read (if more than 1)",
                                  default=1)

    subparsers.add_parser('nand', help="Read the entire NAND")

    args = parser.parse_args()
    printer = ProgressPrinter(sys.stdout if args.output != "-" else sys.stderr, args.page_size, "pages")
    ser = serial.Serial(args.device, args.baudrate, timeout=args.timeout)
    c = CFECommunicator(ser, args.block_size, args.page_size, args.nand_size, args.max_retries, printer)

    if args.command == 'page':
        gen = c.read_pages(args.block, args.page, args.number)
        pages = args.number
    elif args.command == 'block':
        gen = c.read_blocks(args.block, args.number)
        pages = args.block_size // args.page_size * args.number
    elif args.command == 'nand':
        gen = c.read_nand()
        pages = args.nand_size // args.page_size
    else:
        raise RuntimeError

    pages_read = 0

    c.wait_for_prompt()

    with open(args.output, 'wb') as output:
        for page in gen:
            pages_read += 1
            output.write(page)
            printer.print_progress(pages_read, pages)

    printer.print("\n\n")

if __name__ == "__main__":
    main()
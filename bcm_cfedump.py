#!/usr/bin/python3
# -*- coding: utf-8 -*-
import argparse
import re
import sys
import time
import traceback
from functools import wraps
from typing import Generator, TextIO, BinaryIO

import serial

MAX_RETRIES = 5
NAND_SIZE = 524288 * 1024
BLOCK_SIZE = 128 * 1024
PAGE_SIZE = 2048

line_regex = re.compile(r'(?P<addr>[0-9a-fA-F]{8}):(?P<data>(?: [0-9a-fA-F]{8}){4})(?:\s+.{16})?')


def parse_hex_byte_string(hexbytes: str) -> bytes:
    assert len(hexbytes) % 2 == 0
    return int(hexbytes, 16).to_bytes(len(hexbytes) // 2, 'big')


def parse_serial_line(line: str) -> (int, bytes):
    m = line_regex.match(line)

    try:
        addr = int(m.group('addr'), 16)
        bstr = b''
        for chunk in m.group('data').split():
            bstr += parse_hex_byte_string(chunk)

        return addr, bstr
    except Exception:
        print("\n\nError caused by line: '{}'".format(line))
        raise


def format_size(size: int) -> str:
    units = ('', 'K', 'M', 'G', 'T')
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
    if time < 24:
        return "{}h {}m {}s".format(time, m, s)

    h = time % 24
    time //= 24
    return "{}d {}h {}m {}s".format(time, h, m, s)


def print_offset_on_exc(gen):
    @wraps(gen)
    def wrapper(self, *a, **kw):
        try:
            yield from gen(self, *a, **kw)
        except Exception as e:
            if not getattr(e, "offset_printed", None):
                print("Error at offset {} in file".format(self.input_file.tell()))
                e.offset_printed = True
            raise e

    return wrapper


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


class CFEParserBase:
    def __init__(self, printer: PrettyPrinter, block_size: int = BLOCK_SIZE, page_size: int = PAGE_SIZE,
                 nand_size: int = NAND_SIZE, max_retries: int = MAX_RETRIES):
        self.printer = printer
        self.max_retries = max_retries
        self.block_size = block_size
        self.page_size = page_size
        self.nand_size = nand_size

    def _read(self, *a, **kw) -> bytes:
        raise NotImplementedError

    def _write(self, *a, **kw) -> int:
        raise NotImplementedError

    def _readline(self, *a, **kw) -> bytes:
        raise NotImplementedError

    def _file(self) -> BinaryIO:
        raise NotImplementedError

    def wait_for_prompt(self):
        raise NotImplementedError

    def eat_junk(self) -> None:
        while self._read(1):
            pass

    def parse_pages_bulk(self) -> Generator[bytes, None, None]:
        while not self._readline().startswith(b"-----"):
            pass
        buf = b''
        last_addr = -1

        for line in self._file():
            line = line.strip()

            if not line:
                continue

            # Spare area. Yield and skip to next page
            if line.startswith(b"-----") and b'spare area' in line:
                yield buf
                buf = b''

                for line in self._file():
                    if line.startswith(b"-----"):
                        break
                else:
                    break
                continue

            # noinspection PyBroadException
            try:
                addr, buf = parse_serial_line(line.decode())
                if addr <= last_addr:
                    continue
                while last_addr != -1 and addr - last_addr > 16:
                    last_addr += 16
                    self.printer.msg('Address {} missing, padding with zeroes'.format(hex(last_addr)))
                    yield b'\0' * 16
                last_addr = addr
            except Exception:
                traceback.print_exc()

    def read_page(self, block: int, page: int) -> bytes:
        buf = b''
        main_area_read = False
        last_addr = -1

        self._read("dn {block} {page} 1\r\n".format(block=block, page=page).encode())

        while not self._readline().startswith(b"-----"):
            pass

        while True:
            line = self._readline().strip()

            if line.startswith(b"-----"):
                break

            if len(line) == 0:
                continue

            try:
                addr, buf_temp = parse_serial_line(line.decode())
                buf += buf_temp
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
                    retries += 1
                    self.printer.exc()
            else:
                raise IOError("Max number of page read retries exceeded")

    def read_pages_bulk(self, block: int, page_start: int, number: int) -> Generator[bytes, None, None]:
        self._write("dn {block} {page} {number}\r\n".format(block=block, page=page_start, number=number).encode())
        yield from self.parse_pages_bulk()

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
        for block in range(self.nand_size // self.block_size):
            yield from self.read_block(block)

    def read_nand_bulk(self) -> Generator[bytes, None, None]:
        yield from self.read_pages_bulk(0, 0, self.nand_size // self.page_size)


class CFECommunicator(CFEParserBase):
    # noinspection PyShadowingNames
    def __init__(self, serial: serial.Serial, block_size: int = BLOCK_SIZE, page_size: int = PAGE_SIZE,
                 nand_size: int = NAND_SIZE, max_retries: int = MAX_RETRIES, printer: PrettyPrinter = None):
        super().__init__(printer, block_size, page_size, nand_size, max_retries)
        self.ser = serial

    def _read(self, *a, **kw) -> bytes:
        if not type(*a) is int:
            self.ser.write(*a, **kw)
            return self.ser.read(len(*a))
        return self.ser.read(*a, **kw)

    def _write(self, *a, **kw) -> int:
        return self.ser.write(*a, **kw)

    def _readline(self, *a, **kw) -> bytes:
        return self.ser.readline(*a, **kw)

    def _file(self):
        return self.ser

    def wait_for_prompt(self) -> None:
        self.printer.msg("Waiting for a prompt...")
        while True:
            self._read(b"\r\n")
            if self._read(1) == b'C' and self._read(1) == b'F' \
                    and self._read(1) == b'E' and self._read(1) == b'>':
                self.eat_junk()
                return


class CFEParser(CFEParserBase):
    def __init__(self, input_file: BinaryIO, block_size: int = BLOCK_SIZE, page_size: int = PAGE_SIZE,
                 nand_size: int = NAND_SIZE, max_retries: int = MAX_RETRIES, printer: PrettyPrinter = None):
        super().__init__(printer, block_size, page_size, nand_size, max_retries)
        self.input_file = input_file

    def _read(self, *a, **kw) -> bytes:
        return self.input_file.read(*a, **kw)

    def _file(self):
        return self.input_file

    def _write(self, *a, **kw) -> int:
        return 0

    def _readline(self, *a, **kw) -> bytes:
        return self.input_file.readline(*a, **kw)

    def wait_for_prompt(self) -> None:
        pass

    @print_offset_on_exc
    def parse_pages_bulk(self) -> Generator[bytes, None, None]:
        return super().parse_pages_bulk()

    @print_offset_on_exc
    def read_page(self, block: int, page: int) -> bytes:
        return super().read_page(block, page)

    @print_offset_on_exc
    def read_pages(self, block: int, page_start: int, number: int) -> Generator[bytes, None, None]:
        return super().read_pages(block, page_start, number)

    @print_offset_on_exc
    def read_pages_bulk(self, block: int, page_start: int, number: int) -> Generator[bytes, None, None]:
        return super().read_pages_bulk(block, page_start, number)

    @print_offset_on_exc
    def read_block(self, block: int) -> Generator[bytes, None, None]:
        return super().read_block(block)

    @print_offset_on_exc
    def read_blocks(self, block: int, number: int) -> Generator[bytes, None, None]:
        return super().read_blocks(block, number)

    @print_offset_on_exc
    def read_nand(self) -> Generator[bytes, None, None]:
        return super().read_nand()

    @print_offset_on_exc
    def read_nand_bulk(self) -> Generator[bytes, None, None]:
        return super().read_nand_bulk()


def main():
    parser = argparse.ArgumentParser(description="Broadcom CFE dumper")
    parser.add_argument('-N', '--nand-size', type=int, help="NAND size", default=NAND_SIZE)
    parser.add_argument('-B', '--block-size', type=int, help="Block size", default=BLOCK_SIZE)
    parser.add_argument('-P', '--page-size', type=int, help="Page size", default=PAGE_SIZE)
    parser.add_argument('-b', '--baudrate', type=str, help="Baud rate", default=115200)
    parser.add_argument('-t', '--timeout', type=float, help="Serial port timeout", default=0.1)
    parser.add_argument('-O', '--output', type=str, help="Output file, '-' for stdout", default='-')
    parser.add_argument('-r', '--max-retries', type=int, help="Max retries per page on failure", default=MAX_RETRIES)

    group = parser.add_mutually_exclusive_group()
    group.add_argument('-D', '--device', type=str, help="Serial port")
    group.add_argument('-i', '--input-file', type=str, help="Input file")

    subparsers = parser.add_subparsers(help="Available commands", dest='command')

    readpage_parser = subparsers.add_parser('page', help="Read one or more pages")
    readpage_parser.add_argument('block', type=int, help="Block to read pages from")
    readpage_parser.add_argument('page', type=int, help="Page to read")
    readpage_parser.add_argument('number', type=int, help="Number of subsequent pages to read (if more than 1)",
                                 default=1)

    readpage_parser = subparsers.add_parser('pages_bulk', help="Read one or more pages in bulk")
    readpage_parser.add_argument('block', type=int, help="Block to read pages from")
    readpage_parser.add_argument('page', type=int, help="Page to read")
    readpage_parser.add_argument('number', type=int, help="Number of subsequent pages to read (if more than 1)",
                                 default=1)

    readblock_parser = subparsers.add_parser('block', help="Read one or more blocks")
    readblock_parser.add_argument('block', type=int, help="Block to read")
    readblock_parser.add_argument('number', type=int, help="Number of subsequent blocks to read (if more than 1)",
                                  default=1)

    subparsers.add_parser('nand', help="Read the entire NAND")
    subparsers.add_parser('nand_bulk', help="Read the entire NAND in bulk")

    args = parser.parse_args()
    printer = ProgressPrinter(sys.stdout if args.output != "-" else sys.stderr, args.page_size, "pages")

    if getattr(args, "device", None):
        ser = serial.Serial(args.device, args.baudrate, timeout=args.timeout)
        c = CFECommunicator(ser, args.block_size, args.page_size, args.nand_size, args.max_retries, printer)
    elif getattr(args, "input_file", None):
        ser = open(args.input_file, 'rb')
        c = CFEParser(ser, args.block_size, args.page_size, args.nand_size, args.max_retries, printer)
    else:
        raise ValueError("Please provide an input")

    if args.command == 'page':
        gen = c.read_pages(args.block, args.page, args.number)
        pages = args.number
    elif args.command == 'pages_bulk':
        gen = c.read_pages_bulk(args.block, args.page, args.number)
        pages = args.number
    elif args.command == 'block':
        gen = c.read_blocks(args.block, args.number)
        pages = args.block_size // args.page_size * args.number
    elif args.command == 'nand':
        gen = c.read_nand()
        pages = args.nand_size // args.page_size
    elif args.command == 'nand_bulk':
        gen = c.read_nand_bulk()
        pages = args.nand_size // args.page_size
    else:
        raise RuntimeError

    pages_read = 0

    c.wait_for_prompt()

    with open(args.output, 'wb') as output:
        try:
            for page in gen:
                pages_read += 1
                output.write(page)
                if type(c) == CFECommunicator or pages_read % 200 == 0:
                    printer.print_progress(pages_read, pages)
                    output.flush()
        except Exception:
            printer.print_progress(pages_read, pages)
            output.flush()
            raise
        output.flush()

    printer.print("\n\n")


if __name__ == "__main__":
    main()

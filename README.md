# bcm-cfedump
Broadcom CFE NAND dumper (uses `dn` command)

## Usage

```
usage: bcm_cfedump.py [-h] [-N NAND_SIZE] [-B BLOCK_SIZE] [-P PAGE_SIZE] -D
                      DEVICE [-b BAUDRATE] [-t TIMEOUT] [-O OUTPUT]
                      [-r MAX_RETRIES]
                      {page,block,nand} ...

Broadcom CFE dumper

positional arguments:
  {page,block,nand}     Available commands
    page                Read one or more pages
    block               Read one or more blocks
    nand                Read the entire NAND

optional arguments:
  -h, --help            show this help message and exit
  -N NAND_SIZE, --nand-size NAND_SIZE
                        NAND size
  -B BLOCK_SIZE, --block-size BLOCK_SIZE
                        Block size
  -P PAGE_SIZE, --page-size PAGE_SIZE
                        Page size
  -D DEVICE, --device DEVICE
                        Serial port
  -b BAUDRATE, --baudrate BAUDRATE
                        Baud rate
  -t TIMEOUT, --timeout TIMEOUT
                        Serial port timeout
  -O OUTPUT, --output OUTPUT
                        Output file, '-' for stdout
  -r MAX_RETRIES, --max-retries MAX_RETRIES
                        Max retries per page on failure


usage: bcm_cfedump.py page [-h] block page number

positional arguments:
  block       Block to read pages from
  page        Page to read
  number      Number of subsequent pages to read (if more than 1)



usage: bcm_cfedump.py block [-h] block number

positional arguments:
  block       Block to read
  number      Number of subsequent blocks to read (if more than 1)


usage: bcm_cfedump.py nand [-h]
Dump the whole NAND
```



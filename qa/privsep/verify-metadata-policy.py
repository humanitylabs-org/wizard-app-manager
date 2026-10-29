#!/usr/bin/env python3
"""Interpret the emitted cBPF bytes, not a duplicated proposed policy.
Static evidence only: kernel installation and actual EPERM are separate gates.
"""
import hashlib
import json
from pathlib import Path
import struct
import sys

raw = Path(sys.argv[1]).read_bytes()
assert raw and len(raw) % 8 == 0
program = [struct.unpack_from('<HBBI', raw, at) for at in range(0,len(raw),8)]
assert len(program) < 4096
arch = {'amd64':0xc000003e, 'arm64':0xc00000b7}[sys.argv[2]]
native = {'amd64':(332,1,60,231),'arm64':(291,64,93,94)}[sys.argv[2]]
statx, write, exit_, exit_group = native
DENY=0x50001; ALLOW=0x7fff0000; KILL=0x80000000

def result(nr, args=(0,)*6, abi=arch):
    data=struct.pack('<IIQ6Q',nr,abi,0,*[x&0xffffffffffffffff for x in args])
    pc=0; accumulator=0
    for _ in range(len(program)+1):
        assert 0 <= pc < len(program), 'out-of-range cBPF jump'
        op,jt,jf,k=program[pc]; pc+=1
        if op==0x20:
            assert k%4==0 and k+4<=len(data)
            accumulator=struct.unpack_from('<I',data,k)[0]
        elif op==0x15:
            pc += jt if accumulator==k else jf
        elif op==0x45:
            pc += jt if accumulator&k else jf
        elif op==0x06:
            assert k in (DENY,ALLOW,KILL), hex(k)
            return k
        else:
            raise AssertionError('unsupported instruction '+hex(op))
    raise AssertionError('cBPF did not return')

checks=0
def expect(nr,args,expected,abi=arch):
    global checks
    got=result(nr,args,abi); checks+=1
    assert got==expected,(nr,args,hex(abi),hex(got),hex(expected))

for nr in range(1024):
    # Only exit calls permit completely unconstrained argument words.
    expect(nr,(0,)*6,ALLOW if nr in (exit_,exit_group) else DENY)
for nr in (statx,write,exit_,exit_group,0):
    for other in (0x40000003,0xc000003e,0xc00000b7,0x40000028,0):
        if other!=arch: expect(nr,(0,)*6,KILL,other)
for nr in range(1024):
    if arch==0xc000003e: expect(nr|0x40000000,(0,)*6,KILL)
valid=(-100,0x12340000,0x100,0x7ff,0x56780000,0)
expect(statx,valid,ALLOW)
for idx in (0,2,3):
    for bad in (0,1,0x100,0x7ff,0xffffff9c,0x100000100,0x1000007ff,0xffffffffffffffff):
        if bad!=(valid[idx]&0xffffffffffffffff):
            value: list[int] = list(valid); value[idx]=bad; expect(statx,value,DENY)
for fd in range(64):
    expect(write,(fd,0,32,0,0,0),ALLOW if fd in (1,2) else DENY)
    expect(write,((1<<32)|fd,0,32,0,0,0),DENY)
for nr in (exit_,exit_group):
    expect(nr,(0xffffffffffffffff,)*6,ALLOW)
print(json.dumps({'scope':'static cBPF interpreter, not kernel execution','arch':sys.argv[2],
    'policy_sha256':hashlib.sha256(raw).hexdigest(),'instruction_count':len(program),
    'assertions':checks,'passed':True},sort_keys=True))

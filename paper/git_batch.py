"""Bounded, binary Git blob retrieval. No shell, network or persistent cache."""
import hashlib
import os
import re
import subprocess
from types import MappingProxyType


def blobs(root, requests, *, timeout=15):
    requests = tuple(requests)
    if not requests or len(set(requests)) != len(requests):
        raise ValueError('Distinct explicit Git objects required')
    for request in requests:
        if not re.fullmatch(r'[0-9a-f]{40}:[A-Za-z0-9_./-]+', request) or '..' in request:
            raise ValueError('Fixed revision and safe literal path required')
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    result = subprocess.run(['git', 'cat-file', '--batch'], cwd=root,
        input=('\n'.join(requests)+'\n').encode('ascii'), capture_output=True,
        check=True, timeout=timeout, creationflags=flags)
    raw, offset, output = result.stdout, 0, {}
    for request in requests:
        end = raw.find(b'\n', offset)
        if end < 0 or end-offset > 128:
            raise ValueError('Missing/oversized Git batch header')
        header = raw[offset:end]
        match = re.fullmatch(rb'([0-9a-f]{40}) blob (0|[1-9][0-9]*)', header)
        if not match:
            raise ValueError('Git object missing, malformed or not a blob')
        size = int(match[2]); offset = end+1
        if size > 8_000_000 or offset+size >= len(raw):
            raise ValueError('Truncated/oversized Git blob')
        data = raw[offset:offset+size]
        if raw[offset+size:offset+size+1] != b'\n':
            raise ValueError('Incorrect Git blob length/framing')
        oid = hashlib.sha1(b'blob '+str(size).encode()+b'\0'+data).hexdigest().encode()
        if oid != match[1]:
            raise ValueError('Git blob content/object identity mismatch')
        output[request] = data
        offset += size+1
    if offset != len(raw):
        raise ValueError('Unexpected/incomplete Git batch response')
    return MappingProxyType(output)

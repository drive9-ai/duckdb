#!/usr/bin/env python3
"""Exercise generic file-volume APIs using only Python's standard library.

No DuckDB, extension, SDK or database filenames are required. Use a disposable
filesystem; each run leaves one uniquely named volume containing durable files.
Reads DRIVE9_BASE and DRIVE9_API_KEY without printing either credential.
"""
import base64
import json
import os
import urllib.error
import urllib.request
import uuid


def main():
    endpoint = os.environ['DRIVE9_BASE'].rstrip('/')
    credential = os.environ['DRIVE9_API_KEY']
    volume = '/index-worker-' + uuid.uuid4().hex
    token = None

    def call(op, **fields):
        request = dict(op=op, volume=volume, **fields)
        if token:
            request['token'] = token
        raw = json.dumps(request).encode()
        req = urllib.request.Request(endpoint + '/v1/file-volumes', data=raw,
                                     headers={'Authorization': 'Bearer ' + credential,
                                              'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.load(response)
        assert result['version'] == 1
        return result

    def read(file_id, length):
        return base64.b64decode(call('read', file_id=file_id, length=length).get('data', ''))

    try:
        token = call('acquire', create=True)['token']
        call('open', path='/segments/', directory=True, create=True)
        first = call('open', path='/segments/index.bin', create=True)['file_id']
        payload = bytes(range(256)) * 400
        call('write', file_id=first, offset=7, data=base64.b64encode(payload).decode())
        assert read(first, len(payload) + 7) == bytes(7) + payload
        call('truncate', file_id=first, offset=11)
        assert read(first, 99) == bytes(7) + payload[:4]
        replacement = call('open', path='/segments/next.bin', create=True)['file_id']
        call('write', file_id=replacement, data=base64.b64encode(b'next').decode())
        call('rename', path='/segments/next.bin', target='/segments/index.bin')
        assert read(first, 99) == bytes(7) + payload[:4]  # Existing handle stays pinned.
        assert call('stat', path='/segments/index.bin')['file_id'] == replacement
        call('open', path='/.tmp', create=True)  # Filename does not imply temporary lifetime.
        call('open', path='/scratch/', create=True, directory=True, ephemeral=True)
        assert call('open', path='/scratch/work', create=True)['ephemeral']
        call('sync')
        call('release')
        token = None
        token = call('acquire', read_only=True)['token']
        assert call('stat', path='/.tmp')['exists']
        assert not call('stat', path='/scratch/')['exists']
        assert not call('stat', path='/scratch/work')['exists']
        assert read(replacement, 4) == b'next'
        names = {entry['name'] for entry in call('list', path='/')['entries']}
        assert names == {'segments', '.tmp'}
        try:
            call('write', file_id=replacement, data=base64.b64encode(b'bad').decode())
        except urllib.error.HTTPError as error:
            assert error.code == 403
        else:
            raise AssertionError('Read-only lease accepted a write')
    finally:
        if token:
            call('release')
    print('Generic file-volume HTTP smoke passed (no DuckDB dependency)', flush=True)


if __name__ == '__main__':
    main()

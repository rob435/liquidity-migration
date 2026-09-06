import json
import os
import struct
import sys
import time

source = sys.stdin.buffer
while True:
    payload = bytearray()
    while True:
        header = source.read(4)
        if not header:
            sys.exit(0)
        count = struct.unpack("<I", header)[0]
        if not count:
            break
        payload.extend(source.read(count))
    request = json.loads(payload)
    started = time.process_time()
    while sys.argv[1] == "runaway" or time.process_time() - started < 1.1:
        pass
    reply = json.dumps({
        "kind": "finished",
        "callback_id": request["callback_id"],
        "state": request["state"],
        "pid": os.getpid(),
        "process_cpu_seconds": time.process_time(),
    }).encode()
    sys.stdout.buffer.write(struct.pack("<I", len(reply)) + reply + bytes(4))
    sys.stdout.buffer.flush()

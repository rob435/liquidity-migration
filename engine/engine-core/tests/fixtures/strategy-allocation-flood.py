import json, struct, sys
source = sys.stdin.buffer
payload = bytearray()
while True:
    count = struct.unpack('<I', source.read(4))[0]
    if not count: break
    payload.extend(source.read(count))
request = json.loads(payload)
allocations = []
for _ in range(96):
    allocations.append(bytearray(8 * 1024 * 1024))
reply = json.dumps({'kind':'finished','callback_id':request['callback_id'],'state':request['state']}).encode()
sys.stdout.buffer.write(struct.pack('<I',len(reply)) + reply + bytes(4))
sys.stdout.buffer.flush()

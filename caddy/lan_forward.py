"""Userspace TCP forwarder: bridges the host LAN IP to Docker's loopback-only
published ports. Docker Desktop on WSL2 only binds 127.0.0.1 even though
`docker ps` prints 0.0.0.0, so router-forwarded packets to the LAN IP find no
listener. This binds the LAN IP (high ports => no admin needed) and relays to
127.0.0.1. Intended to run as the logged-in user; persistent via a per-user
logon Scheduled Task (no elevation required).
"""
import asyncio
import sys

LAN_IP = "0.0.0.0"  # bind all ifaces incl 127.0.0.1 so host tailscaled can reach control via loopback (was 192.168.50.74)
# (listen_port_on_LAN_IP, connect_port_on_loopback)
# Loopback ports are Docker's private publish ports (18443/18080) to avoid the
# host-proxy collision on the real 8443/8080.
PAIRS = [(8443, 18443), (8080, 18080)]


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle(local_reader, local_writer, cport):
    try:
        remote_reader, remote_writer = await asyncio.open_connection("127.0.0.1", cport)
    except Exception as e:
        local_writer.close()
        return
    await asyncio.gather(
        pipe(local_reader, remote_writer),
        pipe(remote_reader, local_writer),
    )


async def _serve_one(lport, cport):
    # The logon task can fire before the LAN IP (192.168.50.74) is assigned, so
    # start_server would raise OSError and the whole process would exit (the
    # 2026-06-04 post-reboot outage: headscale control was unreachable until
    # this was relaunched by hand). Retry the bind until the address exists.
    while True:
        try:
            srv = await asyncio.start_server(
                lambda r, w, c=cport: handle(r, w, c), LAN_IP, lport
            )
            print(f"listening {LAN_IP}:{lport} -> 127.0.0.1:{cport}", flush=True)
            return srv
        except OSError as e:
            print(f"bind {LAN_IP}:{lport} not ready ({e}); retrying in 5s", flush=True)
            await asyncio.sleep(5)


async def main():
    servers = [await _serve_one(lport, cport) for lport, cport in PAIRS]
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)

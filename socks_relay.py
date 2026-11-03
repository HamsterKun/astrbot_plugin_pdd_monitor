#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地免认证 SOCKS5 中继 —— 专给「不会发 SOCKS5 密码」的客户端用

为什么需要它
    Chromium 的 --proxy-server 只认 socks5://host:port，握手时只声明 0x00
    （免认证）这一种方式，用户名密码一律不发。而家宽代理为了安全是强制认证的。
    两者对不上，Chromium 会直接连不上。

它怎么解决
    在本机（127.0.0.1）起一个不需要认证的 SOCKS5 端口，收到 CONNECT 之后
    自己带用户名密码去连上游的家宽代理，然后双向转发：

        Chromium → 127.0.0.1:<本地端口>（免认证）→ 本中继 → 上游（带认证）

    好处是「暴露到公网的那一端依然有密码保护」，不用为了迁就 Chromium 把
    隧道开成免认证。中继只监听回环地址，局域网里别的机器都连不上。

安全性
    上游的域名白名单、端口白名单、内网地址拦截全部照常生效 —— 中继只是把
    认证这一步替客户端做了，并没有放宽任何目标侧的限制。但反过来说：任何能
    访问到这个本地端口的人都能借用上游出口，所以它只监听 127.0.0.1。

用法（命令行）
    python socks_relay.py --upstream socks5://user:pass@frp-host:12345
    python socks_relay.py --upstream socks5://... --check    # 起完顺手验一条目标

用法（当模块）
    from socks_relay import Socks5Relay
    relay = Socks5Relay("socks5://user:pass@host:port").start()
    print(relay.url)      # socks5://127.0.0.1:54321 ，填给 Chromium
    ...
    relay.stop()
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys
import threading
from urllib.parse import urlsplit

SOCKS5_VERSION = 0x05
SOCKS5_NOAUTH = 0x00
SOCKS5_USERPASS = 0x02
SOCKS5_NOMETHOD = 0xFF

# RFC 1928 的 reply code，用来把上游的失败原因忠实回传给客户端
REPLY_TEXT = {
    0x01: "上游代理一般性故障",
    0x02: "上游规则不允许该目标",
    0x03: "网络不可达",
    0x04: "主机不可达",
    0x05: "连接被拒绝",
    0x06: "TTL 过期",
    0x07: "上游不支持该命令",
    0x08: "上游不支持该地址类型",
}

log = logging.getLogger("socks-relay")


class Upstream:
    """上游 SOCKS5 代理的地址与凭据。"""

    def __init__(self, url: str):
        if "://" not in url:
            url = "socks5://" + url
        sp = urlsplit(url)
        scheme = (sp.scheme or "socks5").lower()
        if scheme not in ("socks5", "socks5h", "socks"):
            raise ValueError(f"上游只支持 socks5://，收到 {scheme}://")
        if not sp.hostname:
            raise ValueError("上游地址缺少主机名")
        self.host = sp.hostname
        self.port = sp.port or 1080
        self.username = sp.username or ""
        self.password = sp.password or ""
        # socks5:// 的语义是「客户端本地解析域名」，但我们是代理链，必须让
        # 家宽那端解析（家里和机房拿到的 CDN 节点不一样，本地解析等于白绕）。
        # 所以无论地址写 socks5 还是 socks5h，域名一律原样转发。
        self.remote_dns = True

    def __str__(self) -> str:
        auth = "***@" if self.username else ""
        return f"socks5://{auth}{self.host}:{self.port}"


# ------------------------------------------------------------------ 上游握手

async def _upstream_handshake(reader, writer, up: Upstream, timeout: float) -> None:
    """和上游协商认证方式并完成认证。失败抛 ConnectionError，带可读原因。"""
    if up.username:
        writer.write(bytes([SOCKS5_VERSION, 0x01, SOCKS5_USERPASS]))
    else:
        writer.write(bytes([SOCKS5_VERSION, 0x01, SOCKS5_NOAUTH]))
    await writer.drain()

    resp = await asyncio.wait_for(reader.readexactly(2), timeout)
    log.debug(f"上游认证协商回应 {resp.hex()}")
    if resp[0] != SOCKS5_VERSION:
        raise ConnectionError("上游不是 SOCKS5（回的版本号不对）")
    if resp[1] == SOCKS5_NOMETHOD:
        raise ConnectionError("上游拒绝了协商的认证方式")
    if resp[1] != SOCKS5_USERPASS:
        return  # 上游选了免认证，不用再走认证

    u = up.username.encode("utf-8")
    p = up.password.encode("utf-8")
    writer.write(bytes([0x01, len(u)]) + u + bytes([len(p)]) + p)
    await writer.drain()
    ar = await asyncio.wait_for(reader.readexactly(2), timeout)
    log.debug(f"上游认证结果 {ar.hex()}")
    if ar[1] != 0x00:
        raise ConnectionError("上游用户名或密码错误")


async def _upstream_connect(reader, writer, atyp: int, addr: bytes, port: int,
                            timeout: float) -> int:
    """发 CONNECT 给上游，返回 reply code（0x00 表示成功）。"""
    writer.write(bytes([SOCKS5_VERSION, 0x01, 0x00, atyp]) + addr
                 + port.to_bytes(2, "big"))
    await writer.drain()
    head = await asyncio.wait_for(reader.readexactly(4), timeout)
    code = head[1]
    # 不管成功失败，BND 地址都要读完，否则隧道里的字节流会错位
    bnd_atyp = head[3]
    if bnd_atyp == 0x01:
        await asyncio.wait_for(reader.readexactly(4 + 2), timeout)
    elif bnd_atyp == 0x04:
        await asyncio.wait_for(reader.readexactly(16 + 2), timeout)
    elif bnd_atyp == 0x03:
        ln = (await asyncio.wait_for(reader.readexactly(1), timeout))[0]
        await asyncio.wait_for(reader.readexactly(ln + 2), timeout)
    return code


# -------------------------------------------------------------------- 中继本体

class _Relay:
    def __init__(self, up: Upstream, timeout: float = 15.0, idle_timeout: float = 90.0):
        self.up = up
        self.timeout = timeout
        self.idle_timeout = idle_timeout
        self.conns = 0

    async def handle(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        peer_s = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) else str(peer)
        self.conns += 1
        label = peer_s
        try:
            code = await self._session(reader, writer, peer_s)
            if code:
                log.info(f"{label} 结束（{REPLY_TEXT.get(code, f'0x{code:02x}')}）")
        except (asyncio.TimeoutError, asyncio.IncompleteReadError,
                ConnectionError, OSError) as e:
            log.debug(f"{label} 断开：{e!r}")
        except Exception as e:
            log.warning(f"{label} 异常：{e!r}")
        finally:
            self.conns -= 1
            try:
                writer.close()
            except Exception:
                pass

    async def _session(self, reader, writer, peer_s: str) -> int:
        to = self.timeout

        # 1) 欢迎：客户端只会声明 0x00，我们只接受 0x00
        ver, n = await asyncio.wait_for(reader.readexactly(2), to)
        if ver != SOCKS5_VERSION:
            return 0
        methods = await asyncio.wait_for(reader.readexactly(n), to) if n else b""
        if SOCKS5_NOAUTH not in methods:
            writer.write(bytes([SOCKS5_VERSION, SOCKS5_NOMETHOD]))
            await writer.drain()
            log.warning(f"{peer_s} 客户端只提供 {methods.hex()}，本中继只支持免认证")
            return 0
        writer.write(bytes([SOCKS5_VERSION, SOCKS5_NOAUTH]))
        await writer.drain()

        # 2) 请求
        ver, cmd, _rsv, atyp = await asyncio.wait_for(reader.readexactly(4), to)
        if ver != SOCKS5_VERSION:
            return 0x01
        # addr 统一存「ATYP 之后的完整字段」：IP 就是 4/16 字节原始地址，
        # 域名则是「长度字节 + 名字」。少放这个长度字节，上游会把名字的首字母
        # 当成长度字节去等数据，两边一起卡死 —— 这个坑踩过一次。
        if atyp == 0x01:
            addr = await asyncio.wait_for(reader.readexactly(4), to)
            target = socket.inet_ntoa(addr)
        elif atyp == 0x04:
            addr = await asyncio.wait_for(reader.readexactly(16), to)
            target = socket.inet_ntop(socket.AF_INET6, addr)
        elif atyp == 0x03:
            ln = (await asyncio.wait_for(reader.readexactly(1), to))[0]
            host_raw = await asyncio.wait_for(reader.readexactly(ln), to)
            addr = bytes([ln]) + host_raw
            try:
                target = host_raw.decode("idna")
            except UnicodeError:
                target = host_raw.decode("latin-1", "replace")
        else:
            await self._reply(writer, 0x08)
            return 0x08
        port = int.from_bytes(await asyncio.wait_for(reader.readexactly(2), to), "big")

        if cmd != 0x01:
            await self._reply(writer, 0x07)
            log.warning(f"{peer_s} 不支持的命令 0x{cmd:02x}")
            return 0x07

        label = f"{peer_s} → {target}:{port}"

        # 3) 连上游（注意：连的是代理本身，不是目标）
        try:
            up_r, up_w = await asyncio.wait_for(
                asyncio.open_connection(self.up.host, self.up.port), 10)
        except Exception as e:
            await self._reply(writer, 0x03)
            log.warning(f"{label} 连不上上游 {self.up}：{e}")
            return 0x03

        try:
            try:
                await _upstream_handshake(up_r, up_w, self.up, to)
                code = await _upstream_connect(up_r, up_w, atyp, addr, port, to)
            except Exception as e:
                await self._reply(writer, 0x01)
                # IncompleteReadError 的 str() 是空串，必须打 repr 才看得见
                log.warning(f"{label} 上游握手失败：{e!r}（上游 {self.up}）")
                try:
                    up_w.close()
                except Exception:
                    pass
                return 0x01

            if code != 0x00:
                await self._reply(writer, code)
                log.warning(f"{label} 上游拒绝：{REPLY_TEXT.get(code, hex(code))}")
                try:
                    up_w.close()
                except Exception:
                    pass
                return code

            # 4) 告诉客户端隧道通了，然后纯转发
            await self._reply(writer, 0x00)
            up, down = await self._pipe(reader, writer, up_r, up_w)
            log.info(f"{label} 200 {up}B↑ {down}B↓")
            return 0
        finally:
            try:
                up_w.close()
            except Exception:
                pass

    async def _pipe(self, cr, cw, sr, sw) -> tuple[int, int]:
        up = [0]
        down = [0]

        async def one_way(r, w, counter):
            try:
                while True:
                    data = await asyncio.wait_for(r.read(65536), self.idle_timeout)
                    if not data:
                        break
                    w.write(data)
                    await w.drain()
                    counter[0] += len(data)
            except (asyncio.TimeoutError, ConnectionError, OSError):
                pass
            finally:
                try:
                    w.close()
                except Exception:
                    pass

        await asyncio.gather(one_way(cr, sw, up), one_way(sr, cw, down),
                             return_exceptions=True)
        return up[0], down[0]

    async def _reply(self, writer, code: int) -> None:
        try:
            writer.write(bytes([SOCKS5_VERSION, code, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
            await writer.drain()
        except Exception:
            pass


# ---------------------------------------------------------------- 线程化封装

class Socks5Relay:
    """在后台线程里跑一个中继，给不支持 SOCKS5 认证的客户端当跳板。

    起完就能拿 relay.url 直接喂给 Chromium / 任何只认 socks5:// 的工具。
    """

    def __init__(self, upstream: str | Upstream,
                 listen_host: str = "127.0.0.1", listen_port: int = 0):
        self.up = upstream if isinstance(upstream, Upstream) else Upstream(upstream)
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.AbstractServer | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    @property
    def url(self) -> str:
        return f"socks5://{self.listen_host}:{self.port}"

    def start(self, timeout: float = 8.0) -> "Socks5Relay":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="socks-relay", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError(f"中继启动超时（{timeout}s）")
        if self._error is not None:
            raise RuntimeError(f"中继启动失败：{self._error}") from self._error
        return self

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        relay = _Relay(self.up)
        try:
            server = loop.run_until_complete(
                asyncio.start_server(relay.handle, self.listen_host,
                                     self.listen_port, limit=65536))
            self._server = server
            self.port = server.sockets[0].getsockname()[1]
        except BaseException as e:
            self._error = e
            self._ready.set()
            loop.close()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                server.close()
                loop.run_until_complete(server.wait_closed())
            except Exception:
                pass
            loop.close()

    def stop(self) -> None:
        loop, server = self._loop, self._server
        if loop is None:
            return
        try:
            if server is not None:
                loop.call_soon_threadsafe(server.close)
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._thread = None

    def __enter__(self) -> "Socks5Relay":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


# ------------------------------------------------------------------- 端到端验

async def check(relay_url: str, target: str = "mobile.yangkeduo.com", path: str = "/") -> int:
    """从「客户端视角」走一遍中继，确认隧道里真能取到数据。"""
    sp = urlsplit(relay_url)
    host, port = sp.hostname or "127.0.0.1", sp.port or 1080
    print(f"经中继 {host}:{port} 访问 {target}")
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, port), 10)
    except Exception as e:
        print(f"  [1] 失败  TCP 连不上中继 —— {e}")
        return 1
    print("  [1] 通过  中继 TCP 已连通")

    try:
        w.write(bytes([SOCKS5_VERSION, 0x01, SOCKS5_NOAUTH]))
        await w.drain()
        resp = await asyncio.wait_for(r.readexactly(2), 10)
        if resp != bytes([SOCKS5_VERSION, SOCKS5_NOAUTH]):
            print(f"  [2] 失败  中继没选免认证：{resp.hex()}")
            return 1
        print("  [2] 通过  免认证协商完成")

        raw = target.encode("idna") if not _is_ip(target) else None
        if raw is None:
            import ipaddress
            ip = ipaddress.ip_address(target)
            atyp = 0x01 if ip.version == 4 else 0x04
            addr = ip.packed
        else:
            atyp = 0x03
            addr = bytes([len(raw)]) + raw
        w.write(bytes([SOCKS5_VERSION, 0x01, 0x00, atyp]) + addr + (80).to_bytes(2, "big"))
        await w.drain()
        head = await asyncio.wait_for(r.readexactly(4), 20)
        if head[1] != 0x00:
            print(f"  [3] 失败  CONNECT 被拒：{REPLY_TEXT.get(head[1], hex(head[1]))}")
            return 1
        if head[3] == 0x01:
            await asyncio.wait_for(r.readexactly(6), 5)
        elif head[3] == 0x04:
            await asyncio.wait_for(r.readexactly(18), 5)
        else:
            ln = (await asyncio.wait_for(r.readexactly(1), 5))[0]
            await asyncio.wait_for(r.readexactly(ln + 2), 5)
        print("  [3] 通过  CONNECT 成功，隧道已建立")

        w.write((f"GET {path} HTTP/1.1\r\nHost: {target}\r\n"
                 "Connection: close\r\n\r\n").encode("latin-1"))
        await w.drain()
        data = await asyncio.wait_for(r.read(4096), 25)
    except Exception as e:
        print(f"  [4] 失败  隧道内请求出错 —— {e}")
        return 1
    finally:
        try:
            w.close()
        except Exception:
            pass

    first = data.split(b"\r\n", 1)[0].decode("latin-1", "replace").strip()
    print(f"  [4] 通过  端到端拿到 {len(data)} 字节，响应行：{first}")
    print("\n中继可用。Chromium 的 --proxy-server 填上面那个 socks5:// 地址即可。")
    return 0


def _is_ip(s: str) -> bool:
    try:
        import ipaddress
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="本地免认证 SOCKS5 中继（上游带认证）")
    ap.add_argument("--upstream", "-u", required=True,
                    help="上游 socks5 地址，如 socks5://user:pass@host:12345")
    ap.add_argument("--host", default="127.0.0.1", help="本地监听地址（默认只回环）")
    ap.add_argument("--port", type=int, default=0, help="本地端口，0 表示随机")
    ap.add_argument("--check", action="store_true", help="起来后顺手验一条目标再退出")
    ap.add_argument("--verbose", "-v", action="store_true", help="打印每条连接的日志")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    up = Upstream(args.upstream)
    relay = Socks5Relay(up, args.host, args.port)
    try:
        relay.start()
    except Exception as e:
        print(f"启动失败：{e}")
        return 1

    print(f"上游：{up}")
    print(f"本地：{relay.url}")
    if up.password and not args.check:
        print("（Chromium 就用上面这个地址，不用填账号密码）")

    try:
        if args.check:
            return asyncio.run(check(relay.url))
        print("\nCtrl+C 停止")
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        relay.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

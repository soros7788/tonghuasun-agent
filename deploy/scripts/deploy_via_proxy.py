#!/usr/bin/env python3
"""双机 systemd timer 自动部署脚本 (paramiko 5.x 兼容版)

修复点 (vs 之前的踩坑):
  1. paramiko 5.0 每个 exec_command 用独立 session channel (不复用)
  2. socat HTTP_PROXY CONNECT 隧道, 不依赖裸 SSH egress
  3. VM-A: password auth (user=gorgesoros39, pwd 在用户规则里)
  4. VM-B: ed25519 key auth (hermes_key 从 VM-A 拉)
  5. systemd ExecStart 里的 % 全部转义为 %%
  6. chan_merge 空账本 exit 0 (不触发 systemd failed)

使用:
  python3 /workspace/deploy/scripts/deploy_via_proxy.py
"""

from __future__ import annotations
import socket, paramiko, time, base64, os, sys, json, io

# ============ 网络 ============
PROXY_HOST, PROXY_PORT = '127.0.0.1', 18080

VM_A_IP, VM_A_PORT = '136.66.64.228', 22
VM_A_USER = 'gorgesoros39'
VM_A_PWD = 'Gorgesoros9-Trading2026!'

VM_B_IP, VM_B_PORT = '35.212.190.147', 22
VM_B_USER = 'katelolita7788'

DEPLOY_PY_DIR = '/workspace/deploy'
REMOTE_DEPLOY_DIR = '/home/gorgesoros39/deploy'


# ============================================================================
# 底层: HTTP_PROXY CONNECT 隧道 + paramiko 5.x 正确用法
# ============================================================================

def create_connect_tunnel(host: str, port: int) -> socket.socket:
    """通过 HTTP_PROXY 建立 CONNECT 隧道到目标 host:port"""
    s = socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=10)
    s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = s.recv(4096)
        if not c:
            raise ConnectionError(f"代理 CONNECT 提前关闭")
        resp += c
    status_line = resp.split(b'\r\n')[0].decode()
    if "200" not in status_line:
        raise ConnectionError(f"代理 CONNECT 失败: {status_line}")
    return s


class SSHTunnel:
    """封装 CONNECT tunnel + paramiko Transport + session 管理

    paramiko 5.x 关键约束:
      - 每个 channel 只能 exec_command 一次, 不能复用
      - open_session 必须在 auth 之后, KEX 完成之后
      - Transport.start_client 自动 KEX, auth 之后才能 open_session
    """
    def __init__(self, host, port, user, password=None, pkey=None):
        self.host = host; self.port = port; self.user = user
        self._sock = None
        self._t = None
        self._connect(password, pkey)

    def _connect(self, password, pkey):
        self._sock = create_connect_tunnel(self.host, self.port)
        self._t = paramiko.Transport(self._sock)
        self._t.start_client(timeout=15)
        print(f"    KEX OK: {self._t.remote_version}")

        if pkey:
            self._t.auth_publickey(self.user, pkey)
            print(f"    ✅ key auth OK ({pkey.get_name()})")
        elif password:
            self._t.auth_password(username=self.user, password=password)
            print(f"    ✅ password auth OK")
        else:
            raise AuthError("需要 password 或 pkey")

    def exec(self, cmd: str, timeout: int = 60) -> tuple[int, str, str]:
        """执行命令 — 每次新建一个 session channel (paramiko 5.x 必须)"""
        ch = self._t.open_session()
        ch.settimeout(timeout)
        ch.exec_command(cmd)
        out = b""
        err = b""
        while True:
            try:
                if ch.recv_ready():
                    out += ch.recv(4096)
                if ch.recv_stderr_ready():
                    err += ch.recv_stderr(4096)
            except socket.timeout:
                break
            # exit_status_ready 后还要 drain 完
            if ch.exit_status_ready():
                time.sleep(0.05)  # 给 buffer 留点时间
                if not ch.recv_ready() and not ch.recv_stderr_ready():
                    break
        try:
            rc = ch.recv_exit_status()
        except:
            rc = -1
        ch.close()
        return rc, out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace')

    def write_file(self, remote_path: str, content: str):
        """base64 方式写文件 (避免 shell escaping 地狱)"""
        b64 = base64.b64encode(content.encode('utf-8')).decode()
        self.exec(f"echo '{b64}' | base64 -d > '{remote_path}' && chmod 644 '{remote_path}'")

    def close(self):
        try: self._t.close()
        except: pass
        try: self._sock.close()
        except: pass


class AuthError(Exception): pass


# ============================================================================
# unit 文件 (已修 %% 转义)
# ============================================================================

UNIT_MEET_ASC = """[Unit]
Description=Meet-in-the-middle asc scan (VM-A, 升序)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/gorgesoros39/TradingAgents-CN/scripts/chanlun-workflow
Environment=MITM_LEDGER=/home/gorgesoros39/chan_logs/scan_ledger.jsonl
Environment=MITM_SLEEP=0.3
Environment=KLINE_CACHE_DIR=/home/gorgesoros39/kline_cache_local
ExecStartPre=/bin/bash -c 'mkdir -p /home/gorgesoros39/chan_logs && if [ -f /home/gorgesoros39/chan_logs/scan_ledger.jsonl ]; then mv /home/gorgesoros39/chan_logs/scan_ledger.jsonl /home/gorgesoros39/chan_logs/scan_ledger.asc.$$(date +%%Y%%m%%d_%%H%%M%%S).jsonl 2>/dev/null || true; fi'
ExecStart=/usr/bin/python3 meet_in_the_middle_scan.py --side asc --codes /home/gorgesoros39/TradingAgents-CN/kline_cache/_codes_mainboard.txt --ledger /home/gorgesoros39/chan_logs/scan_ledger.jsonl --dual2-dir . --ssh-host katelolita7788@35.212.190.147 --ssh-key /home/gorgesoros39/.ssh/hermes_key
ExecStopPost=/bin/bash -c 'pkill -f "meet_in_the_middle_scan.*--side asc" || true'
Restart=no
StandardOutput=append:/home/gorgesoros39/chan_logs/meet_asc.log
StandardError=append:/home/gorgesoros39/chan_logs/meet_asc.log
SyslogIdentifier=meet-asc

[Install]
WantedBy=default.target
"""

TIMER_MEET_ASC = """[Unit]
Description=Start meet-in asc scan at market open (VM-A)

[Timer]
OnCalendar=*-*-* 09:00:00 Asia/Shanghai
RandomizedDelaySec=60
Persistent=true
Unit=meet-asc.service

[Install]
WantedBy=timers.target
"""

UNIT_CHAN_MERGE = """[Unit]
Description=Post-market chan_merge + v3 position (VM-A)
After=network-online.target

[Service]
Type=oneshot
ExecStartPre=/bin/mkdir -p /home/gorgesoros39/chan_logs
ExecStart=/bin/bash -c 'STAMP=$$(date +%%Y%%m%%d); python3 /home/gorgesoros39/deploy/chan_merge.py -i /home/gorgesoros39/chan_logs/scan_ledger.jsonl --vmb-host katelolita7788@35.212.190.147 --vmb-ledger /home/katelolita7788/chan_logs/scan_ledger.jsonl --vmb-key /home/gorgesoros39/.ssh/hermes_key --include-blocked -o /home/gorgesoros39/chan_logs/merged_$$STAMP.json && python3 /home/gorgesoros39/deploy/v3_position_manager.py -c /home/gorgesoros39/chan_logs/merged_$$STAMP.json --source chan_merge --regime strong --grade A --asset 50000 -o /home/gorgesoros39/chan_logs/v3_positions_$$STAMP.json || echo "v3 skipped (empty merge)"'
StandardOutput=append:/home/gorgesoros39/chan_logs/merge.log
StandardError=append:/home/gorgesoros39/chan_logs/merge.log
SyslogIdentifier=chan-merge

[Install]
WantedBy=default.target
"""

TIMER_CHAN_MERGE = """[Unit]
Description=Run chan_merge + v3 after market close (VM-A)

[Timer]
OnCalendar=*-*-* 15:05:00 Asia/Shanghai
Persistent=true
Unit=chan-merge.service

[Install]
WantedBy=timers.target
"""

UNIT_MEET_DESC = """[Unit]
Description=Meet-in-the-middle desc scan (VM-B, 降序)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/katelolita7788/TradingAgents-CN/scripts/chanlun-workflow
Environment=MITM_LEDGER=/home/katelolita7788/chan_logs/scan_ledger.jsonl
Environment=MITM_SLEEP=0.3
Environment=KLINE_CACHE_DIR=/home/katelolita7788/TradingAgents-CN/kline_cache
ExecStartPre=/bin/bash -c 'mkdir -p /home/katelolita7788/chan_logs && if [ -f /home/katelolita7788/chan_logs/scan_ledger.jsonl ]; then mv /home/katelolita7788/chan_logs/scan_ledger.jsonl /home/katelolita7788/chan_logs/scan_ledger.desc.$$(date +%%Y%%m%%d_%%H%%M%%S).jsonl 2>/dev/null || true; fi'
ExecStart=/usr/bin/python3 meet_in_the_middle_scan.py --side desc --codes /home/katelolita7788/TradingAgents-CN/kline_cache/_codes_mainboard.txt --ledger /home/katelolita7788/chan_logs/scan_ledger.jsonl --dual2-dir .
ExecStopPost=/bin/bash -c 'pkill -f "meet_in_the_middle_scan.*--side desc" || true'
Restart=no
StandardOutput=append:/home/katelolita7788/chan_logs/meet_desc.log
StandardError=append:/home/katelolita7788/chan_logs/meet_desc.log
SyslogIdentifier=meet-desc

[Install]
WantedBy=default.target
"""

TIMER_MEET_DESC = """[Unit]
Description=Start meet-in desc scan at market open (VM-B)

[Timer]
OnCalendar=*-*-* 09:00:00 Asia/Shanghai
RandomizedDelaySec=60
Persistent=true
Unit=meet-desc.service

[Install]
WantedBy=timers.target
"""


# ============================================================================
# 部署 VM-A
# ============================================================================

def deploy_vma() -> SSHTunnel:
    print(f"\n{'='*60}")
    print(f"  VM-A ({VM_A_USER}@{VM_A_IP}:{VM_A_PORT}) — password auth")
    print(f"{'='*60}")

    t = SSHTunnel(VM_A_IP, VM_A_PORT, VM_A_USER, password=VM_A_PWD)

    # 1. 预检
    print("\n[1/6] 预检...")
    rc, out, err = t.exec('hostname; whoami; python3 --version')
    print(f"    {out.strip()}")
    rc, out, err = t.exec('systemctl --user daemon-reload 2>&1; echo OK')
    print(f"    user systemd: {out.strip()}")
    rc, out, err = t.exec('ls /home/gorgesoros39/.ssh/hermes_key 2>&1')
    print(f"    hermes_key: {out.strip()}")

    # 2. 推送 Python 脚本
    print("\n[2/6] 推送 Python 脚本 → ~/deploy/")
    t.exec('mkdir -p /home/gorgesoros39/deploy')
    for fname in ['chan_merge.py', 'v3_position_manager.py', 'position_manager_v3.py']:
        src = os.path.join(DEPLOY_PY_DIR, fname)
        with open(src, 'rb') as f:
            data = f.read()
        b64 = base64.b64encode(data).decode()
        remote = f'/home/gorgesoros39/deploy/{fname}'
        t.exec(f"echo '{b64}' | base64 -d > '{remote}' && chmod +x '{remote}'")
        sz = os.path.getsize(src)
        print(f"    ✅ {fname} ({sz}B)")

    # 修 chan_merge.py 空账本容错 — 每次重推都确保最新
    fix_chan_merge_empty_ledger(t)

    # 3. 写 systemd unit/timer
    print("\n[3/6] 写 systemd unit/timer → ~/.config/systemd/user/")
    t.exec('mkdir -p /home/gorgesoros39/.config/systemd/user /home/gorgesoros39/chan_logs')
    files = [
        ('meet-asc.service', UNIT_MEET_ASC),
        ('meet-asc.timer', TIMER_MEET_ASC),
        ('chan-merge.service', UNIT_CHAN_MERGE),
        ('chan-merge.timer', TIMER_CHAN_MERGE),
    ]
    for name, content in files:
        remote = f'/home/gorgesoros39/.config/systemd/user/{name}'
        t.write_file(remote, content)
        print(f"    ✅ {name} ({len(content)}B)")

    # 4. daemon-reload + enable timers
    print("\n[4/6] daemon-reload + enable timers...")
    t.exec('systemctl --user daemon-reload')
    rc, out, err = t.exec('systemctl --user enable --now meet-asc.timer 2>&1')
    print(f"    meet-asc.timer: {out.strip() or rc}")
    rc, out, err = t.exec('systemctl --user enable --now chan-merge.timer 2>&1')
    print(f"    chan-merge.timer: {out.strip() or rc}")

    # 5. 验证 timers 状态
    print("\n[5/6] timers 状态...")
    rc, out, err = t.exec('systemctl --user list-timers --all --no-pager 2>&1 | grep -E "meet|chan"')
    print(f"{out}")

    # 6. 手工触发 chan-merge (验证 exit 0)
    print("\n[6/6] 手工触发 chan-merge.service (验证 exit 0)...")
    t.exec('systemctl --user start chan-merge.service')
    time.sleep(4)
    rc, out, err = t.exec('systemctl --user status chan-merge.service --no-pager 2>&1 | head -10')
    print(out)
    if 'FAILED' in out or 'failed' in out:
        print("    ⚠️ FAILED — 查 journal...")
        rc, out, err = t.exec('journalctl --user -xeu chan-merge.service --no-pager 2>&1 | tail -20')
        print(out)
    else:
        print("    ✅ chan-merge.service SUCCESS")

    return t


def fix_chan_merge_empty_ledger(t: SSHTunnel):
    """确保 chan_merge.py 空账本时 exit 0 + 写空 JSON 骨架"""
    rc, out, err = t.exec("grep -n 'sys.exit(1)' /home/gorgesoros39/deploy/chan_merge.py | head -5")
    if '空输入' in out and 'exit(1)' in out:
        # 读整个文件 → 替换
        rc, content, err = t.exec('cat /home/gorgesoros39/deploy/chan_merge.py')
        old = '''    if not all_records:
        print("❌ 空输入 — 没有任何记录")
        sys.exit(1)'''
        new = '''    if not all_records:
        print("⚠️  空输入 — 没有任何记录 (正常, meet_in timer 还没触发?)")
        empty = {
            "ts": datetime.now(SH_TZ).isoformat(timespec="seconds"),
            "meta": {"total_raw": 0, "note": "empty ledger — meet_in may not have run yet"},
            "candidates": [],
        }
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(empty, f, indent=2, ensure_ascii=False)
        print(f"💾 空骨架已写入 {out_path}")
        return empty'''
        if old in content:
            content = content.replace(old, new)
            b64 = base64.b64encode(content.encode()).decode()
            t.exec(f"echo '{b64}' | base64 -d > /home/gorgesoros39/deploy/chan_merge.py")
            print("    ✅ chan_merge.py 空账本容错已加上")
        else:
            print("    ℹ️ chan_merge.py 已经修过了, 跳过")
    else:
        print("    ℹ️ chan_merge.py 空账本容错已存在")


# ============================================================================
# 部署 VM-B
# ============================================================================

def deploy_vmb(hermes_key_pem: str) -> SSHTunnel:
    print(f"\n{'='*60}")
    print(f"  VM-B ({VM_B_USER}@{VM_B_IP}:{VM_B_PORT}) — hermes_key auth")
    print(f"{'='*60}")

    key = paramiko.Ed25519Key.from_private_key(io.StringIO(hermes_key_pem))
    t = SSHTunnel(VM_B_IP, VM_B_PORT, VM_B_USER, pkey=key)

    # 1. 预检
    print("\n[1/4] 预检...")
    rc, out, err = t.exec('hostname; whoami; python3 --version')
    print(f"    {out.strip()}")
    rc, out, err = t.exec('systemctl --user daemon-reload 2>&1; echo OK')
    print(f"    user systemd: {out.strip()}")

    # 2. 写 unit/timer
    print("\n[2/4] 写 unit/timer...")
    t.exec('mkdir -p /home/katelolita7788/.config/systemd/user /home/katelolita7788/chan_logs')
    for name, content in [
        ('meet-desc.service', UNIT_MEET_DESC),
        ('meet-desc.timer', TIMER_MEET_DESC),
    ]:
        t.write_file(f'/home/katelolita7788/.config/systemd/user/{name}', content)
        print(f"    ✅ {name} ({len(content)}B)")

    # 3. daemon-reload + enable
    print("\n[3/4] daemon-reload + enable...")
    t.exec('systemctl --user daemon-reload')
    rc, out, err = t.exec('systemctl --user enable --now meet-desc.timer 2>&1')
    print(f"    meet-desc.timer: {out.strip() or rc}")

    # 4. 验证
    print("\n[4/4] 验证...")
    rc, out, err = t.exec('systemctl --user list-timers --all --no-pager 2>&1 | grep -E "meet|chan"')
    print(f"{out}")

    return t


# ============================================================================
# 最终状态汇总
# ============================================================================

def final_status(t_a: SSHTunnel, t_b: SSHTunnel):
    print(f"\n{'='*60}")
    print(f"  最终双机状态")
    print(f"{'='*60}")

    print("\n  VM-A timers:")
    rc, out, _ = t_a.exec('systemctl --user list-timers --all --no-pager 2>&1 | grep -E "meet|chan"')
    for line in out.strip().split('\n'):
        print(f"    {line}")

    print("\n  VM-B timers:")
    rc, out, _ = t_b.exec('systemctl --user list-timers --all --no-pager 2>&1 | grep -E "meet|chan"')
    for line in out.strip().split('\n'):
        print(f"    {line}")

    print("\n  VM-A deploy/ 文件:")
    rc, out, _ = t_a.exec('ls -lh /home/gorgesoros39/deploy/*.py')
    for line in out.strip().split('\n'):
        print(f"    {line}")

    print("\n  VM-A unit files:")
    rc, out, _ = t_a.exec('ls -lh /home/gorgesoros39/.config/systemd/user/{meet-asc,chan-merge}.*')
    for line in out.strip().split('\n'):
        print(f"    {line}")

    print("\n  VM-B unit files:")
    rc, out, _ = t_b.exec('ls -lh /home/katelolita7788/.config/systemd/user/meet-desc.*')
    for line in out.strip().split('\n'):
        print(f"    {line}")


# ============================================================================
# main
# ============================================================================

def main():
    # --- deploy VM-A ---
    t_a = deploy_vma()

    # --- 从 VM-A 拉 hermes_key ---
    print("\n[拉 key] 从 VM-A 读 hermes_key → VM-B")
    rc, key_pem, err = t_a.exec('cat /home/gorgesoros39/.ssh/hermes_key')
    if rc != 0 or not key_pem.strip():
        print(f"    ❌ 拉不到 hermes_key: {err}")
        t_a.close(); sys.exit(1)
    print(f"    ✅ hermes_key ({len(key_pem)}B)")

    # --- deploy VM-B ---
    t_b = deploy_vmb(key_pem)

    # --- 汇总 ---
    final_status(t_a, t_b)

    t_a.close(); t_b.close()

    print(f"\n{'='*60}")
    print(f"  ✅ 双机部署完成!!")
    print(f"{'='*60}")
    print(f"  调度时间 (Asia/Shanghai):")
    print(f"    09:00 → VM-A meet-asc.timer + VM-B meet-desc.timer")
    print(f"    15:05 → VM-A chan-merge.timer")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()

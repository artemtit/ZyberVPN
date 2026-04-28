#!/usr/bin/env python3
import sys, time, paramiko

HOST = "2.26.141.181"
USER = "root"
PASSWORD = "kRc68xWvqQBF"


def ssh_connect():
    for attempt in range(5):
        try:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(HOST, username=USER, password=PASSWORD, timeout=30)
            return c
        except Exception as e:
            print(f"[connect attempt {attempt+1} failed: {e}]")
            time.sleep(4)
    raise RuntimeError("Could not connect")


def run(ssh, cmd, timeout=60):
    print(f"\n$ {cmd}")
    transport = ssh.get_transport()
    chan = transport.open_session()
    chan.set_combine_stderr(True)
    chan.exec_command(cmd)
    out = []
    start = time.time()
    while True:
        if chan.recv_ready():
            data = chan.recv(4096).decode(errors="replace")
            print(data, end="", flush=True)
            out.append(data)
        elif chan.exit_status_ready():
            while chan.recv_ready():
                data = chan.recv(4096).decode(errors="replace")
                print(data, end="", flush=True)
                out.append(data)
            break
        elif time.time() - start > timeout:
            print(f"\n[timeout {timeout}s]")
            chan.close()
            return -1, "".join(out)
        else:
            time.sleep(0.2)
    code = chan.recv_exit_status()
    chan.close()
    return code, "".join(out)


def main():
    print("=" * 60)
    print("STEP 4c — resolve conflicting nginx configs on port 443")
    print("=" * 60)
    ssh = ssh_connect()

    # Show what's actually enabled
    run(ssh, "ls -la /etc/nginx/sites-enabled/")
    _, enabled_sub = run(ssh, "ls /etc/nginx/sites-enabled/ | grep -v sub.zybervpn || true")
    # The 'sub' config proxies to 2096 (wrong). Disable it.
    run(ssh, "test -e /etc/nginx/sites-enabled/sub && echo EXISTS || echo MISSING")
    _, check = run(ssh, "test -e /etc/nginx/sites-enabled/sub && echo EXISTS || echo MISSING")
    if "EXISTS" in check:
        print("\n[disabling conflicting 'sub' config — it proxies to wrong port 2096]")
        run(ssh, "rm -f /etc/nginx/sites-enabled/sub")

    # Verify sub.zybervpn.ru is enabled
    run(ssh, "test -e /etc/nginx/sites-enabled/sub.zybervpn.ru && echo ENABLED || echo MISSING")
    _, check2 = run(ssh, "test -e /etc/nginx/sites-enabled/sub.zybervpn.ru && echo ENABLED || echo MISSING")
    if "MISSING" in check2:
        run(ssh, "ln -sf /etc/nginx/sites-available/sub.zybervpn.ru /etc/nginx/sites-enabled/sub.zybervpn.ru")

    # Final config state
    run(ssh, "cat /etc/nginx/sites-available/sub.zybervpn.ru")

    print("\n" + "=" * 60)
    print("STEP 5 — nginx -t && reload")
    print("=" * 60)
    code, _ = run(ssh, "nginx -t && systemctl reload nginx")
    ssh.close()
    if code != 0:
        print("[nginx FAILED]")
        return

    time.sleep(3)

    print("\n" + "=" * 60)
    print("STEP 6 — verify TLS cert on port 443")
    print("=" * 60)
    ssh = ssh_connect()
    run(ssh, "curl -sI --max-time 10 https://sub.zybervpn.ru/healthz | head -10")
    run(ssh, (
        "echo | openssl s_client -connect sub.zybervpn.ru:443 "
        "-servername sub.zybervpn.ru 2>/dev/null "
        "| openssl x509 -noout -subject -issuer 2>/dev/null || true"
    ))
    ssh.close()

    print("\n" + "=" * 60)
    print("STEP 7 — check/fix PUBLIC_BASE_URL in /opt/zybervpn/.env")
    print("=" * 60)
    ssh = ssh_connect()
    _, env_out = run(ssh, "grep PUBLIC_BASE_URL /opt/zybervpn/.env")
    if ":8443" in env_out:
        print("[fixing — removing :8443]")
        run(ssh, "sed -i 's|PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=https://sub.zybervpn.ru|' /opt/zybervpn/.env")
    run(ssh, "grep PUBLIC_BASE_URL /opt/zybervpn/.env")
    ssh.close()

    print("\n" + "=" * 60)
    print("STEP 8 — restart Docker bot")
    print("=" * 60)
    ssh = ssh_connect()
    run(ssh, "cd /opt/zybervpn && docker compose restart", timeout=40)
    time.sleep(6)
    run(ssh, "cd /opt/zybervpn && docker compose ps")
    run(ssh, "cd /opt/zybervpn && docker compose logs --tail=10 --no-color")
    ssh.close()

    print("\n[ALL DONE]")


if __name__ == "__main__":
    main()

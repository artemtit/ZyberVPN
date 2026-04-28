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
    print("Diagnose: what is listening on port 443")
    print("=" * 60)
    ssh = ssh_connect()
    run(ssh, "ss -tlnp | grep ':443 ' || netstat -tlnp 2>/dev/null | grep ':443 ' || true")
    run(ssh, "ss -tlnp | grep ':8443' || true")
    ssh.close()

    print("\n" + "=" * 60)
    print("Revert nginx config: remove listen 443 (xray owns that port)")
    print("=" * 60)
    ssh = ssh_connect()

    # Remove the 'listen 443 ssl;' line we added — it conflicts with xray
    run(ssh, "sed -i '/listen 443 ssl;$/d' /etc/nginx/sites-available/sub.zybervpn.ru")
    run(ssh, "cat /etc/nginx/sites-available/sub.zybervpn.ru")

    run(ssh, "nginx -t && systemctl reload nginx")
    ssh.close()

    print("\n" + "=" * 60)
    print("Verify port 8443 still works")
    print("=" * 60)
    time.sleep(2)
    ssh = ssh_connect()
    run(ssh, "curl -sI --max-time 10 https://sub.zybervpn.ru:8443/healthz | head -5")
    run(ssh, (
        "echo | openssl s_client -connect sub.zybervpn.ru:8443 "
        "-servername sub.zybervpn.ru 2>/dev/null "
        "| openssl x509 -noout -subject -issuer 2>/dev/null || true"
    ))
    ssh.close()

    print("\n" + "=" * 60)
    print("Fix PUBLIC_BASE_URL to use :8443 (the working port)")
    print("=" * 60)
    ssh = ssh_connect()
    run(ssh, "sed -i 's|PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=https://sub.zybervpn.ru:8443|' /opt/zybervpn/.env")
    run(ssh, "grep PUBLIC_BASE_URL /opt/zybervpn/.env")

    print("\n" + "=" * 60)
    print("Restart bot to pick up new PUBLIC_BASE_URL")
    print("=" * 60)
    run(ssh, "cd /opt/zybervpn && docker compose restart", timeout=40)
    time.sleep(5)
    run(ssh, "cd /opt/zybervpn && docker compose ps")
    run(ssh, "cd /opt/zybervpn && docker compose logs --tail=8 --no-color")
    ssh.close()

    print("\n[DONE — subscription URLs will use https://sub.zybervpn.ru:8443]")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import sys, time, paramiko

HOST = "2.26.141.181"
USER = "root"
PASSWORD = "kRc68xWvqQBF"


def ssh_connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PASSWORD, timeout=30)
    return c


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
    # ------------------------------------------------------------------ #
    # Step 4 fix: show full config, then add listen 443 ssl if missing   #
    # ------------------------------------------------------------------ #
    print("=" * 60)
    print("STEP 4b — show full sub.zybervpn.ru config, add listen 443")
    print("=" * 60)
    ssh = ssh_connect()

    _, cat_out = run(ssh, "cat /etc/nginx/sites-available/sub.zybervpn.ru")

    if "listen 443" not in cat_out:
        print("\n[adding listen 443 ssl; to sub.zybervpn.ru server block with 8443]")
        # Replace 'listen 8443 ssl;' → add 'listen 443 ssl;' before it
        run(ssh, (
            "sed -i '/listen 8443 ssl;/i\\    listen 443 ssl;' "
            "/etc/nginx/sites-available/sub.zybervpn.ru"
        ))
        run(ssh, "cat /etc/nginx/sites-available/sub.zybervpn.ru")
    else:
        print("[listen 443 already present]")

    # Also check /etc/nginx/sites-available/sub
    _, cat_sub = run(ssh, "cat /etc/nginx/sites-available/sub")
    if "sub.zybervpn.ru" in cat_sub and "listen 443" not in cat_sub:
        print("\n[also adding listen 443 ssl; to /etc/nginx/sites-available/sub]")
        run(ssh, (
            "sed -i '/listen 8443 ssl;/i\\    listen 443 ssl;' "
            "/etc/nginx/sites-available/sub"
        ))

    ssh.close()

    # ------------------------------------------------------------------ #
    print("\n" + "=" * 60)
    print("STEP 5 — nginx -t && reload")
    print("=" * 60)
    ssh = ssh_connect()
    code, _ = run(ssh, "nginx -t && systemctl reload nginx")
    ssh.close()
    if code != 0:
        print("[nginx test FAILED — aborting]")
        return

    # ------------------------------------------------------------------ #
    print("\n" + "=" * 60)
    print("STEP 6 — verify TLS cert on port 443")
    print("=" * 60)
    time.sleep(2)
    ssh = ssh_connect()
    run(ssh, "curl -sv --max-time 10 https://sub.zybervpn.ru/healthz 2>&1 | head -30")
    run(ssh, (
        "echo | openssl s_client -connect sub.zybervpn.ru:443 "
        "-servername sub.zybervpn.ru 2>/dev/null "
        "| openssl x509 -noout -subject -issuer 2>/dev/null || true"
    ))
    ssh.close()

    # ------------------------------------------------------------------ #
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

    # ------------------------------------------------------------------ #
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

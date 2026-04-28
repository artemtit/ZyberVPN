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
    print("=" * 60)
    print("STEP 1 — nginx config for sub.zybervpn.ru")
    print("=" * 60)
    ssh = ssh_connect()
    _, out1 = run(ssh, "nginx -T 2>/dev/null | grep -B 5 -A 40 'sub.zybervpn'")
    ssh.close()

    print("\n" + "=" * 60)
    print("STEP 2 — existing certbot certificates")
    print("=" * 60)
    ssh = ssh_connect()
    _, out2 = run(ssh, "certbot certificates 2>/dev/null | grep -A 6 'zybervpn' || echo 'no zybervpn cert found'")
    ssh.close()

    need_cert = "sub.zybervpn.ru" not in out2 or "VALID" not in out2.upper()
    print(f"\n[need_cert={need_cert}]")

    if need_cert:
        print("\n" + "=" * 60)
        print("STEP 3 — obtain certificate via certbot --nginx")
        print("=" * 60)
        ssh = ssh_connect()
        code, _ = run(ssh,
            "certbot certonly --nginx -d sub.zybervpn.ru --non-interactive "
            "--agree-tos --email admin@zybervpn.ru",
            timeout=120)
        ssh.close()
        if code != 0:
            print("\n[certbot failed — trying standalone mode]")
            ssh = ssh_connect()
            run(ssh, "systemctl stop nginx || true")
            run(ssh,
                "certbot certonly --standalone -d sub.zybervpn.ru --non-interactive "
                "--agree-tos --email admin@zybervpn.ru",
                timeout=120)
            run(ssh, "systemctl start nginx || true")
            ssh.close()

    print("\n" + "=" * 60)
    print("STEP 4 — find nginx site config and fix cert paths on port 443")
    print("=" * 60)
    ssh = ssh_connect()

    # Find which config file handles sub.zybervpn.ru
    _, cfg_file = run(ssh, "grep -rl 'sub.zybervpn' /etc/nginx/ 2>/dev/null || echo ''")
    cfg_file = cfg_file.strip()
    print(f"[config file(s): {cfg_file!r}]")

    if not cfg_file:
        # No existing config — create one
        print("[no config found — creating /etc/nginx/sites-available/sub.zybervpn.ru]")
        nginx_conf = r"""server {
    listen 80;
    server_name sub.zybervpn.ru;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name sub.zybervpn.ru;

    ssl_certificate     /etc/letsencrypt/live/sub.zybervpn.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/sub.zybervpn.ru/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
"""
        sftp = ssh.open_sftp()
        with sftp.open("/etc/nginx/sites-available/sub.zybervpn.ru", "w") as f:
            f.write(nginx_conf)
        sftp.close()
        run(ssh, "ln -sf /etc/nginx/sites-available/sub.zybervpn.ru /etc/nginx/sites-enabled/sub.zybervpn.ru")
    else:
        # Patch the existing config file(s)
        for fpath in cfg_file.splitlines():
            fpath = fpath.strip()
            if not fpath:
                continue
            print(f"[patching {fpath}]")
            # Show current cert lines
            run(ssh, f"grep -n 'ssl_certificate' {fpath} || echo 'no ssl_certificate lines'")
            # Replace cert paths
            run(ssh, (
                f"sed -i "
                f"'s|ssl_certificate .*|ssl_certificate /etc/letsencrypt/live/sub.zybervpn.ru/fullchain.pem;|g' "
                f"{fpath}"
            ))
            run(ssh, (
                f"sed -i "
                f"'s|ssl_certificate_key .*|ssl_certificate_key /etc/letsencrypt/live/sub.zybervpn.ru/privkey.pem;|g' "
                f"{fpath}"
            ))
            # Show result
            run(ssh, f"grep -n 'ssl_certificate' {fpath}")

    ssh.close()

    print("\n" + "=" * 60)
    print("STEP 5 — nginx -t && reload")
    print("=" * 60)
    ssh = ssh_connect()
    code, _ = run(ssh, "nginx -t && systemctl reload nginx")
    ssh.close()
    if code != 0:
        print("[nginx test FAILED — aborting]")
        return

    print("\n" + "=" * 60)
    print("STEP 6 — verify https://sub.zybervpn.ru")
    print("=" * 60)
    ssh = ssh_connect()
    run(ssh, "curl -sI --max-time 10 https://sub.zybervpn.ru/healthz | head -10")
    run(ssh, "echo | openssl s_client -connect sub.zybervpn.ru:443 -servername sub.zybervpn.ru 2>/dev/null | openssl x509 -noout -subject -issuer 2>/dev/null || true")
    ssh.close()

    print("\n" + "=" * 60)
    print("STEP 7 — check/fix PUBLIC_BASE_URL in /opt/zybervpn/.env")
    print("=" * 60)
    ssh = ssh_connect()
    _, env_out = run(ssh, "grep PUBLIC_BASE_URL /opt/zybervpn/.env")
    if ":8443" in env_out or "8443" in env_out:
        print("[fixing PUBLIC_BASE_URL — removing :8443]")
        run(ssh, "sed -i 's|PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=https://sub.zybervpn.ru|' /opt/zybervpn/.env")
        run(ssh, "grep PUBLIC_BASE_URL /opt/zybervpn/.env")
    else:
        print("[PUBLIC_BASE_URL already correct]")
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

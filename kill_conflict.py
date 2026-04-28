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
            print(f"[retry {attempt+1}: {e}]")
            time.sleep(4)
    raise RuntimeError("connect failed")


def run(ssh, cmd, timeout=30):
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
            chan.close()
            return -1, "".join(out)
        else:
            time.sleep(0.2)
    code = chan.recv_exit_status()
    chan.close()
    return code, "".join(out)


def main():
    ssh = ssh_connect()

    print("=== ALL PROCESSES WITH app.main ===")
    run(ssh, "ps aux | grep 'app.main' | grep -v grep || echo none")

    print("\n=== ALL PYTHON PROCESSES ===")
    run(ssh, "ps aux | grep python | grep -v grep")

    print("\n=== SYSTEMD SERVICES (vpn/bot/zyber) ===")
    run(ssh, "systemctl list-units --type=service --state=running | grep -iE 'vpn|bot|zyber|tg' || echo none")

    print("\n=== STOPPING ALL non-Docker app.main instances ===")
    # Kill anything running app.main that is NOT inside Docker (not the python in /opt)
    run(ssh, "ps aux | grep 'app.main' | grep -v grep | grep -v 'docker\\|containerd'")
    run(ssh, "pkill -f '/root/ZyberVPN' 2>/dev/null && echo 'killed /root/ZyberVPN' || echo 'none'")
    run(ssh, "pkill -9 -f '/root/ZyberVPN' 2>/dev/null || true")
    # Disable & stop the systemd service permanently
    run(ssh, "systemctl stop zybervpn 2>/dev/null || true")
    run(ssh, "systemctl disable zybervpn 2>/dev/null || true")
    run(ssh, "systemctl mask zybervpn 2>/dev/null || true")

    print("\n=== REMAINING PROCESSES AFTER KILL ===")
    run(ssh, "ps aux | grep 'app.main' | grep -v grep || echo none")

    print("\n=== WAITING 5s THEN CHECK BOT LOGS ===")
    time.sleep(5)
    run(ssh, "cd /opt/zybervpn && docker compose logs --tail=15 --no-color")

    ssh.close()


if __name__ == "__main__":
    main()

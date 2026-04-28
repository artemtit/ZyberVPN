#!/usr/bin/env python3
import sys, time, paramiko

HOST = "2.26.141.181"
USER = "root"
PASSWORD = "kRc68xWvqQBF"
DEPLOY_DIR = "/opt/zybervpn"


def run(ssh, cmd, timeout=30):
    print(f"\n$ {cmd}")
    transport = ssh.get_transport()
    chan = transport.open_session()
    chan.set_combine_stderr(True)
    chan.exec_command(cmd)
    output = []
    start = time.time()
    while True:
        if chan.recv_ready():
            data = chan.recv(4096).decode(errors="replace")
            print(data, end="", flush=True)
            output.append(data)
        elif chan.exit_status_ready():
            while chan.recv_ready():
                data = chan.recv(4096).decode(errors="replace")
                print(data, end="", flush=True)
                output.append(data)
            break
        elif time.time() - start > timeout:
            break
        else:
            time.sleep(0.2)
    code = chan.recv_exit_status()
    chan.close()
    return code, "".join(output)


def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"Connecting to {HOST}...")
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=30)
    print("Connected.\n")

    # Kill the old bare-metal bot process
    print("=== KILLING OLD /root/ZyberVPN PROCESS ===")
    run(ssh, "pkill -f '/root/ZyberVPN' && echo killed || echo not_found")

    # Check for systemd service that might restart it
    print("\n=== CHECKING SYSTEMD SERVICES ===")
    run(ssh, "systemctl list-units --type=service | grep -iE 'vpn|bot|zyber' || echo none")
    run(ssh, "systemctl disable zybervpn 2>/dev/null || true")
    run(ssh, "systemctl stop zybervpn 2>/dev/null || true")
    run(ssh, "ls /etc/systemd/system/ | grep -iE 'vpn|bot|zyber' || echo no_service_files")

    # Check crontab
    print("\n=== CRONTAB ===")
    run(ssh, "crontab -l 2>/dev/null || echo empty")

    # Verify only Docker process remains
    print("\n=== REMAINING PYTHON PROCESSES ===")
    run(ssh, "ps aux | grep 'app.main' | grep -v grep || echo none")

    # Wait a moment and check logs
    time.sleep(8)
    print("\n=== LOGS AFTER KILL ===")
    run(ssh, f"cd {DEPLOY_DIR} && docker compose logs --tail=15 --no-color")

    ssh.close()


if __name__ == "__main__":
    main()

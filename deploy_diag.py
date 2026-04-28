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

    # All running processes that might be the bot
    print("=== ALL PYTHON PROCESSES (host) ===")
    run(ssh, "ps aux | grep python | grep -v grep || echo none")

    # Any other docker containers
    print("\n=== ALL DOCKER CONTAINERS ===")
    run(ssh, "docker ps -a")

    # Check what api_url is set in .env on server
    print("\n=== XUI_BASE_URL in .env ===")
    run(ssh, f"grep XUI_BASE_URL {DEPLOY_DIR}/.env")

    # Restart container so it picks up fresh Supabase data
    print("\n=== RESTARTING CONTAINER ===")
    run(ssh, f"cd {DEPLOY_DIR} && docker compose restart", timeout=30)
    time.sleep(8)

    # Fresh logs
    print("\n=== LOGS AFTER RESTART ===")
    run(ssh, f"cd {DEPLOY_DIR} && docker compose logs --tail=20 --no-color", timeout=30)

    ssh.close()


if __name__ == "__main__":
    main()

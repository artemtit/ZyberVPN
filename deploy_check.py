#!/usr/bin/env python3
import sys
import time
import paramiko

HOST = "2.26.141.181"
USER = "root"
PASSWORD = "kRc68xWvqQBF"
DEPLOY_DIR = "/opt/zybervpn"


def run(ssh, cmd, timeout=60):
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
            safe = data.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace")
            print(safe, end="", flush=True)
            output.append(data)
        elif chan.exit_status_ready():
            while chan.recv_ready():
                data = chan.recv(4096).decode(errors="replace")
                safe = data.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace")
                print(safe, end="", flush=True)
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

    run(ssh, f"cd {DEPLOY_DIR} && docker compose ps")
    run(ssh, f"cd {DEPLOY_DIR} && docker compose logs --tail=50 --no-color")

    ssh.close()


if __name__ == "__main__":
    main()

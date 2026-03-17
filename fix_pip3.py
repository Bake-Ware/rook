import paramiko
import sys
import time

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('192.168.1.168', username='root', password='Jamison1129!', timeout=10)

# Wait for the lock to release, then install
print("Waiting for apt lock and installing...", flush=True)
si, so, se = client.exec_command('while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do sleep 2; done; DEBIAN_FRONTEND=noninteractive apt-get install -y python3.11-venv python3-pip 2>&1 | tail -10')
so.channel.settimeout(300)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

# Recreate venv and install websockets
print("Setting up venv + websockets...", flush=True)
si, so, se = client.exec_command('rm -rf /root/.rook-venv && python3 -m venv /root/.rook-venv && /root/.rook-venv/bin/pip install websockets 2>&1')
so.channel.settimeout(60)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

# Restart
print("Restarting rook...", flush=True)
si, so, se = client.exec_command('systemctl restart rook')
so.channel.settimeout(10)
so.channel.recv_exit_status()

time.sleep(5)

# Check
si, so, se = client.exec_command('systemctl is-active rook 2>&1; echo "---"; journalctl -u rook -n 10 --no-pager 2>&1')
so.channel.settimeout(10)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

client.close()

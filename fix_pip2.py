import paramiko
import sys
import time

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('192.168.1.168', username='root', password='Jamison1129!', timeout=10)

# Step 1: apt install
print("Installing python3-pip and python3-venv...", flush=True)
si, so, se = client.exec_command('DEBIAN_FRONTEND=noninteractive apt-get install -y python3-pip python3-venv 2>&1 | tail -5')
so.channel.settimeout(300)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

# Step 2: recreate venv and install websockets
print("Setting up venv...", flush=True)
si, so, se = client.exec_command('rm -rf /root/.rook-venv && python3 -m venv /root/.rook-venv && /root/.rook-venv/bin/pip install websockets 2>&1')
so.channel.settimeout(60)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

# Step 3: restart service
print("Restarting rook...", flush=True)
si, so, se = client.exec_command('systemctl restart rook')
so.channel.settimeout(10)
so.channel.recv_exit_status()

time.sleep(4)

# Step 4: check
si, so, se = client.exec_command('systemctl status rook 2>&1 | head -15; echo "---"; journalctl -u rook -n 10 --no-pager 2>&1')
so.channel.settimeout(10)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

client.close()

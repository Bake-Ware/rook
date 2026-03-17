import paramiko
import sys
import time

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('192.168.1.168', username='root', password='Jamison1129!', timeout=10)

# Fix broken dpkg state
print("Fixing dpkg...", flush=True)
si, so, se = client.exec_command('dpkg --configure -a 2>&1; echo "EXIT: $?"')
so.channel.settimeout(120)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

# Fix broken installs
print("Fixing broken installs...", flush=True)
si, so, se = client.exec_command('DEBIAN_FRONTEND=noninteractive apt-get -f install -y 2>&1 | tail -5; echo "EXIT: $?"')
so.channel.settimeout(120)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

# Now install python3.11-venv
print("Installing python3.11-venv...", flush=True)
si, so, se = client.exec_command('DEBIAN_FRONTEND=noninteractive apt-get install -y python3.11-venv 2>&1 | tail -10; echo "EXIT: $?"')
so.channel.settimeout(180)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

# Verify
si, so, se = client.exec_command('dpkg -l python3.11-venv 2>&1 | tail -2')
so.channel.settimeout(10)
so.channel.recv_exit_status()
print("Verify:", so.read().decode(), flush=True)

# Create venv
print("Creating venv...", flush=True)
si, so, se = client.exec_command('rm -rf /root/.rook-venv && python3 -m venv /root/.rook-venv 2>&1 && echo VENV_OK || echo VENV_FAIL')
so.channel.settimeout(30)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

# Install websockets
print("Installing websockets...", flush=True)
si, so, se = client.exec_command('/root/.rook-venv/bin/pip install websockets 2>&1 | tail -3')
so.channel.settimeout(60)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

# Restart rook
si, so, se = client.exec_command('systemctl restart rook')
so.channel.settimeout(10)
so.channel.recv_exit_status()
time.sleep(5)

si, so, se = client.exec_command('systemctl is-active rook 2>&1')
so.channel.settimeout(10)
so.channel.recv_exit_status()
print("Service:", so.read().decode().strip(), flush=True)

client.close()

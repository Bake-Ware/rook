import paramiko
import sys
import time

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('192.168.1.168', username='root', password='Jamison1129!', timeout=10)

# Kill everything apt related, remove locks
print("Nuking apt locks...", flush=True)
si, so, se = client.exec_command('kill -9 $(pgrep -f apt) 2>/dev/null; sleep 1; rm -f /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/cache/apt/archives/lock; dpkg --configure -a 2>&1 | tail -3; echo "LOCKS CLEARED"')
so.channel.settimeout(30)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

# Install
print("Installing python3.11-venv...", flush=True)
si, so, se = client.exec_command('DEBIAN_FRONTEND=noninteractive apt-get install -y python3.11-venv 2>&1 | tail -10')
so.channel.settimeout(180)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

# Recreate venv + websockets
print("Venv + websockets...", flush=True)
si, so, se = client.exec_command('rm -rf /root/.rook-venv && python3 -m venv /root/.rook-venv && /root/.rook-venv/bin/pip install websockets 2>&1 | tail -5')
so.channel.settimeout(60)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

# Restart
si, so, se = client.exec_command('systemctl restart rook')
so.channel.settimeout(10)
so.channel.recv_exit_status()
time.sleep(5)

si, so, se = client.exec_command('systemctl is-active rook; journalctl -u rook -n 5 --no-pager 2>&1')
so.channel.settimeout(10)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

client.close()

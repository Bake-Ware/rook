import paramiko
import sys
import time

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('192.168.1.168', username='root', password='Jamison1129!', timeout=10)

# Check if apt lock is still held
si, so, se = client.exec_command('fuser /var/lib/dpkg/lock-frontend 2>&1; ps aux | grep apt | grep -v grep')
so.channel.settimeout(10)
so.channel.recv_exit_status()
print("APT status:", so.read().decode(), flush=True)

# Check what the worker script does re: venv - find the venv setup section
si, so, se = client.exec_command('grep -n "venv\\|ensurepip\\|websockets" /opt/rook_worker.py | head -20')
so.channel.settimeout(10)
so.channel.recv_exit_status()
print("Worker venv lines:", so.read().decode(), flush=True)

# Download websockets as a standalone .whl using wget/curl and extract manually
# Or better: use get-pip.py to bootstrap pip
print("Bootstrapping pip...", flush=True)
si, so, se = client.exec_command('curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py && python3 /tmp/get-pip.py --break-system-packages 2>&1 | tail -5')
so.channel.settimeout(60)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

# Now install websockets system-wide
print("Installing websockets...", flush=True)
si, so, se = client.exec_command('python3 -m pip install websockets --break-system-packages 2>&1 | tail -5')
so.channel.settimeout(30)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

# Now patch the worker to skip venv - just make the venv a symlink to system python
print("Creating fake venv...", flush=True)
si, so, se = client.exec_command('rm -rf /root/.rook-venv; mkdir -p /root/.rook-venv/bin; ln -sf /usr/bin/python3 /root/.rook-venv/bin/python3; echo OK')
so.channel.settimeout(10)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

# Restart rook
si, so, se = client.exec_command('systemctl restart rook')
so.channel.settimeout(10)
so.channel.recv_exit_status()
time.sleep(5)

si, so, se = client.exec_command('systemctl is-active rook; journalctl -u rook -n 8 --no-pager 2>&1')
so.channel.settimeout(10)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

client.close()

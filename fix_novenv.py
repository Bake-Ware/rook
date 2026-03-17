import paramiko
import sys
import time

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('192.168.1.168', username='root', password='Jamison1129!', timeout=10)

# Skip venv entirely. Install websockets into system python via pip or manually
# First check if pip works at all
print("Checking pip...", flush=True)
si, so, se = client.exec_command('python3 -m pip install websockets 2>&1 | tail -5')
so.channel.settimeout(30)
exit_code = so.channel.recv_exit_status()
print(so.read().decode(), flush=True)
print(f"Exit: {exit_code}", flush=True)

if exit_code != 0:
    # Try downloading websockets wheel manually
    print("Trying manual websockets install...", flush=True)
    si, so, se = client.exec_command('python3 -c "import websockets; print(websockets.__version__)" 2>&1')
    so.channel.settimeout(10)
    so.channel.recv_exit_status()
    result = so.read().decode().strip()
    print(f"websockets import: {result}", flush=True)
    
    if "No module" in result or "Error" in result:
        # Install via pip with --break-system-packages
        si, so, se = client.exec_command('python3 -m pip install websockets --break-system-packages 2>&1 | tail -5')
        so.channel.settimeout(30)
        so.channel.recv_exit_status()
        print(so.read().decode(), flush=True)

# Now modify the service to use system python directly instead of venv
service_unit = """[Unit]
Description=Rook Remote Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/rook_worker.py --server ws://192.168.1.209:7005/ws --name starscream --no-venv
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""

# Check if --no-venv flag exists
si, so, se = client.exec_command('grep -c "no.venv\\|no_venv" /opt/rook_worker.py 2>&1')
so.channel.settimeout(5)
so.channel.recv_exit_status()
has_novenv = so.read().decode().strip()
print(f"has --no-venv: {has_novenv}", flush=True)

client.close()

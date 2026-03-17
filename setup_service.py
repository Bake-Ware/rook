import paramiko
import sys

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('192.168.1.168', username='root', password='Jamison1129!', timeout=10)

# Check python3
si, so, se = client.exec_command('which python3 && python3 --version')
so.channel.settimeout(10)
so.channel.recv_exit_status()
print('Python:', so.read().decode().strip(), flush=True)

# Create systemd service
service_unit = """[Unit]
Description=Rook Remote Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/rook_worker.py --server wss://rook.bake.systems/ws --name starscream
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""

si, so, se = client.exec_command(f'cat > /etc/systemd/system/rook.service << \'HEREDOC\'\n{service_unit}HEREDOC')
so.channel.settimeout(10)
so.channel.recv_exit_status()

# Reload, enable, start
si, so, se = client.exec_command('systemctl daemon-reload && systemctl enable rook && systemctl start rook 2>&1 && echo STARTED OK')
so.channel.settimeout(10)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)
print('STDERR:', se.read().decode(), flush=True)

# Check status
import time
time.sleep(2)
si, so, se = client.exec_command('systemctl status rook 2>&1 | head -20')
so.channel.settimeout(10)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

client.close()

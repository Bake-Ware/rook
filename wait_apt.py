import paramiko
import sys
import time

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('192.168.1.168', username='root', password='Jamison1129!', timeout=10)

# Wait for apt to finish
print("Waiting for apt-get to finish...", flush=True)
si, so, se = client.exec_command('while pgrep -x apt-get > /dev/null; do sleep 5; echo -n "."; done; echo " DONE"; dpkg -l python3.11-venv 2>&1 | tail -2')
so.channel.settimeout(600)
exit_code = so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

# Now recreate venv properly and restart
print("Setting up venv...", flush=True)
si, so, se = client.exec_command('rm -rf /root/.rook-venv && python3 -m venv /root/.rook-venv 2>&1 && echo VENV_OK || echo VENV_FAIL')
so.channel.settimeout(30)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

# Install websockets in venv
print("Installing websockets in venv...", flush=True)
si, so, se = client.exec_command('/root/.rook-venv/bin/pip install websockets 2>&1 | tail -3')
so.channel.settimeout(60)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

# Restart rook
print("Restarting rook...", flush=True)
si, so, se = client.exec_command('systemctl restart rook')
so.channel.settimeout(10)
so.channel.recv_exit_status()
time.sleep(5)

si, so, se = client.exec_command('systemctl is-active rook 2>&1; echo "---"; journalctl -u rook -n 8 --no-pager 2>&1')
so.channel.settimeout(10)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

client.close()

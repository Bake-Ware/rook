import paramiko
import sys

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('192.168.1.168', username='root', password='Jamison1129!', timeout=10)

# Fix: use direct IP instead of DNS, and use ws:// not wss://
cmds = """
systemctl stop rook 2>&1
sed -i 's|wss://rook.bake.systems/ws|ws://192.168.1.209:7005/ws|' /etc/systemd/system/rook.service
systemctl daemon-reload
systemctl start rook 2>&1
echo "STARTED"
sleep 3
systemctl status rook 2>&1 | head -20
echo "---JOURNAL---"
journalctl -u rook -n 20 --no-pager 2>&1
"""

si, so, se = client.exec_command(cmds)
so.channel.settimeout(20)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

client.close()

import paramiko
import sys

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('192.168.1.168', username='root', password='Jamison1129!', timeout=10)

cmds = """
apt-get update -qq 2>&1 | tail -3
apt-get install -y python3-pip python3-venv 2>&1 | tail -5
echo "---APT DONE---"
rm -rf /root/.rook-venv
python3 -m venv /root/.rook-venv
/root/.rook-venv/bin/python3 -m pip install websockets -q 2>&1
echo "PIP EXIT: $?"
systemctl restart rook
sleep 3
systemctl status rook 2>&1 | head -15
echo "---JOURNAL---"
journalctl -u rook -n 10 --no-pager 2>&1
"""

si, so, se = client.exec_command(cmds)
so.channel.settimeout(120)
so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

client.close()

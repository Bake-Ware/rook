import paramiko
import sys

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('192.168.1.168', username='root', password='Jamison1129!', timeout=10)

# Download worker.py directly and run it as a service
cmds = """
curl -m 10 -fsSL -u bake:poop http://192.168.1.209:7005/worker.py -o /opt/rook_worker.py 2>&1
echo "DOWNLOAD EXIT: $?"
ls -la /opt/rook_worker.py 2>&1
head -5 /opt/rook_worker.py 2>&1
"""

si, so, se = client.exec_command(cmds)
so.channel.settimeout(15)
exit_status = so.channel.recv_exit_status()
print(so.read().decode(), flush=True)
print('STDERR:', se.read().decode(), flush=True)

client.close()

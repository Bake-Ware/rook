import paramiko
import sys

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('192.168.1.168', username='root', password='Jamison1129!', timeout=10)

# First check if rook.bake.systems resolves, if not use direct IP
si, so, se = client.exec_command('curl -fsSL http://192.168.1.209:7005/worker.py -o /tmp/rook_worker.py 2>&1 && echo DOWNLOAD_OK || echo DOWNLOAD_FAIL')
exit_status = so.channel.recv_exit_status()
print('Download:', so.read().decode(), flush=True)
print('Download stderr:', se.read().decode(), flush=True)

# Try the bootstrap script directly
si, so, se = client.exec_command('curl -fsSL https://rook.bake.systems 2>&1 | head -50')
exit_status = so.channel.recv_exit_status()
print('Bootstrap script:', so.read().decode(), flush=True)
print('Bootstrap stderr:', se.read().decode(), flush=True)

client.close()

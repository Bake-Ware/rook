import paramiko
import sys

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('192.168.1.168', username='root', password='Jamison1129!', timeout=10)
si, so, se = client.exec_command('echo hello world')
exit_status = so.channel.recv_exit_status()
print('exit:', exit_status, flush=True)
print('stdout:', so.read().decode(), flush=True)
print('stderr:', se.read().decode(), flush=True)
client.close()

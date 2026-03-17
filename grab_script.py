import paramiko
import sys

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('192.168.1.168', username='root', password='Jamison1129!', timeout=10)

si, so, se = client.exec_command('curl -m 10 -fsSL -u bake:poop http://192.168.1.209:7005/ 2>&1')
so.channel.settimeout(15)
exit_status = so.channel.recv_exit_status()
print(so.read().decode(), flush=True)

client.close()

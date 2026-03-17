"""Check rook service status on starscream server."""
import paramiko

def main():
    host = "192.168.1.168"
    username = "root"
    password = "Jamison1129!"
    
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=host, username=username, password=password, timeout=30)
        
        # Run the status check commands
        stdin, stdout, stderr = client.exec_command("systemctl is-active rook; journalctl -u rook -n 3 --no-pager 2>&1")
        output = stdout.read().decode('utf-8') + stderr.read().decode('utf-8')
        
        print("=== Rook Service Status on Starscream ===")
        print(output)
        
        # Check if permanently failed
        stdin2, stdout2, stderr2 = client.exec_command("systemctl show rook --property=SubState,ActiveState 2>/dev/null")
        state_output = stdout2.read().decode('utf-8')
        print("\n=== Service State Details ===")
        print(state_output)
        
        # Determine if permanently failed
        is_failed = "failed" in output.lower() and "activating" not in output.lower()
        print(f"\n=== ANALYSIS ===")
        print(f"Service appears permanently failed: {is_failed}")
        
        client.close()
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()

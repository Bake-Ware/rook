"""Disposable machines for clean-room runs.

Every machine a run uses is created fresh (a cloud image, a wiped emulator)
and destroyed afterwards. The providers refuse to touch anything they did not
create: Proxmox VMs must sit in the configured VMID range, carry the
``rook-cleanroom`` tag and a ``cleanroom-`` name; emulators must use the
configured AVD name.

Site details (hypervisor host, image path, ranges, AVD) come from the site
config, never from this file.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field

TAG = "rook-cleanroom"
NAME_PREFIX = "cleanroom-"


class ProviderError(RuntimeError):
    pass


def run(argv, *, timeout=600, check=True, input=None):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=input)
    if check and p.returncode != 0:
        raise ProviderError(f"{argv[:6]} exit {p.returncode}: {(p.stderr or p.stdout)[-800:]}")
    return p


@dataclass
class Machine:
    """A disposable Linux machine reachable over SSH."""

    name: str
    host: str
    user: str
    key: str
    vmid: int | None = None
    meta: dict = field(default_factory=dict)

    def ssh_argv(self, cmd: str, *, tty=False):
        argv = [
            "ssh", "-i", self.key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "ServerAliveInterval=15",
        ]
        if tty:
            argv.append("-t")
        return argv + [f"{self.user}@{self.host}", cmd]

    def sh(self, cmd: str, *, timeout=600, check=True, input=None):
        return run(self.ssh_argv(cmd), timeout=timeout, check=check, input=input)

    def put(self, local: str, remote: str, *, timeout=600):
        run([
            "scp", "-q", "-i", self.key, "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", local, f"{self.user}@{self.host}:{remote}",
        ], timeout=timeout)

    def write(self, remote: str, text: str, *, mode="600"):
        self.sh(f"umask 077; cat > {shlex.quote(remote)} && chmod {mode} {shlex.quote(remote)}",
                input=text)


class ProxmoxProvider:
    """Fresh VMs from a cloud image on a Proxmox host, driven over SSH (qm)."""

    def __init__(self, cfg: dict, *, log=print):
        self.host = cfg["host"]                       # ssh destination of the hypervisor
        self.image = cfg["image"]                     # cloud image path on the hypervisor
        self.storage = cfg.get("storage", "local-lvm")
        self.bridge = cfg.get("bridge", "vmbr0")
        lo, hi = cfg.get("vmid_range", [900, 919])
        self.vmid_range = (int(lo), int(hi))
        self.key = os.path.expanduser(cfg["ssh_key"])  # private key on the controller
        self.user = cfg.get("vm_user", "rook")
        self.subnet_scan = cfg.get("subnet_scan")      # e.g. "192.168.1" to find DHCP leases
        self.log = log

    # -- hypervisor plumbing ---------------------------------------------
    def _pve(self, cmd: str, *, timeout=600, check=True, input=None):
        return run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", self.host, cmd],
                   timeout=timeout, check=check, input=input)

    def _owned(self) -> dict[int, str]:
        """VMIDs in our range that carry our tag and name prefix."""
        out = self._pve("for f in /etc/pve/qemu-server/*.conf; do echo \"== $f\"; "
                        "grep -E '^(name|tags):' $f; done", check=False).stdout
        owned, cur, name, tags = {}, None, "", ""
        for line in out.splitlines() + ["== end"]:
            if line.startswith("== "):
                if cur is not None and name.startswith(NAME_PREFIX) and TAG in tags.split(";"):
                    owned[cur] = name
                m = re.search(r"/(\d+)\.conf$", line)
                cur = int(m.group(1)) if m else None
                name = tags = ""
                if cur is not None and not (self.vmid_range[0] <= cur <= self.vmid_range[1]):
                    cur = None
            elif line.startswith("name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("tags:"):
                tags = line.split(":", 1)[1].strip()
        return owned

    def _free_vmid(self) -> int:
        used = set()
        out = self._pve("ls /etc/pve/nodes/*/qemu-server/ /etc/pve/nodes/*/lxc/ 2>/dev/null").stdout
        for m in re.finditer(r"(\d+)\.conf", out):
            used.add(int(m.group(1)))
        for vmid in range(self.vmid_range[0], self.vmid_range[1] + 1):
            if vmid not in used:
                return vmid
        raise ProviderError("no free VMID in the clean-room range")

    def free_memory_mb(self) -> int:
        out = self._pve("awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo").stdout
        return int(out.strip() or 0)

    # -- lifecycle ---------------------------------------------------------
    def create(self, role: str, *, memory_mb: int, cores: int, disk_gb: int = 20) -> Machine:
        vmid = self._free_vmid()
        name = f"{NAME_PREFIX}{role}-{vmid}"
        free = self.free_memory_mb()
        if free < memory_mb + 768:
            raise ProviderError(f"hypervisor has {free} MB free; {name} needs {memory_mb} MB + headroom")
        pub = open(self.key + ".pub").read().strip()
        self.log(f"[proxmox] create {name} ({memory_mb} MB, {cores} cores)")
        self._pve(f"umask 077; cat > /tmp/{name}.pub", input=pub + "\n")
        q = shlex.quote
        self._pve(
            f"set -e; qm create {vmid} --name {q(name)} --memory {memory_mb} --cores {cores} --cpu host"
            f" --net0 virtio,bridge={q(self.bridge)} --scsihw virtio-scsi-pci --ostype l26"
            f" --serial0 socket --vga serial0 --onboot 0 --tags {TAG}"
            f" --description {q('Disposable Rook clean-room VM; the harness destroys it after the run.')};"
            f" qm importdisk {vmid} {q(self.image)} {q(self.storage)} >/dev/null;"
            f" qm set {vmid} --scsi0 {q(self.storage)}:vm-{vmid}-disk-0 --boot order=scsi0"
            f" --ide2 {q(self.storage)}:cloudinit >/dev/null;"
            f" qm resize {vmid} scsi0 {disk_gb}G >/dev/null;"
            f" qm set {vmid} --ciuser {q(self.user)} --sshkeys /tmp/{name}.pub --ipconfig0 ip=dhcp >/dev/null;"
            f" rm -f /tmp/{name}.pub; qm start {vmid}",
            timeout=900,
        )
        mac = self._pve(f"qm config {vmid} | sed -n 's/^net0: virtio=\\([^,]*\\),.*/\\1/p'").stdout.strip().lower()
        m = Machine(name=name, host="", user=self.user, key=self.key, vmid=vmid,
                    meta={"provider": "proxmox", "mac": mac, "memory_mb": memory_mb, "cores": cores})
        m.host = self._wait_ip(mac)
        self._wait_ssh(m)
        self.log(f"[proxmox] {name} up at {m.host}")
        return m

    def _wait_ip(self, mac: str, timeout=300) -> str:
        deadline = time.time() + timeout
        scan = ""
        if self.subnet_scan:
            scan = (f"for h in $(seq 2 254); do ping -c1 -W1 {self.subnet_scan}.$h >/dev/null 2>&1 & done; wait;")
        while time.time() < deadline:
            out = self._pve(f"ip neigh | awk -v m={mac} 'tolower($5)==m {{print $1}}'", check=False).stdout
            ips = [ip for ip in out.split() if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip)]
            if ips:
                return ips[0]
            time.sleep(5)
            if scan:
                self._pve(scan, check=False, timeout=120)
        raise ProviderError(f"no DHCP address seen for {mac}")

    def _wait_ssh(self, m: Machine, timeout=300):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if m.sh("cloud-init status --wait >/dev/null 2>&1; true", check=False, timeout=200).returncode == 0:
                return
            time.sleep(5)
        raise ProviderError(f"{m.name}: ssh never came up")

    def stop(self, m: Machine):
        """Hard power-off (the 'kill the machine' failure)."""
        self._check_owned(m.vmid)
        self._pve(f"qm stop {m.vmid} --skiplock 1", check=False)

    def start(self, m: Machine):
        self._check_owned(m.vmid)
        self._pve(f"qm start {m.vmid}", check=False)
        self._wait_ssh(m)

    def destroy(self, m: Machine):
        self._check_owned(m.vmid)
        self.log(f"[proxmox] destroy {m.name}")
        self._pve(f"qm stop {m.vmid} --skiplock 1 >/dev/null 2>&1; qm destroy {m.vmid} --purge 1",
                  check=False, timeout=300)

    def sweep(self):
        """Destroy every leftover clean-room VM (after a crashed run)."""
        for vmid, name in self._owned().items():
            self.log(f"[proxmox] sweep {name}")
            self._pve(f"qm stop {vmid} --skiplock 1 >/dev/null 2>&1; qm destroy {vmid} --purge 1",
                      check=False, timeout=300)

    def _check_owned(self, vmid):
        if vmid not in self._owned():
            raise ProviderError(f"refusing to act on VM {vmid}: not a clean-room VM")


class EmulatorProvider:
    """A wiped Android emulator per run (the APK side of a profile)."""

    def __init__(self, cfg: dict, *, log=print):
        self.sdk = os.path.expanduser(cfg.get("sdk", "~/Android/Sdk"))
        self.avd = cfg["avd"]
        self.port = int(cfg.get("port", 5584))
        self.serial = f"emulator-{self.port}"
        self.headless = cfg.get("headless", True)
        self.memory_mb = int(cfg.get("memory_mb", 2048))
        self.log = log
        self.proc = None

    def adb(self, *args, timeout=120, check=True):
        return run([os.path.join(self.sdk, "platform-tools", "adb"), "-s", self.serial, *args],
                   timeout=timeout, check=check)

    def create(self):
        avd_dir = os.path.expanduser(f"~/.android/avd/{self.avd}.avd")
        if not os.path.isdir(avd_dir):
            raise ProviderError(f"AVD {self.avd} not found; create it once (see README)")
        argv = [os.path.join(self.sdk, "emulator", "emulator"), "-avd", self.avd,
                "-port", str(self.port), "-wipe-data", "-no-snapshot", "-no-boot-anim",
                "-no-audio", "-memory", str(self.memory_mb)]
        if self.headless:
            argv += ["-no-window", "-gpu", "swiftshader_indirect"]
        self.log(f"[emulator] boot {self.avd} (wiped) as {self.serial}")
        logf = open(f"/tmp/{self.avd}-{self.port}.log", "w")
        self.proc = subprocess.Popen(argv, stdout=logf, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        deadline = time.time() + 300
        while time.time() < deadline:
            p = self.adb("shell", "getprop", "sys.boot_completed", check=False, timeout=20)
            if p.stdout.strip() == "1":
                self.adb("shell", "input", "keyevent", "82", check=False)
                return self
            time.sleep(4)
        raise ProviderError("emulator did not finish booting")

    def kill(self):
        self.adb("emu", "kill", check=False, timeout=30)
        if self.proc:
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, 9)
        self.proc = None

    destroy = kill

"""Windows pseudoconsoles (ConPTY) in pure ctypes, for ``work.stream.*``.

A ConPTY is the Windows counterpart of a POSIX PTY (Windows 10 1809+): the
child gets a real console, and everything it draws comes back as a VT byte
stream on a pipe, so the web terminal renders it exactly like Linux output.
No compiled dependency: the worker bundle is a plain .pyz, and kernel32 has
everything needed.

Shape (one object per terminal):

* two anonymous pipes; the pseudoconsole reads input from one and writes VT
  output to the other (``CreatePseudoConsole``),
* the child is created suspended with the pseudoconsole attribute, put into
  a Job Object with KILL_ON_JOB_CLOSE (so its children die with it), then
  resumed,
* a daemon thread reads the output pipe and hands chunks to a callback.

Signals: Ctrl-C is the byte 0x03 on the input pipe (the console turns it
into CTRL_C_EVENT for the foreground program, as a terminal would); hang-up
is ``ClosePseudoConsole`` (CTRL_CLOSE_EVENT to every attached program);
kill is ``TerminateJobObject``.

Every Win32 call goes through the ``kernel32`` object passed in, so the tests
drive this module on Linux with a fake at the ctypes boundary.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
import threading
from ctypes import POINTER, Structure, byref, c_int16, c_int32, c_int64, c_size_t, c_uint16, c_uint32, c_uint64, c_void_p, c_wchar_p, sizeof

log = logging.getLogger("rook.worker.conpty")

HANDLE = c_void_p
DWORD = c_uint32
BOOL = c_int32
HRESULT = c_int32

EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_SUSPENDED = 0x00000004
STARTF_USESTDHANDLES = 0x00000100
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9
WAIT_OBJECT_0 = 0
INFINITE = 0xFFFFFFFF
ERROR_BROKEN_PIPE = 109
MAX_CMDLINE = 32767


class COORD(Structure):
    _fields_ = [("X", c_int16), ("Y", c_int16)]


class STARTUPINFOW(Structure):
    _fields_ = [("cb", DWORD), ("lpReserved", c_wchar_p), ("lpDesktop", c_wchar_p),
                ("lpTitle", c_wchar_p), ("dwX", DWORD), ("dwY", DWORD), ("dwXSize", DWORD),
                ("dwYSize", DWORD), ("dwXCountChars", DWORD), ("dwYCountChars", DWORD),
                ("dwFillAttribute", DWORD), ("dwFlags", DWORD), ("wShowWindow", c_uint16),
                ("cbReserved2", c_uint16), ("lpReserved2", c_void_p), ("hStdInput", HANDLE),
                ("hStdOutput", HANDLE), ("hStdError", HANDLE)]


class STARTUPINFOEXW(Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", c_void_p)]


class PROCESS_INFORMATION(Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE),
                ("dwProcessId", DWORD), ("dwThreadId", DWORD)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(Structure):
    _fields_ = [("PerProcessUserTimeLimit", c_int64), ("PerJobUserTimeLimit", c_int64),
                ("LimitFlags", DWORD), ("MinimumWorkingSetSize", c_size_t),
                ("MaximumWorkingSetSize", c_size_t), ("ActiveProcessLimit", DWORD),
                ("Affinity", c_size_t), ("PriorityClass", DWORD), ("SchedulingClass", DWORD)]


class IO_COUNTERS(Structure):
    _fields_ = [(n, c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount", "WriteTransferCount",
                "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", c_size_t),
                ("JobMemoryLimit", c_size_t), ("PeakProcessMemoryUsed", c_size_t),
                ("PeakJobMemoryUsed", c_size_t)]


_PROTOTYPES = {
    "CreatePipe": (BOOL, [POINTER(HANDLE), POINTER(HANDLE), c_void_p, DWORD]),
    "CreatePseudoConsole": (HRESULT, [COORD, HANDLE, HANDLE, DWORD, POINTER(HANDLE)]),
    "ResizePseudoConsole": (HRESULT, [HANDLE, COORD]),
    "ClosePseudoConsole": (None, [HANDLE]),
    "InitializeProcThreadAttributeList": (BOOL, [c_void_p, DWORD, DWORD, POINTER(c_size_t)]),
    "UpdateProcThreadAttribute": (BOOL, [c_void_p, DWORD, c_size_t, c_void_p, c_size_t, c_void_p, c_void_p]),
    "DeleteProcThreadAttributeList": (None, [c_void_p]),
    "CreateProcessW": (BOOL, [c_wchar_p, c_void_p, c_void_p, c_void_p, BOOL, DWORD, c_void_p,
                              c_wchar_p, POINTER(STARTUPINFOEXW), POINTER(PROCESS_INFORMATION)]),
    "CreateJobObjectW": (HANDLE, [c_void_p, c_wchar_p]),
    "SetInformationJobObject": (BOOL, [HANDLE, c_int32, c_void_p, DWORD]),
    "AssignProcessToJobObject": (BOOL, [HANDLE, HANDLE]),
    "TerminateJobObject": (BOOL, [HANDLE, c_uint32]),
    "TerminateProcess": (BOOL, [HANDLE, c_uint32]),
    "ResumeThread": (DWORD, [HANDLE]),
    "ReadFile": (BOOL, [HANDLE, c_void_p, DWORD, POINTER(DWORD), c_void_p]),
    "WriteFile": (BOOL, [HANDLE, c_void_p, DWORD, POINTER(DWORD), c_void_p]),
    "WaitForSingleObject": (DWORD, [HANDLE, DWORD]),
    "GetExitCodeProcess": (BOOL, [HANDLE, POINTER(DWORD)]),
    "CloseHandle": (BOOL, [HANDLE]),
}

_k32 = None


def kernel32():
    """The real kernel32 with prototypes set (64-bit handles need them)."""
    global _k32
    if _k32 is None:
        lib = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, (restype, argtypes) in _PROTOTYPES.items():
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = restype, argtypes
        _k32 = lib
    return _k32


def available() -> bool:
    """True on Windows 10 1809+ (the first release with CreatePseudoConsole)."""
    if sys.platform != "win32":
        return False
    try:
        kernel32()
        return True
    except (OSError, AttributeError):
        return False


def _error(what: str) -> OSError:
    code = getattr(ctypes, "get_last_error", lambda: 0)()
    if hasattr(ctypes, "WinError") and code:
        err = ctypes.WinError(code)
        return OSError(err.errno, f"{what}: {err.strerror}", None, code)
    return OSError(f"{what} failed (error {code})")


def env_block(env: dict) -> str:
    """A CREATE_UNICODE_ENVIRONMENT block: sorted ``k=v\\0`` entries plus a
    terminating NUL. Entries with NUL or '=' in the name are dropped."""
    items = sorted(((str(k), str(v)) for k, v in env.items()
                    if k and "=" not in str(k)[1:] and "\0" not in f"{k}{v}"),
                   key=lambda kv: kv[0].upper())
    return "".join(f"{k}={v}\0" for k, v in items) + "\0"


def cmdline(argv: list[str]) -> str:
    """Quote argv for CreateProcessW (MSVCRT rules, like subprocess)."""
    line = subprocess.list2cmdline([str(a) for a in argv])
    if len(line) >= MAX_CMDLINE:
        raise ValueError("command line is too long for Windows")
    return line


class ConPty:
    """One child process attached to a pseudoconsole. Blocking methods are
    meant for worker threads (``asyncio.to_thread``)."""

    def __init__(self, k32) -> None:
        self.k = k32
        self.hpc = None          # HPCON, None once closed
        self.in_w = None         # we write keystrokes here
        self.out_r = None        # we read VT output here
        self.process = None
        self.thread = None
        self.job = None
        self.pid: int | None = None
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._closed = False

    # -- creation ------------------------------------------------------------

    @classmethod
    def spawn(cls, command: str, cwd: str, env: dict, cols: int = 120, rows: int = 32,
              k32=None) -> "ConPty":
        """Start ``command`` (a full Windows command line) under a new
        pseudoconsole of ``cols`` x ``rows``."""
        self = cls(k32 if k32 is not None else kernel32())
        k = self.k
        in_r, in_w, out_r, out_w = HANDLE(), HANDLE(), HANDLE(), HANDLE()
        attrs = None
        try:
            if not k.CreatePipe(byref(in_r), byref(in_w), None, 0):
                raise _error("CreatePipe")
            self.in_w = in_w.value
            if not k.CreatePipe(byref(out_r), byref(out_w), None, 0):
                raise _error("CreatePipe")
            self.out_r = out_r.value
            hpc = HANDLE()
            hr = k.CreatePseudoConsole(COORD(int(cols), int(rows)), in_r.value, out_w.value, 0, byref(hpc))
            if hr != 0:
                raise OSError(f"CreatePseudoConsole failed (HRESULT {hr & 0xFFFFFFFF:#010x})")
            self.hpc = hpc.value
            # The pseudoconsole holds its own duplicates of these two ends.
            k.CloseHandle(in_r.value)
            k.CloseHandle(out_w.value)
            in_r = out_w = None

            size = c_size_t(0)
            k.InitializeProcThreadAttributeList(None, 1, 0, byref(size))
            attrs = ctypes.create_string_buffer(max(size.value, 1))
            if not k.InitializeProcThreadAttributeList(attrs, 1, 0, byref(size)):
                raise _error("InitializeProcThreadAttributeList")
            if not k.UpdateProcThreadAttribute(attrs, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                                               c_void_p(self.hpc), sizeof(c_void_p), None, None):
                raise _error("UpdateProcThreadAttribute")

            si = STARTUPINFOEXW()
            si.StartupInfo.cb = sizeof(STARTUPINFOEXW)
            # Null std handles plus USESTDHANDLES: the child must take its
            # console from the pseudoconsole, never the worker's own
            # (redirected) stdout/stderr.
            si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
            si.lpAttributeList = ctypes.cast(attrs, c_void_p)
            pi = PROCESS_INFORMATION()
            cmd = ctypes.create_unicode_buffer(command)
            block = env_block(env)
            envbuf = ctypes.create_unicode_buffer(len(block) + 1)
            envbuf[:len(block)] = block
            flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED
            if not k.CreateProcessW(None, cmd, None, None, False, flags, envbuf, cwd,
                                    byref(si), byref(pi)):
                raise _error("CreateProcessW")
            self.process, self.thread, self.pid = pi.hProcess, pi.hThread, int(pi.dwProcessId)

            # Into a kill-on-close job before it runs a single instruction,
            # so nothing it starts can escape the terminal's lifetime.
            job = k.CreateJobObjectW(None, None)
            if job:
                self.job = job
                info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not (k.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                                  byref(info), sizeof(info))
                        and k.AssignProcessToJobObject(job, self.process)):
                    log.warning("conpty: job object unavailable; children may outlive pid %s", self.pid)
                    k.CloseHandle(job)
                    self.job = None
            if k.ResumeThread(self.thread) == 0xFFFFFFFF:
                raise _error("ResumeThread")
            return self
        except BaseException:
            for h in (in_r, out_w):
                if h is not None and h.value:
                    k.CloseHandle(h.value)
            if self.process is not None:
                self.kill()     # it may still be suspended: never leave it behind
            self._release(force=True)
            raise
        finally:
            if attrs is not None:
                k.DeleteProcThreadAttributeList(attrs)

    # -- io --------------------------------------------------------------------

    def start_reader(self, on_data, on_eof=None) -> None:
        """Read output on a daemon thread until the pipe closes."""
        def run() -> None:
            buf = ctypes.create_string_buffer(65536)
            n = DWORD(0)
            try:
                while True:
                    h = self.out_r
                    if h is None or not self.k.ReadFile(h, buf, len(buf), byref(n), None) or not n.value:
                        break
                    on_data(buf.raw[:n.value])
            except Exception:
                log.debug("conpty reader stopped", exc_info=True)
            finally:
                if on_eof is not None:
                    on_eof()
        self._reader = threading.Thread(target=run, name=f"conpty-{self.pid}", daemon=True)
        self._reader.start()

    def write(self, data: bytes) -> int:
        view = memoryview(bytes(data))
        n = DWORD(0)
        while view:
            if self.in_w is None:
                raise OSError("pseudoconsole is closed")
            chunk = bytes(view[:65536])
            if not self.k.WriteFile(self.in_w, chunk, len(chunk), byref(n), None):
                raise _error("WriteFile")
            view = view[n.value:]
        return len(data)

    def resize(self, cols: int, rows: int) -> None:
        with self._lock:
            if self.hpc is None:
                return
            hr = self.k.ResizePseudoConsole(self.hpc, COORD(int(cols), int(rows)))
        if hr != 0:
            raise OSError(f"ResizePseudoConsole failed (HRESULT {hr & 0xFFFFFFFF:#010x})")

    # -- lifetime ------------------------------------------------------------

    def wait(self, timeout: float | None = None) -> int | None:
        """The exit code, or None if still running after ``timeout`` seconds."""
        if self.process is None:
            return None
        ms = INFINITE if timeout is None else max(0, int(timeout * 1000))
        if self.k.WaitForSingleObject(self.process, ms) != WAIT_OBJECT_0:
            return None
        code = DWORD(0)
        if not self.k.GetExitCodeProcess(self.process, byref(code)):
            return -1
        return int(code.value)

    def interrupt(self) -> None:
        """Ctrl-C, the way a keyboard sends it."""
        self.write(b"\x03")

    def hangup(self) -> None:
        """Close the pseudoconsole: attached programs get CTRL_CLOSE_EVENT and
        the output pipe reaches EOF once they are gone. Idempotent. Must not
        run on the reader thread (older Windows blocks here until the output
        has been drained)."""
        with self._lock:
            hpc, self.hpc = self.hpc, None
        if hpc is not None:
            self.k.ClosePseudoConsole(hpc)

    def kill(self, code: int = 1) -> None:
        """Terminate the whole job (the child and everything it started)."""
        if self.job is not None:
            self.k.TerminateJobObject(self.job, code)
        elif self.process is not None:
            # No job (nested-job refusal on very old systems): the child only.
            self.k.TerminateProcess(self.process, code)

    def close(self, reader_timeout: float = 3.0) -> None:
        """Release everything once the child is gone: hang up, kill
        stragglers left in the job, let the reader drain to EOF, close
        handles. Idempotent."""
        if self._closed:
            return
        self.hangup()
        self.kill()
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(reader_timeout)
        self._release(force=False)

    def _release(self, force: bool) -> None:
        self._closed = True
        k = self.k
        if self.hpc is not None:
            self.hangup()
        reader_alive = self._reader is not None and self._reader.is_alive()
        for attr in ("in_w", "out_r", "thread", "process", "job"):
            h = getattr(self, attr)
            if h is None:
                continue
            if attr == "out_r" and reader_alive and not force:
                # Closing a handle under a blocked ReadFile is undefined;
                # leak it rather than risk the worker.
                log.warning("conpty reader for pid %s did not finish; leaving its pipe open", self.pid)
                continue
            try:
                k.CloseHandle(h)
            except Exception:
                pass
            setattr(self, attr, None)

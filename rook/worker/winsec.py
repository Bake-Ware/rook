"""Small Windows security helpers in pure ctypes (no pywin32 in the bundle).

* owner-only files and folders (the Windows meaning of mode 0600/0700),
* "is this file private to me" checks (owner + DACL),
* facts about a process (image, creation time, owner, alive),
* named-pipe client connections with the server's process id.

Every function is a no-op or raises on other platforms; callers branch on
``sys.platform`` first. Tests replace these functions, not ctypes.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import POINTER, Structure, byref, c_int32, c_uint8, c_uint16, c_uint32, c_uint64, c_void_p, c_wchar_p

HANDLE = c_void_p
DWORD = c_uint32
BOOL = c_int32

SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
TRUSTED_SIDS = frozenset({SYSTEM_SID, ADMINISTRATORS_SID})

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
TOKEN_QUERY = 0x0008
TokenUser = 1
SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x1
DACL_SECURITY_INFORMATION = 0x4
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
ACCESS_DENIED_ACE_TYPE = 1
STILL_ACTIVE = 259
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
SECURITY_SQOS_PRESENT = 0x00100000
SECURITY_IDENTIFICATION = 0x00010000
ERROR_PIPE_BUSY = 231
_IS_WIN = sys.platform == "win32"
INVALID_HANDLE_VALUE = (1 << (8 * ctypes.sizeof(c_void_p))) - 1


class ACL(Structure):
    _fields_ = [("AclRevision", c_uint8), ("Sbz1", c_uint8), ("AclSize", c_uint16),
                ("AceCount", c_uint16), ("Sbz2", c_uint16)]


class ACE_HEADER(Structure):
    _fields_ = [("AceType", c_uint8), ("AceFlags", c_uint8), ("AceSize", c_uint16)]


_libs = None


def _api():
    global _libs
    if _libs is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        a = ctypes.WinDLL("advapi32", use_last_error=True)
        protos = (
            (k, "GetCurrentProcess", HANDLE, []),
            (k, "OpenProcess", HANDLE, [DWORD, BOOL, DWORD]),
            (k, "CloseHandle", BOOL, [HANDLE]),
            (k, "LocalFree", HANDLE, [HANDLE]),
            (k, "GetProcessTimes", BOOL, [HANDLE] + [POINTER(c_uint64)] * 4),
            (k, "GetExitCodeProcess", BOOL, [HANDLE, POINTER(DWORD)]),
            (k, "QueryFullProcessImageNameW", BOOL, [HANDLE, DWORD, c_wchar_p, POINTER(DWORD)]),
            (k, "CreateFileW", HANDLE, [c_wchar_p, DWORD, DWORD, c_void_p, DWORD, DWORD, HANDLE]),
            (k, "WriteFile", BOOL, [HANDLE, c_void_p, DWORD, POINTER(DWORD), c_void_p]),
            (k, "WaitNamedPipeW", BOOL, [c_wchar_p, DWORD]),
            (k, "GetNamedPipeServerProcessId", BOOL, [HANDLE, POINTER(c_uint32)]),
            (a, "OpenProcessToken", BOOL, [HANDLE, DWORD, POINTER(HANDLE)]),
            (a, "GetTokenInformation", BOOL, [HANDLE, c_int32, c_void_p, DWORD, POINTER(DWORD)]),
            (a, "ConvertSidToStringSidW", BOOL, [c_void_p, POINTER(c_void_p)]),
            (a, "GetNamedSecurityInfoW", DWORD, [c_wchar_p, c_int32, DWORD, POINTER(c_void_p),
                                                 POINTER(c_void_p), POINTER(c_void_p),
                                                 POINTER(c_void_p), POINTER(c_void_p)]),
            (a, "GetAce", BOOL, [c_void_p, DWORD, POINTER(c_void_p)]),
            (a, "ConvertStringSecurityDescriptorToSecurityDescriptorW", BOOL,
             [c_wchar_p, DWORD, POINTER(c_void_p), POINTER(DWORD)]),
            (a, "SetFileSecurityW", BOOL, [c_wchar_p, DWORD, c_void_p]),
        )
        for lib, name, restype, argtypes in protos:
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = restype, argtypes
        _libs = (k, a)
    return _libs


def _fail(what: str) -> OSError:
    code = getattr(ctypes, "get_last_error", lambda: 0)()
    return OSError(f"{what} failed (error {code})")


def _sid_string(psid) -> str:
    k, a = _api()
    out = c_void_p()
    if not a.ConvertSidToStringSidW(psid, byref(out)):
        raise _fail("ConvertSidToStringSidW")
    try:
        return ctypes.wstring_at(out.value)
    finally:
        k.LocalFree(out)


def _token_user_sid(process_handle) -> str:
    k, a = _api()
    token = HANDLE()
    if not a.OpenProcessToken(process_handle, TOKEN_QUERY, byref(token)):
        raise _fail("OpenProcessToken")
    try:
        size = DWORD(0)
        a.GetTokenInformation(token, TokenUser, None, 0, byref(size))
        buf = ctypes.create_string_buffer(max(size.value, 64))
        if not a.GetTokenInformation(token, TokenUser, buf, len(buf), byref(size)):
            raise _fail("GetTokenInformation")
        # TOKEN_USER starts with SID_AND_ATTRIBUTES whose first field is the PSID.
        return _sid_string(c_void_p.from_buffer(buf).value)
    finally:
        k.CloseHandle(token)


_me: str | None = None


def current_user_sid() -> str:
    """The string SID this worker runs as."""
    global _me
    if _me is None:
        k, _a = _api()
        _me = _token_user_sid(k.GetCurrentProcess())
    return _me


def file_security(path: str) -> tuple[str, list[tuple[int, str]] | None]:
    """(owner SID, [(ace type, SID), ...]) for a file; the list is None for
    a NULL DACL (everyone has full access)."""
    k, a = _api()
    owner, dacl, sd = c_void_p(), c_void_p(), c_void_p()
    err = a.GetNamedSecurityInfoW(str(path), SE_FILE_OBJECT,
                                  OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                                  byref(owner), None, byref(dacl), None, byref(sd))
    if err:
        raise OSError(f"GetNamedSecurityInfoW failed (error {err})")
    try:
        if not dacl.value:
            return _sid_string(owner), None
        aces = []
        for i in range(ACL.from_address(dacl.value).AceCount):
            ace = c_void_p()
            if not a.GetAce(dacl, i, byref(ace)):
                raise _fail("GetAce")
            kind = ACE_HEADER.from_address(ace.value).AceType
            # ACCESS_ALLOWED/DENIED_ACE: header (4) + mask (4), then the SID.
            aces.append((kind, _sid_string(ace.value + 8)))
        return _sid_string(owner), aces
    finally:
        k.LocalFree(sd)


def is_private(path, me: str | None = None) -> bool:
    """True when ``path`` is owned by ``me`` and no one else but SYSTEM and
    Administrators (who can read anything anyway) is granted access. This
    is the Windows reading of "mode 0600, owned by me": files under a user
    profile inherit exactly that DACL."""
    me = me or current_user_sid()
    try:
        owner, aces = file_security(str(path))
    except OSError:
        return False
    if owner != me or aces is None:
        return False
    return all(kind == ACCESS_DENIED_ACE_TYPE or sid == me or sid in TRUSTED_SIDS
               for kind, sid in aces)


def restrict_to_owner(path, directory: bool = False) -> None:
    """Replace the DACL with one entry: full control for this user, nothing
    inherited (Windows' chmod 600 / 700)."""
    k, a = _api()
    inherit = "OICI" if directory else ""
    sddl = f"D:P(A;{inherit};FA;;;{current_user_sid()})"
    sd = c_void_p()
    if not a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, byref(sd), None):
        raise _fail("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        if not a.SetFileSecurityW(str(path), DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION, sd):
            raise _fail("SetFileSecurityW")
    finally:
        k.LocalFree(sd)


def process_info(pid: int) -> dict | None:
    """{image (lower-case file name), created (FILETIME int, UTC), alive,
    sid} for a process this user may query, else None."""
    k, _a = _api()
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        size = DWORD(1024)
        name = ctypes.create_unicode_buffer(size.value)
        if not k.QueryFullProcessImageNameW(h, 0, name, byref(size)):
            return None
        created, exited, kern, user = c_uint64(), c_uint64(), c_uint64(), c_uint64()
        if not k.GetProcessTimes(h, byref(created), byref(exited), byref(kern), byref(user)):
            return None
        code = DWORD(0)
        alive = bool(k.GetExitCodeProcess(h, byref(code))) and code.value == STILL_ACTIVE
        try:
            sid = _token_user_sid(h)
        except OSError:
            sid = ""
        return {"image": os.path.basename(name.value).lower(), "created": int(created.value),
                "alive": alive, "sid": sid}
    finally:
        k.CloseHandle(h)


def open_pipe(path: str, wait_ms: int = 5000):
    """Connect to a local named pipe for read/write. The server only gets an
    identification-level token for us (it cannot act as this user)."""
    k, _a = _api()
    for attempt in (0, 1):
        h = k.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING,
                          SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION, None)
        if h and h != INVALID_HANDLE_VALUE:
            return h
        if attempt or ctypes.get_last_error() != ERROR_PIPE_BUSY or not k.WaitNamedPipeW(path, wait_ms):
            raise _fail("CreateFileW(pipe)")
    raise OSError("pipe busy")


def pipe_server_pid(handle) -> int:
    k, _a = _api()
    pid = c_uint32(0)
    if not k.GetNamedPipeServerProcessId(handle, byref(pid)):
        raise _fail("GetNamedPipeServerProcessId")
    return int(pid.value)


def write_all(handle, data: bytes) -> None:
    k, _a = _api()
    view = memoryview(bytes(data))
    n = DWORD(0)
    while view:
        chunk = bytes(view[:65536])
        if not k.WriteFile(handle, chunk, len(chunk), byref(n), None):
            raise _fail("WriteFile")
        view = view[n.value:]


def close_handle(handle) -> None:
    k, _a = _api()
    k.CloseHandle(handle)


# -- portable private files ----------------------------------------------------

def make_private_dir(path) -> None:
    """mkdir -m 700 -p, with an owner-only DACL on Windows."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    if _IS_WIN:
        restrict_to_owner(path, directory=True)


def write_private_file(path, text: str) -> None:
    """Create/truncate ``path`` readable by this user only (0600, or an
    owner-only DACL on Windows applied before any content is written), then
    write ``text``."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if _IS_WIN:
            restrict_to_owner(path)
        view = memoryview(text.encode("utf-8"))
        while view:
            view = view[os.write(fd, view):]
    except BaseException:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    os.close(fd)

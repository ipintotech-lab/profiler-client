"""Where the pseudonymization key comes from, and how it is kept between runs.

A key file is protected by the operating system:

- Mac and Linux: the file holds the key in hex and must be mode 0600.
- Windows: Unix modes don't exist there, so the key is encrypted with DPAPI
  for the current Windows account (``CryptProtectData``, user scope). The
  file starts with ``DPAPI_HEADER``; a copy is useless to another account
  or on another machine. A plain hex file found on Windows is converted on
  first use.

A file without a header holds the key in hex, as every earlier version
wrote it, so Mac and Linux key files are unchanged.

A key can also come from an environment variable or a command (a team's or
a customer's secret store). A key that can't be read is an error,
never a reason to make a new one: a new key would silently break comparison
with earlier reports.

No message here ever quotes a key or a command's output.
"""
from __future__ import annotations

import base64
import ctypes
import os
import secrets
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

KEY_BYTES = 32

WINDOWS = os.name == "nt"
DPAPI_HEADER = "profiler-client key dpapi v1"
# mixed into DPAPI, so another program running as the same user can't
# decrypt the blob by calling CryptUnprotectData without it
DPAPI_ENTROPY = b"profiler-client key v1"

COMMAND_TIMEOUT_S = 60

MOVE_HINT = ("run prepare on the Windows account and machine that made it, or keep a key your team shares in a "
             "secret store and pass it with --key-env or --key-command")


class KeyUnavailable(ValueError):
    """The key can't be read. Never answered with a new key."""


def new_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


def _hex_key(text: str, source: str) -> bytes:
    try:
        key = bytes.fromhex(text.strip())
    except ValueError:
        raise KeyUnavailable(f"{source} doesn't hold a key in hex") from None
    if len(key) < KEY_BYTES:
        raise KeyUnavailable(f"{source} holds a key shorter than {KEY_BYTES} bytes")
    return key


# -- DPAPI -------------------------------------------------------------------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p)]


def _dpapi(data: bytes, protect: bool) -> bytes:
    """``CryptProtectData`` / ``CryptUnprotectData`` for the current user.
    Raises OSError when Windows refuses (another account, another machine)."""
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    blob_p = ctypes.POINTER(_Blob)
    call.argtypes = [blob_p, ctypes.c_wchar_p if protect else ctypes.c_void_p, blob_p,
                     ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, blob_p]
    call.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    data_buf = ctypes.create_string_buffer(data, len(data))
    entropy_buf = ctypes.create_string_buffer(DPAPI_ENTROPY, len(DPAPI_ENTROPY))
    blob_in = _Blob(len(data), ctypes.cast(data_buf, ctypes.c_void_p))
    entropy = _Blob(len(DPAPI_ENTROPY), ctypes.cast(entropy_buf, ctypes.c_void_p))
    out = _Blob()
    ui_forbidden = 0x1
    description = "profiler-client key" if protect else None
    if not call(ctypes.byref(blob_in), description, ctypes.byref(entropy), None, None, ui_forbidden,
                ctypes.byref(out)):
        raise OSError(ctypes.get_last_error(), "Windows refused to decrypt the key")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


# -- key files ---------------------------------------------------------------

def _encode(key: bytes) -> str:
    if WINDOWS:
        return f"{DPAPI_HEADER}\n{base64.b64encode(_dpapi(key, protect=True)).decode('ascii')}\n"
    return key.hex() + "\n"


def _write_new(path: Path, text: str) -> None:
    """Create ``path`` (never overwrite), owner-only on Mac and Linux."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def _replace(path: Path, text: str) -> None:
    """Put ``text`` in ``path`` in one step. It is written to a new file
    beside it and flushed to disk, then renamed over ``path``: an interruption
    leaves the old file or the new one, never a partial file."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        _write_new(tmp, text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _encrypt_in_place(path: Path, key: bytes, note: Callable[[str], None] | None) -> None:
    """Replace a plain key file on Windows with the encrypted one. The
    encrypted copy must decrypt to the same key before it replaces the file,
    and any failure keeps the plain file as it was, to be tried again."""
    try:
        text = _encode(key)
        if _dpapi(base64.b64decode(text.split("\n")[1]), protect=False) != key:
            raise OSError(0, "the encrypted copy didn't decrypt to the same key")
        _replace(path, text)
    except OSError as exc:
        if note:
            note(f"{path} holds the key unencrypted and couldn't be encrypted ({exc.strerror or exc}); it is "
                 "used as it is and encrypting it is tried again next time")
        return
    if note:
        note(f"{path} held the key unencrypted; it is now encrypted for this Windows account "
             "(client versions before 0.36 can't read it)")


def write_key_file(path: Path, key: bytes) -> None:
    """Keep ``key`` at a new ``path``, protected as this system protects key files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_new(path, _encode(key))


def load_or_create_key(path: Path, note: Callable[[str], None] | None = None) -> bytes:
    """Read a persistent key, creating it if it doesn't exist."""
    if not path.exists():
        key = new_key()
        write_key_file(path, key)
        return key
    try:
        text = path.read_text(encoding="ascii")
    except UnicodeDecodeError:
        raise KeyUnavailable(f"{path} is not a profiler-client key file") from None
    header, _, body = text.partition("\n")
    if header == DPAPI_HEADER:
        if not WINDOWS:
            raise KeyUnavailable(f"{path} is protected by Windows for one Windows account and can't be read "
                                 "here; " + MOVE_HINT.format(path=path))
        try:
            blob = base64.b64decode(body.strip(), validate=True)
        except ValueError:
            raise KeyUnavailable(f"{path} is damaged: its encrypted key isn't readable. No new key is made, "
                                 "because one couldn't be compared with your earlier reports") from None
        try:
            key = _dpapi(blob, protect=False)
        except OSError:
            raise KeyUnavailable(
                f"{path} can't be decrypted: Windows protected it for another account or another machine. No new "
                "key is made, because one couldn't be compared with your earlier reports; "
                + MOVE_HINT.format(path=path)) from None
        return _hex_key(key.hex(), str(path))
    if WINDOWS:
        key = _hex_key(text, str(path))
        _encrypt_in_place(path, key, note)
        return key
    if path.stat().st_mode & 0o077:
        raise KeyUnavailable(f"{path} is readable by other users; run: chmod 600 {path}")
    return _hex_key(text, str(path))


# -- keys from outside a file ------------------------------------------------

def key_from_env(var: str) -> bytes:
    value = os.environ.get(var, "")
    if not value.strip():
        raise KeyUnavailable(f"the environment variable {var} is not set or is empty")
    return _hex_key(value, f"the environment variable {var}")


def key_from_command(command: str) -> bytes:
    """Run ``command`` and read the key in hex from its standard output.

    No shell runs it, so ``|``, ``&&`` or ``$(...)`` are passed to the program
    as plain arguments. It gets no input and ``COMMAND_TIMEOUT_S`` to finish.
    Its standard output, which is the key, is never shown or written, in any
    error either; its error output goes to the terminal as it is."""
    args = command if WINDOWS else shlex.split(command)
    try:
        done = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, timeout=COMMAND_TIMEOUT_S,
                              check=False)
    except subprocess.TimeoutExpired:
        raise KeyUnavailable(f"--key-command didn't finish within {COMMAND_TIMEOUT_S} s") from None
    except (OSError, ValueError) as exc:
        raise KeyUnavailable(f"--key-command couldn't run ({getattr(exc, 'strerror', None) or 'bad command'})") \
            from None
    if done.returncode != 0:
        raise KeyUnavailable(f"--key-command exited with status {done.returncode}")
    return _hex_key(done.stdout.decode("ascii", "replace"), "the output of --key-command")

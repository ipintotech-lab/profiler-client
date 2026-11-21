"""Where the key comes from and how it is kept (issue #54).

On Mac and Linux a key file is hex, mode 0600, as before. On Windows it is
encrypted with DPAPI for the current account. The DPAPI logic is checked on
every OS with a stand-in for Windows; the real DPAPI calls run on Windows.
"""
import json
import os
import sys

import pytest

from profiler_client import cli, keyfile
from profiler_client.dataset import EVENTS
from profiler_client.keyfile import DPAPI_HEADER, KeyUnavailable, load_or_create_key
from profiler_client.pseudonym import Pseudonymizer

KEY = bytes(range(32))
ON_WINDOWS = pytest.mark.skipif(os.name != "nt", reason="needs Windows DPAPI")
NOT_WINDOWS = pytest.mark.skipif(os.name == "nt", reason="Mac and Linux key files")


@pytest.fixture
def fake_windows(monkeypatch):
    """Windows, with DPAPI that only "this account" can undo."""
    def dpapi(data, protect):
        if protect:
            return b"this-account:" + bytes(b ^ 0x5A for b in data)
        if not data.startswith(b"this-account:"):
            raise OSError(13, "refused")
        return bytes(b ^ 0x5A for b in data.removeprefix(b"this-account:"))
    monkeypatch.setattr(keyfile, "WINDOWS", True)
    monkeypatch.setattr(keyfile, "_dpapi", dpapi)


# -- Mac and Linux: unchanged ------------------------------------------------

@NOT_WINDOWS
def test_an_existing_hex_key_file_is_read_and_left_as_it_was(tmp_path):
    path = tmp_path / "key"
    path.write_text(KEY.hex() + "\n", encoding="ascii")
    os.chmod(path, 0o600)
    before = path.read_bytes()
    assert load_or_create_key(path) == KEY
    assert path.read_bytes() == before


@NOT_WINDOWS
def test_a_new_key_file_is_still_plain_hex(tmp_path):
    path = tmp_path / "cfg" / "key"
    key = load_or_create_key(path)
    assert path.read_text(encoding="ascii") == key.hex() + "\n"


@NOT_WINDOWS
def test_a_windows_key_file_is_refused_here_with_the_way_to_move_it(tmp_path):
    path = tmp_path / "key"
    path.write_text(f"{DPAPI_HEADER}\nAAAA\n", encoding="ascii")
    os.chmod(path, 0o600)
    with pytest.raises(KeyUnavailable, match="--key-env"):
        load_or_create_key(path)


# -- Windows, with the stand-in ----------------------------------------------

def test_on_windows_a_new_key_file_is_encrypted_and_reused(tmp_path, fake_windows):
    path = tmp_path / "cfg" / "key"
    key = load_or_create_key(path)
    text = path.read_text(encoding="ascii")
    assert text.startswith(DPAPI_HEADER + "\n")
    assert key.hex() not in text.lower()
    os.chmod(path, 0o644)  # Windows reports every file like this; the mode isn't what protects it
    assert load_or_create_key(path) == key
    assert load_or_create_key(path) == key


def test_on_windows_a_plain_key_file_is_encrypted_on_first_use(tmp_path, fake_windows):
    path = tmp_path / "key"
    path.write_text(KEY.hex() + "\n", encoding="ascii")
    notes = []
    assert load_or_create_key(path, note=notes.append) == KEY
    assert path.read_text(encoding="ascii").startswith(DPAPI_HEADER + "\n")
    assert KEY.hex() not in path.read_text(encoding="ascii")
    assert len(notes) == 1 and "encrypted" in notes[0]
    assert load_or_create_key(path, note=notes.append) == KEY and len(notes) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["key"]  # no temporary file left



def _plain_key_file(tmp_path):
    path = tmp_path / "key"
    path.write_text(KEY.hex() + "\n", encoding="ascii")
    return path, path.read_bytes()


@pytest.mark.parametrize("step", ["fsync", "replace"])
def test_on_windows_a_failed_encryption_keeps_the_plain_key_file(tmp_path, fake_windows, monkeypatch, step):
    path, before = _plain_key_file(tmp_path)

    def fail(*args):
        raise OSError(5, "access denied")
    monkeypatch.setattr(keyfile.os, step, fail)
    notes = []
    assert load_or_create_key(path, note=notes.append) == KEY
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["key"]
    assert "couldn't be encrypted" in notes[0] and "tried again" in notes[0]


def test_on_windows_an_interrupted_encryption_leaves_the_plain_key_file(tmp_path, fake_windows, monkeypatch):
    """Ctrl+C or a crash while the encrypted copy is written: the original
    file is only ever replaced by a rename, so it is still whole."""
    path, before = _plain_key_file(tmp_path)

    def interrupted(fd):
        raise KeyboardInterrupt
    monkeypatch.setattr(keyfile.os, "fsync", interrupted)
    with pytest.raises(KeyboardInterrupt):
        load_or_create_key(path)
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["key"]


def test_on_windows_an_encrypted_copy_that_doesnt_decrypt_is_never_kept(tmp_path, fake_windows, monkeypatch):
    path, before = _plain_key_file(tmp_path)
    monkeypatch.setattr(keyfile, "_dpapi", lambda data, protect: b"x" + data if protect else data)
    notes = []
    assert load_or_create_key(path, note=notes.append) == KEY
    assert path.read_bytes() == before and "didn't decrypt to the same key" in notes[0]
    assert [p.name for p in tmp_path.iterdir()] == ["key"]

def test_on_windows_a_key_another_account_made_is_an_error_not_a_new_key(tmp_path, fake_windows):
    path = tmp_path / "key"
    path.write_text(f"{DPAPI_HEADER}\nb3RoZXItYWNjb3VudDp4eHg=\n", encoding="ascii")  # "other-account:xxx"
    before = path.read_bytes()
    with pytest.raises(KeyUnavailable, match="another account or another machine") as exc:
        load_or_create_key(path)
    assert "No new key" in str(exc.value) and "--key-env" in str(exc.value)
    assert path.read_bytes() == before



def test_on_windows_a_damaged_key_file_is_an_error_not_a_new_key(tmp_path, fake_windows):
    path = tmp_path / "key"
    path.write_text(f"{DPAPI_HEADER}\nnot base64!\n", encoding="ascii")
    with pytest.raises(KeyUnavailable, match="damaged"):
        load_or_create_key(path)

def test_prepare_refuses_to_run_when_the_key_cant_be_decrypted(tmp_path, transcript_file, fake_windows, capsys):
    path = tmp_path / "key"
    path.write_text(f"{DPAPI_HEADER}\nb3RoZXItYWNjb3VudDp4eHg=\n", encoding="ascii")
    out = tmp_path / "ready"
    assert cli.main(["prepare", str(transcript_file), "-o", str(out), "--key-file", str(path)]) == 1
    assert "another account" in capsys.readouterr().err
    assert not out.exists()


# -- Windows, for real -------------------------------------------------------

@ON_WINDOWS
def test_dpapi_keeps_the_key_encrypted_for_this_account(tmp_path):
    path = tmp_path / "key"
    key = load_or_create_key(path)
    text = path.read_text(encoding="ascii")
    assert text.startswith(DPAPI_HEADER + "\n") and key.hex() not in text.lower()
    assert load_or_create_key(path) == key
    # the blob is bound to the client's label (not a secret; see DPAPI_ENTROPY)
    import base64
    blob = base64.b64decode(text.split("\n")[1])
    with pytest.raises(OSError):
        _unprotect_plain(blob)


def _unprotect_plain(blob):
    """CryptUnprotectData with no entropy, as another program would call it."""
    import ctypes
    crypt32, kernel32 = ctypes.WinDLL("crypt32", use_last_error=True), ctypes.WinDLL("kernel32")
    buf = ctypes.create_string_buffer(blob, len(blob))
    blob_in = keyfile._Blob(len(blob), ctypes.cast(buf, ctypes.c_void_p))
    out = keyfile._Blob()
    ptr = ctypes.POINTER(keyfile._Blob)
    crypt32.CryptUnprotectData.argtypes = [ptr, ctypes.c_void_p, ptr, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_uint32, ptr]
    if crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 1, ctypes.byref(out)):
        kernel32.LocalFree(ctypes.c_void_p(out.pbData))
        return
    raise OSError(ctypes.get_last_error(), "refused")


@ON_WINDOWS
def test_dpapi_refuses_a_changed_key_file(tmp_path):
    path = tmp_path / "key"
    load_or_create_key(path)
    header, blob, _ = path.read_text(encoding="ascii").split("\n")
    path.write_text(f"{header}\n{blob[:-8]}AAAAAAA=\n", encoding="ascii")
    with pytest.raises(KeyUnavailable, match="damaged|can.t be decrypted"):
        load_or_create_key(path)


# -- keys from outside a file ------------------------------------------------

def _all_output(out, capsys):
    captured = capsys.readouterr()
    return "".join(p.read_text(encoding="utf-8") for p in out.iterdir()) + captured.out + captured.err


def test_key_env_gives_the_key_and_it_is_written_nowhere(tmp_path, transcript_file, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("TEAM_KEY", KEY.hex().upper())
    out = tmp_path / "ready"
    assert cli.main(["prepare", str(transcript_file), "-o", str(out), "--key-env", "TEAM_KEY"]) == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["key_id"] == Pseudonymizer(KEY).key_id()
    written = _all_output(out, capsys) + "".join(
        p.read_text(encoding="utf-8") for p in (tmp_path / "data").rglob("*.json"))
    assert KEY.hex() not in written.lower() and "fresh key" not in written


def test_key_command_gives_the_key_and_it_is_written_nowhere(tmp_path, transcript_file, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    script = tmp_path / "secret_store.py"
    script.write_text(f"print({KEY.hex()!r})\n", encoding="utf-8")
    out = tmp_path / "ready"
    command = f'"{sys.executable}" "{script}"'
    assert cli.main(["prepare", str(transcript_file), "-o", str(out), "--key-command", command]) == 0
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8"))["key_id"] == Pseudonymizer(KEY).key_id()
    assert KEY.hex() not in _all_output(out, capsys).lower()


def test_a_failing_key_command_stops_prepare_and_its_output_is_not_shown(tmp_path, transcript_file, capsys):
    script = tmp_path / "secret_store.py"
    script.write_text(f"print({KEY.hex()!r})\nraise SystemExit(3)\n", encoding="utf-8")
    out = tmp_path / "ready"
    command = f'"{sys.executable}" "{script}"'
    assert cli.main(["prepare", str(transcript_file), "-o", str(out), "--key-command", command]) == 1
    err = capsys.readouterr().err
    assert "status 3" in err and KEY.hex() not in err and not out.exists()



def test_a_slow_key_command_is_stopped_and_its_output_is_not_shown(tmp_path, transcript_file, monkeypatch, capsys):
    monkeypatch.setattr(keyfile, "COMMAND_TIMEOUT_S", 1)
    script = tmp_path / "secret_store.py"
    script.write_text(f"import sys, time\nprint({KEY.hex()!r}, flush=True)\ntime.sleep(30)\n", encoding="utf-8")
    out = tmp_path / "ready"
    command = f'"{sys.executable}" "{script}"'
    assert cli.main(["prepare", str(transcript_file), "-o", str(out), "--key-command", command]) == 1
    err = capsys.readouterr().err
    assert "didn't finish within 1 s" in err and KEY.hex() not in err and not out.exists()


def test_the_key_command_runs_without_a_shell(tmp_path, transcript_file, capsys):
    """``&&`` reaches the program as an argument; no second command runs."""
    script = tmp_path / "secret_store.py"
    script.write_text(f"print({KEY.hex()!r})\n", encoding="utf-8")
    marker = tmp_path / "ran"
    command = (f'"{sys.executable}" "{script}" && "{sys.executable}" -c "open(\'{marker.as_posix()}\', \'w\')"')
    out = tmp_path / "ready"
    assert cli.main(["prepare", str(transcript_file), "-o", str(out), "--key-command", command]) == 0
    assert not marker.exists()


def test_a_key_command_that_prints_no_key_is_refused_without_quoting_it(tmp_path, transcript_file, capsys):
    script = tmp_path / "secret_store.py"
    script.write_text("print('s3cr3t-but-not-hex')\n", encoding="utf-8")
    out = tmp_path / "ready"
    command = f'"{sys.executable}" "{script}"'
    assert cli.main(["prepare", str(transcript_file), "-o", str(out), "--key-command", command]) == 1
    captured = capsys.readouterr()
    assert "doesn't hold a key in hex" in captured.err and "s3cr3t" not in captured.err + captured.out

@pytest.mark.parametrize("value, message", [("", "not set or is empty"), ("zz" * 32, "doesn't hold a key in hex"),
                                            ("ab" * 16, "shorter than 32 bytes")])
def test_a_bad_key_env_stops_prepare_without_quoting_it(tmp_path, transcript_file, monkeypatch, capsys, value, message):
    monkeypatch.setenv("TEAM_KEY", value)
    out = tmp_path / "ready"
    assert cli.main(["prepare", str(transcript_file), "-o", str(out), "--key-env", "TEAM_KEY"]) == 1
    err = capsys.readouterr().err
    assert message in err and not out.exists()
    assert not value or value not in err


def test_only_one_key_source_can_be_given(tmp_path, transcript_file):
    for extra in (["--key-file", "k", "--key-env", "V"], ["--key-env", "V", "--key-command", "c"],
                  ["--key-file", "k", "--key-command", "c"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(["prepare", str(transcript_file), "-o", str(tmp_path / "x"), *extra])
        assert exc.value.code == 2


def test_the_dataset_never_holds_the_key_file(tmp_path, transcript_file, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    path = tmp_path / "key"
    key = load_or_create_key(path)
    out = tmp_path / "ready"
    assert cli.main(["prepare", str(transcript_file), "-o", str(out), "--key-file", str(path)]) == 0
    assert key.hex() not in (out / EVENTS).read_text(encoding="utf-8")

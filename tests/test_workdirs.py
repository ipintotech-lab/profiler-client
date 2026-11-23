"""Working folders left by an app that was killed are deleted at the next
start (#58). Run on Linux, macOS and Windows in CI."""

import json
import os
import subprocess
import sys

from profiler_client import workdirs


def _folder(tmp_path, name, record):
    workdir = tmp_path / f"{workdirs.PREFIX}{name}"
    (workdir / "raw").mkdir(parents=True)
    (workdir / "raw" / "trace.jsonl").write_text("{}\n", encoding="utf-8")  # a file it copied in
    if record is not None:
        (workdir / workdirs.RECORD).write_text(record if isinstance(record, str) else json.dumps(record),
                                               encoding="utf-8")
    return workdir


def test_a_folder_from_a_process_that_has_ended_is_deleted(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    record = {"pid": proc.pid, "started": workdirs.started(proc.pid)}
    assert record["started"]
    proc.communicate(b"")
    assert workdirs.started(proc.pid) is None
    workdir = _folder(tmp_path, "dead", record)
    assert workdirs.sweep(tmp_path) == [workdir]
    assert not workdir.exists()


def test_a_folder_from_a_running_app_is_kept(tmp_path, capsys):
    workdir = _folder(tmp_path, "running", {"pid": os.getpid(), "started": workdirs.started(os.getpid())})
    assert workdirs.sweep(tmp_path) == []
    assert workdir.exists() and capsys.readouterr().out == ""


def test_a_folder_whose_process_id_was_reused_is_deleted(tmp_path):
    # the id is running (this test), but it started at another time than the app that made the folder
    workdir = _folder(tmp_path, "reused", {"pid": os.getpid(), "started": "another start"})
    assert workdirs.sweep(tmp_path) == [workdir]
    assert not workdir.exists()


def test_a_folder_that_cant_be_matched_to_an_app_is_kept_and_reported(tmp_path, capsys):
    unreadable = _folder(tmp_path, "unreadable", "not json")
    missing = _folder(tmp_path, "norecord", None)  # e.g. made by a client before 0.40
    no_start = _folder(tmp_path, "nostart", {"pid": os.getpid(), "started": None})
    assert workdirs.sweep(tmp_path) == []
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 3
    for workdir, line in zip((missing, no_start, unreadable), out):
        assert workdir.exists()
        assert line.endswith(workdirs.delete_command(workdir))


def test_other_folders_in_the_temp_folder_are_not_touched(tmp_path):
    other = tmp_path / "something-else"
    other.mkdir()
    (tmp_path / f"{workdirs.PREFIX}file").write_text("", encoding="utf-8")
    workdirs.sweep(tmp_path)
    assert other.exists() and (tmp_path / f"{workdirs.PREFIX}file").exists()


def test_the_app_records_itself_in_its_folder_and_keeps_it(tmp_path, monkeypatch):
    monkeypatch.setattr(workdirs.tempfile, "tempdir", str(tmp_path))
    workdir = workdirs.make()
    assert workdir.parent == tmp_path
    assert json.loads((workdir / workdirs.RECORD).read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert workdirs.sweep(tmp_path) == []
    assert workdir.exists()

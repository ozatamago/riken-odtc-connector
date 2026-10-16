"""ssh_target = "local": commands and copies on the Fluent PC itself, with scp's meaning of the paths."""

import sys

import pytest

from riken_odtc_connector import remote


def test_spec_and_local():
    assert remote.spec("Tecan@10.5.1.156", r"C:\Users\Tecan\OFML\x.txt") == "Tecan@10.5.1.156:C:/Users/Tecan/OFML/x.txt"
    assert remote.spec("local", r"C:\Users\Tecan\OFML\x.txt") == r"C:\Users\Tecan\OFML\x.txt"
    assert remote.is_local("LOCAL ") and not remote.is_local("Tecan@10.5.1.156")


def test_local_copy_like_scp(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hello")
    assert remote.copy(str(src), str(tmp_path / "out" / "b.txt"), target="local").returncode == 0
    assert (tmp_path / "out" / "b.txt").read_text() == "hello"
    record = tmp_path / "20261016-1_GetTips"
    (record / "steps").mkdir(parents=True)
    (record / "steps" / "01.xml").write_text("<x/>")
    # a folder copied "into" a folder, as store_record does with scp -r ... parent/
    assert remote.copy(str(record), str(tmp_path / "records") + "/", recursive=True, target="local").returncode == 0
    assert (tmp_path / "records" / "20261016-1_GetTips" / "steps" / "01.xml").read_text() == "<x/>"
    failed = remote.copy(str(tmp_path / "missing"), str(tmp_path / "x"), target="local")
    assert failed.returncode == 1 and failed.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="the command line below is for a POSIX shell")
def test_local_run_uses_the_shell():
    out = remote.run("local", "echo one && echo two", 10)
    assert out.returncode == 0 and out.stdout.split() == [b"one", b"two"]

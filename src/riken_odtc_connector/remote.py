"""Running commands and copying files on the Fluent PC, over SSH or - when the server runs on the Fluent PC itself -
directly.

The connector was first run on another computer and reaches the Fluent PC with ssh/scp (ssh_target, e.g.
"Tecan@10.5.1.156"). With ssh_target = "local" it runs on the Fluent PC: the same command lines are run with the local
cmd.exe (as the Fluent PC's OpenSSH server does), and copies are plain file copies. Paths are Fluent PC paths in both
cases.
"""

import os
import pathlib
import shutil
import subprocess

LOCAL = "local"


def long_path(path) -> str:
    """On Windows, the form of an absolute path that is not limited to 260 characters ("\\\\?\\C:\\..."): a record
    folder (session, record and step file names) can be longer. Unchanged elsewhere."""
    text = str(path)
    if os.name != "nt" or text.startswith("\\\\?\\"):
        return text
    return "\\\\?\\" + os.path.abspath(text)


def is_local(target: str) -> bool:
    return target.strip().lower() == LOCAL


def run(target: str, command: str, timeout: float) -> subprocess.CompletedProcess:
    """Run one command line on the Fluent PC (cmd.exe syntax, e.g. 'powershell -NoProfile -Command "..."')."""
    if is_local(target):
        return subprocess.run(command, shell=True, capture_output=True, timeout=timeout)
    return subprocess.run(["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", target, command],
                          capture_output=True, timeout=timeout)


def popen(target: str, command: str, **kwargs) -> subprocess.Popen:
    if is_local(target):
        return subprocess.Popen(command, shell=True, **kwargs)
    return subprocess.Popen(["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", target, command], **kwargs)


def spec(target: str, path: str) -> str:
    """A Fluent PC path in the form copy() takes ("Tecan@host:C:/x" over SSH, the path itself locally)."""
    if is_local(target):
        return path
    return f"{target}:{path.replace(chr(92), '/')}"


def copy(src: str, dest: str, recursive: bool = False, timeout: float = 600, target: str = "") -> subprocess.CompletedProcess:
    """Copy like scp: src and dest are local paths or spec() paths; a dest ending in "/" is a folder to copy into."""
    if not is_local(target):
        return subprocess.run(["scp", "-q"] + (["-r"] if recursive else []) + [src, dest],
                              capture_output=True, timeout=timeout)
    try:
        into = dest.endswith(("/", "\\")) or os.path.isdir(dest)
        source = pathlib.Path(long_path(src))
        final = pathlib.Path(long_path(dest)) / source.name if into else pathlib.Path(long_path(dest))
        final.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            if not recursive:
                raise IsADirectoryError(src)
            shutil.copytree(source, final, dirs_exist_ok=True)
        else:
            shutil.copy2(source, final)
        return subprocess.CompletedProcess([src, dest], 0, b"", b"")
    except OSError as exc:
        return subprocess.CompletedProcess([src, dest], 1, b"", str(exc).encode())

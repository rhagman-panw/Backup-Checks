import stat
from pathlib import Path
from types import SimpleNamespace

import paramiko
import pytest

from bundlesearch.remote import (
    RemoteBundleFetcher,
    RemoteError,
    load_private_key,
    validate_pattern,
    validate_port,
)

FILES = {
    "/backups/old.tgz": (b"old", 100),
    "/backups/new.tgz": (b"newest-bundle", 300),
    "/backups/notes.txt": (b"x", 400),
}
MB = 1024**2


def attr(name, size, mtime, mode=stat.S_IFREG | 0o644):
    return SimpleNamespace(filename=name, st_size=size, st_mtime=mtime, st_mode=mode)


class FakeSFTP:
    def __init__(self, files=FILES):
        self.files = files
        self.downloaded: list[str] = []

    def listdir_attr(self, path):
        if path != "/backups":
            raise FileNotFoundError(2, "No such file")
        entries = [attr(p.rsplit("/", 1)[1], len(d), m) for p, (d, m) in self.files.items()]
        entries.append(attr("subdir.tgz", 0, 999, stat.S_IFDIR | 0o755))
        return entries

    def stat(self, path):
        d, m = self.files[path]
        return attr(path, len(d), m)

    def get(self, remote, local):
        self.downloaded.append(remote)
        Path(local).write_bytes(self.files[remote][0])

    def close(self):
        pass


class FakeStream:
    def __init__(self, data: bytes, status=0):
        self._data = data
        self.channel = SimpleNamespace(recv_exit_status=lambda: status)

    def read(self):
        return self._data


class FakeSSH:
    def __init__(self, sftp=None, connect_exc=None, exec_map=None):
        self.sftp, self.connect_exc = sftp, connect_exc
        self.exec_map = exec_map or {}
        self.policy = None
        self.connect_kwargs: dict = {}
        self.commands: list[str] = []
        self.closed = False

    def load_system_host_keys(self):
        pass

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, host, **kwargs):
        self.connect_kwargs = {"host": host, **kwargs}
        if self.connect_exc:
            raise self.connect_exc

    def open_sftp(self):
        if self.sftp is None:
            raise paramiko.SSHException("subsystem request failed")
        return self.sftp

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        out, status = self.exec_map.get(command.split(" --")[0], (b"", 1))
        return None, FakeStream(out, status), FakeStream(b"" if status == 0 else b"ls: denied")

    def get_transport(self):
        return "transport"

    def close(self):
        self.closed = True


def make(ssh, **kw):
    kw.setdefault("username", "svc")
    kw.setdefault("password", "sshpw")
    return RemoteBundleFetcher("backup01", ssh_factory=lambda: ssh, **kw)


def test_validation():
    assert validate_port("") == 22
    with pytest.raises(RemoteError):
        validate_port("70000")
    assert validate_pattern("") == "*.tgz"
    with pytest.raises(RemoteError):
        validate_pattern("../*.tgz")
    with pytest.raises(RemoteError):
        RemoteBundleFetcher("bad host;", username="x")
    with pytest.raises(RemoteError):
        RemoteBundleFetcher("host", username="")


def test_sftp_downloads_newest_matching(tmp_path):
    sftp = FakeSFTP()
    ssh = FakeSSH(sftp=sftp)
    dest = tmp_path / "b.tgz"
    rf = make(ssh).fetch_latest("/backups", "*.tgz", dest, max_bytes=MB)
    assert rf.path == "/backups/new.tgz"
    assert dest.read_bytes() == b"newest-bundle"
    assert sftp.downloaded == ["/backups/new.tgz"]
    assert ssh.closed
    assert isinstance(ssh.policy, paramiko.RejectPolicy)
    assert ssh.connect_kwargs["password"] == "sshpw"
    assert ssh.connect_kwargs["allow_agent"] is False


def test_agent_used_when_no_password_or_key(tmp_path):
    ssh = FakeSSH(sftp=FakeSFTP())
    make(ssh, password=None).fetch_latest("/backups", "*.tgz", tmp_path / "b", max_bytes=MB)
    assert ssh.connect_kwargs["allow_agent"] is True
    assert ssh.connect_kwargs["look_for_keys"] is True


def test_accept_unknown_host_policy(tmp_path):
    ssh = FakeSSH(sftp=FakeSFTP())
    make(ssh, accept_unknown_host=True).fetch_latest("/backups", "*.tgz", tmp_path / "b", max_bytes=MB)
    assert isinstance(ssh.policy, paramiko.AutoAddPolicy)


def test_scp_fallback_when_sftp_unavailable(tmp_path):
    ssh = FakeSSH(
        sftp=None,
        exec_map={
            "ls -1tp": (b"subdir/\nnew.tgz\nold.tgz\nnotes.txt\n", 0),
            "stat -L -c %s": (b"13\n", 0),
        },
    )
    got: list[tuple] = []

    class FakeSCP:
        def __init__(self, transport):
            assert transport == "transport"

        def get(self, remote, local):
            got.append((remote, local))
            Path(local).write_bytes(b"newest-bundle")

        def close(self):
            pass

    dest = tmp_path / "b.tgz"
    rf = make(ssh, scp_factory=FakeSCP).fetch_latest("/backups", "*.tgz", dest, max_bytes=MB)
    assert rf.path == "/backups/new.tgz"
    assert got == [("/backups/new.tgz", str(dest))]
    assert ssh.commands[0] == "ls -1tp -- /backups"


def test_remote_dir_is_shell_quoted(tmp_path):
    ssh = FakeSSH(sftp=None, exec_map={"ls -1tp": (b"", 0)})
    with pytest.raises(RemoteError, match="No files"):
        make(ssh, transfer="scp").fetch_latest("/tmp/x; rm -rf ~", "*.tgz", tmp_path / "b", max_bytes=MB)
    assert ssh.commands[0] == "ls -1tp -- '/tmp/x; rm -rf ~'"


def test_sftp_forced_but_unavailable(tmp_path):
    with pytest.raises(RemoteError, match="SFTP is not available"):
        make(FakeSSH(sftp=None), transfer="sftp").fetch_latest("/backups", "*.tgz", tmp_path / "b", max_bytes=MB)


def test_no_matching_files(tmp_path):
    with pytest.raises(RemoteError, match="No files matching"):
        make(FakeSSH(sftp=FakeSFTP())).fetch_latest("/backups", "*.zip", tmp_path / "b", max_bytes=MB)


def test_missing_dir(tmp_path):
    with pytest.raises(RemoteError, match="Cannot list"):
        make(FakeSSH(sftp=FakeSFTP())).fetch_latest("/nope", "*.tgz", tmp_path / "b", max_bytes=MB)


def test_size_limit(tmp_path):
    with pytest.raises(RemoteError, match="larger than"):
        make(FakeSSH(sftp=FakeSFTP())).fetch_latest("/backups", "*.tgz", tmp_path / "b", max_bytes=5)


@pytest.mark.parametrize(
    "exc, msg",
    [
        (paramiko.AuthenticationException("bad sshpw"), "authentication failed"),
        (paramiko.SSHException("Server 'backup01' not found in known_hosts"), "not in known_hosts"),
        (OSError("refused"), "Could not connect"),
    ],
)
def test_connect_errors_are_safe(tmp_path, exc, msg):
    with pytest.raises(RemoteError) as ei:
        make(FakeSSH(connect_exc=exc)).fetch_latest("/backups", "*.tgz", tmp_path / "b", max_bytes=MB)
    assert msg in str(ei.value)
    assert "sshpw" not in str(ei.value)


def test_load_private_key_roundtrip():
    import io

    key = paramiko.RSAKey.generate(1024)
    buf = io.StringIO()
    key.write_private_key(buf, password="pp")
    loaded = load_private_key(buf.getvalue(), "pp")
    assert loaded.get_fingerprint() == key.get_fingerprint()
    with pytest.raises(RemoteError, match="passphrase"):
        load_private_key(buf.getvalue(), None)
    with pytest.raises(RemoteError):
        load_private_key("not a key")

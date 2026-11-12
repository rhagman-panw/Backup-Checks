"""Fetch the newest Panorama config bundle stored on a Linux server (SSH + SFTP/SCP).

* Authentication: password, uploaded private key (+ optional passphrase), or -
  when neither is given - the local SSH agent / ``~/.ssh`` keys.
* Host keys are checked against the system ``known_hosts``; unknown hosts are
  rejected unless ``accept_unknown_host`` is set.
* The newest file matching ``pattern`` in ``remote_dir`` is chosen by mtime.
  SFTP is used when available; otherwise ``ls`` + SCP.
"""
from __future__ import annotations

import fnmatch
import io
import logging
import posixpath
import re
import shlex
import socket
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import paramiko
from scp import SCPClient, SCPException

log = logging.getLogger(__name__)

TRANSFER_AUTO = "auto"
TRANSFER_SFTP = "sftp"
TRANSFER_SCP = "scp"
TRANSFERS = (TRANSFER_AUTO, TRANSFER_SFTP, TRANSFER_SCP)
DEFAULT_PATTERN = "*.tgz"
MAX_KEY_BYTES = 64 * 1024

_HOST_RE = re.compile(r"^(?:[A-Za-z0-9.-]+|[0-9A-Fa-f:.]+)$")
_BAD_CHARS = ("\x00", "\n", "\r")


class RemoteError(Exception):
    """User-presentable error (never contains credentials)."""


@dataclass
class RemoteFile:
    path: str
    size: int | None
    mtime: float | None = None


# ---------------------------------------------------------------- validation
def validate_host(host: str) -> str:
    h = (host or "").strip().strip("[]")
    if not h or not _HOST_RE.match(h):
        raise RemoteError("Enter a valid Linux server hostname or IP address.")
    return h


def validate_port(port: object) -> int:
    try:
        p = int(str(port).strip()) if port not in (None, "") else 22
    except ValueError:
        raise RemoteError("SSH port must be a number.") from None
    if not 1 <= p <= 65535:
        raise RemoteError("SSH port must be between 1 and 65535.")
    return p


def validate_dir(remote_dir: str) -> str:
    d = (remote_dir or "").strip()
    if not d or any(c in d for c in _BAD_CHARS):
        raise RemoteError("Enter the folder on the Linux server that holds the bundles.")
    return d


def validate_pattern(pattern: str) -> str:
    p = (pattern or "").strip() or DEFAULT_PATTERN
    if "/" in p or any(c in p for c in _BAD_CHARS):
        raise RemoteError("File pattern must be a file name pattern such as *.tgz (no '/').")
    return p


def load_private_key(text: str, passphrase: str | None = None) -> paramiko.PKey:
    """Load an OpenSSH/PEM private key of any supported type."""
    names = ("Ed25519Key", "ECDSAKey", "RSAKey", "DSSKey")
    for cls in (getattr(paramiko, n) for n in names if hasattr(paramiko, n)):
        try:
            return cls.from_private_key(io.StringIO(text), password=passphrase or None)
        except paramiko.PasswordRequiredException:
            raise RemoteError("The private key is encrypted. Enter its passphrase.") from None
        except (paramiko.SSHException, ValueError, TypeError, IndexError):
            continue
    raise RemoteError("Could not load the private key (unsupported format or wrong passphrase).")


# -------------------------------------------------------------------- client
class RemoteBundleFetcher:
    def __init__(
        self,
        host: str,
        *,
        port: object = 22,
        username: str,
        password: str | None = None,
        pkey: paramiko.PKey | None = None,
        accept_unknown_host: bool = False,
        transfer: str = TRANSFER_AUTO,
        timeout: float = 20,
        ssh_factory: Callable = paramiko.SSHClient,
        scp_factory: Callable | None = None,
    ) -> None:
        self.host = validate_host(host)
        self.port = validate_port(port)
        self.username = (username or "").strip()
        if not self.username:
            raise RemoteError("SSH username is required.")
        if transfer not in TRANSFERS:
            raise RemoteError("Unknown transfer method.")
        self.transfer = transfer
        self.accept_unknown_host = accept_unknown_host
        self.timeout = timeout
        self._password = password or None
        self._pkey = pkey
        self._ssh_factory = ssh_factory
        self._scp_factory = scp_factory

    # ---------------------------------------------------------------- helpers
    def _connect_message(self, exc: Exception) -> str:
        where = f"{self.host}:{self.port}"
        if isinstance(exc, paramiko.BadHostKeyException):
            return (
                f"The host key for {self.host} does not match known_hosts. "
                "The connection was refused (possible man-in-the-middle)."
            )
        if isinstance(exc, paramiko.AuthenticationException):
            return f"SSH authentication failed for {self.username}@{self.host}."
        if isinstance(exc, paramiko.SSHException) and "known_hosts" in str(exc):
            return (
                f"The host key for {self.host} is not in known_hosts. Add it "
                "(ssh-keyscan / connect once with ssh) or tick 'Accept unknown host key'."
            )
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return f"Timed out connecting to {where}."
        if isinstance(exc, OSError):
            return f"Could not connect to {where}."
        return f"SSH connection to {where} failed ({type(exc).__name__})."

    def _connect(self):
        client = self._ssh_factory()
        try:
            client.load_system_host_keys()
        except OSError:
            pass
        client.set_missing_host_key_policy(
            paramiko.AutoAddPolicy() if self.accept_unknown_host else paramiko.RejectPolicy()
        )
        use_local_keys = self._password is None and self._pkey is None
        try:
            client.connect(
                self.host,
                port=self.port,
                username=self.username,
                password=self._password,
                pkey=self._pkey,
                timeout=self.timeout,
                banner_timeout=self.timeout,
                auth_timeout=self.timeout,
                allow_agent=use_local_keys,
                look_for_keys=use_local_keys,
            )
        except Exception as exc:  # noqa: BLE001 - mapped to a safe message
            client.close()
            raise RemoteError(self._connect_message(exc)) from None
        return client

    def _run(self, client, command: str) -> tuple[int, str, str]:
        try:
            _stdin, stdout, stderr = client.exec_command(command, timeout=self.timeout)
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            return stdout.channel.recv_exit_status(), out, err
        except (paramiko.SSHException, OSError) as exc:
            raise RemoteError(
                f"Remote command failed on {self.host} ({type(exc).__name__})."
            ) from None

    def _no_match(self, remote_dir: str, pattern: str) -> RemoteError:
        return RemoteError(f"No files matching {pattern!r} found in {remote_dir} on {self.host}.")

    def _latest_via_sftp(self, sftp, remote_dir: str, pattern: str) -> RemoteFile:
        try:
            entries = sftp.listdir_attr(remote_dir)
        except OSError as exc:
            reason = getattr(exc, "strerror", None) or type(exc).__name__
            raise RemoteError(f"Cannot list {remote_dir} on {self.host}: {reason}.") from None
        candidates = []
        for entry in entries:
            if not fnmatch.fnmatchcase(entry.filename, pattern):
                continue
            path = posixpath.join(remote_dir, entry.filename)
            attrs = entry
            if stat.S_ISLNK(entry.st_mode or 0):
                try:
                    attrs = sftp.stat(path)
                except OSError:
                    continue
            if stat.S_ISREG(attrs.st_mode or 0):
                candidates.append(RemoteFile(path, attrs.st_size, attrs.st_mtime or 0))
        if not candidates:
            raise self._no_match(remote_dir, pattern)
        return max(candidates, key=lambda f: f.mtime or 0)

    def _latest_via_exec(self, client, remote_dir: str, pattern: str) -> RemoteFile:
        status, out, err = self._run(client, f"ls -1tp -- {shlex.quote(remote_dir)}")
        if status != 0:
            reason = err.strip().splitlines()[-1][:200] if err.strip() else "ls failed"
            raise RemoteError(f"Cannot list {remote_dir} on {self.host}: {reason}")
        for name in out.splitlines():  # newest first
            if not name or name.endswith("/") or not fnmatch.fnmatchcase(name, pattern):
                continue
            path = posixpath.join(remote_dir, name)
            st, size_out, _ = self._run(client, f"stat -L -c %s -- {shlex.quote(path)}")
            size = int(size_out.strip()) if st == 0 and size_out.strip().isdigit() else None
            return RemoteFile(path, size)
        raise self._no_match(remote_dir, pattern)

    def _scp_get(self, client, remote_path: str, dest: Path) -> None:
        factory = self._scp_factory or (
            lambda transport: SCPClient(transport, socket_timeout=max(self.timeout, 60))
        )
        scp = factory(client.get_transport())
        try:
            scp.get(remote_path, str(dest))
        finally:
            scp.close()

    # -------------------------------------------------------------------- API
    def fetch_latest(
        self, remote_dir: str, pattern: str, dest: Path, *, max_bytes: int
    ) -> RemoteFile:
        """Download the newest matching file to ``dest`` and return its details."""
        remote_dir = validate_dir(remote_dir)
        pattern = validate_pattern(pattern)
        dest = Path(dest)
        client = self._connect()
        try:
            sftp = None
            if self.transfer != TRANSFER_SCP:
                try:
                    sftp = client.open_sftp()
                except (paramiko.SSHException, OSError, EOFError):
                    if self.transfer == TRANSFER_SFTP:
                        raise RemoteError(
                            f"SFTP is not available on {self.host}. Choose SCP as the transfer method."
                        ) from None
                    log.info("SFTP unavailable on %s; using SCP", self.host)
            try:
                latest = (
                    self._latest_via_sftp(sftp, remote_dir, pattern)
                    if sftp
                    else self._latest_via_exec(client, remote_dir, pattern)
                )
                if latest.size is not None and latest.size > max_bytes:
                    raise RemoteError(
                        f"{latest.path} is larger than the {max_bytes // 1024**2} MiB limit."
                    )
                try:
                    if sftp:
                        sftp.get(latest.path, str(dest))
                    else:
                        self._scp_get(client, latest.path, dest)
                except (SCPException, paramiko.SSHException, OSError) as exc:
                    raise RemoteError(
                        f"Download of {latest.path} from {self.host} failed ({type(exc).__name__})."
                    ) from None
            finally:
                if sftp:
                    sftp.close()
        finally:
            client.close()

        if not dest.exists():
            raise RemoteError(f"Download of {latest.path} produced no file.")
        if dest.stat().st_size > max_bytes:
            dest.unlink()
            raise RemoteError(f"{latest.path} is larger than the {max_bytes // 1024**2} MiB limit.")
        return latest

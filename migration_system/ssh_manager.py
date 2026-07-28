"""
ssh_manager.py
===============

PURE SSH/SFTP CONNECTION MANAGER - NO COMMAND EXECUTION.

This module ONLY manages SSH connections.
ALL command execution goes through CommandExecutor.

Provides:
    - Connection management
    - Authentication (password/key)
    - SFTP upload/download
    - Connection pooling
    - Heartbeat

EXECUTION: Use CommandExecutor for ALL commands.
"""

from __future__ import annotations

import hashlib
import logging
import os
import posixpath
import socket
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import paramiko
from paramiko import (
    SSHClient, SFTPClient, AutoAddPolicy, RSAKey, Ed25519Key, ECDSAKey, DSSKey
)
from paramiko.ssh_exception import (
    AuthenticationException, NoValidConnectionsError, SSHException
)

logger = logging.getLogger("ssh_manager")


class SSHManagerError(Exception):
    """Base exception for all ssh_manager errors."""
    pass


class SSHConnectionError(SSHManagerError):
    """Raised when a connection cannot be established."""
    pass


class SFTPTransferError(SSHManagerError):
    """Raised when an SFTP upload/download fails."""
    pass


@dataclass
class SSHCredentials:
    """Authentication parameters for an SSH connection."""
    host: str
    port: int = 22
    username: str = "root"
    password: Optional[str] = None
    private_key_path: Optional[str] = None
    private_key_passphrase: Optional[str] = None

    def validate(self) -> None:
        if not self.password and not self.private_key_path:
            raise ValueError(
                "SSHCredentials requires either 'password' or 'private_key_path'"
            )


def _load_private_key(path: str, passphrase: Optional[str]) -> paramiko.PKey:
    """Load a private key trying every supported key type."""
    key_classes = (Ed25519Key, RSAKey, ECDSAKey, DSSKey)
    expanded = os.path.expanduser(path)

    for key_cls in key_classes:
        try:
            return key_cls.from_private_key_file(expanded, password=passphrase)
        except SSHException:
            continue

    raise SSHConnectionError(
        f"Could not load private key at '{path}' with any supported key type"
    )


class SSHManager:
    """
    PURE SSH/SFTP CONNECTION MANAGER.
    
    Only manages connections. NEVER executes commands directly.
    Use CommandExecutor for ALL command execution.
    """

    def __init__(
        self,
        credentials: SSHCredentials,
        connect_timeout: float = 15.0,
        max_reconnect_attempts: int = 5,
        reconnect_backoff_base: float = 2.0,
        keepalive_interval: int = 15
    ) -> None:
        credentials.validate()
        self.credentials = credentials
        self.connect_timeout = connect_timeout
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_backoff_base = reconnect_backoff_base
        self.keepalive_interval = keepalive_interval

        self._client: Optional[SSHClient] = None
        self._sftp: Optional[SFTPClient] = None
        self._lock = threading.RLock()
        self._connected = False

        logger.info("SSHManager initialized for %s@%s:%d",
                   credentials.username, credentials.host, credentials.port)

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #

    def connect(self) -> None:
        """Establish SSH connection with retries."""
        with self._lock:
            if self._connected and self._is_transport_active():
                return

            attempt = 0
            last_error: Optional[Exception] = None

            while attempt < self.max_reconnect_attempts:
                attempt += 1
                try:
                    logger.info(
                        "Connecting to %s@%s:%d (attempt %d/%d)",
                        self.credentials.username,
                        self.credentials.host,
                        self.credentials.port,
                        attempt,
                        self.max_reconnect_attempts
                    )

                    client = SSHClient()
                    client.set_missing_host_key_policy(AutoAddPolicy())

                    connect_kwargs = {
                        "hostname": self.credentials.host,
                        "port": self.credentials.port,
                        "username": self.credentials.username,
                        "timeout": self.connect_timeout,
                        "banner_timeout": self.connect_timeout,
                        "auth_timeout": self.connect_timeout,
                    }

                    if self.credentials.private_key_path:
                        pkey = _load_private_key(
                            self.credentials.private_key_path,
                            self.credentials.private_key_passphrase
                        )
                        connect_kwargs["pkey"] = pkey
                    else:
                        connect_kwargs["password"] = self.credentials.password

                    client.connect(**connect_kwargs)

                    transport = client.get_transport()
                    if transport:
                        transport.set_keepalive(self.keepalive_interval)

                    self._client = client
                    self._sftp = None
                    self._connected = True

                    logger.info("Connected to %s@%s:%d",
                               self.credentials.username,
                               self.credentials.host,
                               self.credentials.port)
                    return

                except AuthenticationException as e:
                    logger.error("Authentication failed for %s: %s",
                                self.credentials.host, e)
                    raise SSHConnectionError(f"Authentication failed: {e}") from e

                except (NoValidConnectionsError, SSHException, socket.error, OSError) as e:
                    last_error = e
                    wait = min(self.reconnect_backoff_base ** attempt, 30)
                    logger.warning(
                        "Connection attempt %d to %s failed: %s. Retrying in %.1fs",
                        attempt, self.credentials.host, e, wait
                    )
                    time.sleep(wait)

            self._connected = False
            raise SSHConnectionError(
                f"Failed to connect to {self.credentials.host}:{self.credentials.port} "
                f"after {self.max_reconnect_attempts} attempts: {last_error}"
            )

    def _is_transport_active(self) -> bool:
        if self._client is None:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    def ensure_connected(self) -> None:
        """Ensure connection is active, reconnect if needed."""
        with self._lock:
            if not self._is_transport_active():
                logger.warning("Connection to %s dropped, reconnecting...",
                              self.credentials.host)
                self._connected = False
                self._sftp = None
                self.connect()

    def get_transport(self):
        """Get the underlying paramiko transport for CommandExecutor."""
        with self._lock:
            self.ensure_connected()
            if self._client:
                return self._client.get_transport()
            return None

    def disconnect(self) -> None:
        """Close the SSH connection."""
        with self._lock:
            if self._sftp:
                try:
                    self._sftp.close()
                except Exception:
                    pass
                self._sftp = None

            if self._client:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None

            self._connected = False
            logger.info("Disconnected from %s", self.credentials.host)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()

    # ------------------------------------------------------------------ #
    # SFTP operations (PURE FILE TRANSFER)
    # ------------------------------------------------------------------ #

    def _get_sftp(self) -> SFTPClient:
        with self._lock:
            self.ensure_connected()
            if self._sftp is None or self._sftp.sock.closed:
                assert self._client is not None
                self._sftp = self._client.open_sftp()
            return self._sftp

    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        preserve_permissions: bool = True,
        retries: int = 3,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> None:
        """Upload a single file via SFTP."""
        if not os.path.isfile(local_path):
            raise FileNotFoundError(f"Local file not found: {local_path}")

        local_size = os.path.getsize(local_path)
        local_mode = stat.S_IMODE(os.stat(local_path).st_mode)

        last_exception: Optional[Exception] = None

        for attempt in range(retries):
            try:
                sftp = self._get_sftp()

                # Create remote directory
                remote_dir = posixpath.dirname(remote_path)
                if remote_dir:
                    self._makedirs_remote(remote_dir)

                sftp.put(local_path, remote_path, callback=progress_callback, confirm=True)

                if preserve_permissions:
                    sftp.chmod(remote_path, local_mode)

                # Verify size
                remote_size = sftp.stat(remote_path).st_size
                if remote_size != local_size:
                    raise SFTPTransferError(
                        f"Size mismatch: local={local_size} remote={remote_size}"
                    )

                logger.info("Uploaded '%s' -> %s:%s (%d bytes)",
                           local_path, self.credentials.host, remote_path, local_size)
                return

            except (SSHException, socket.error, OSError, SFTPTransferError) as e:
                last_exception = e
                logger.warning("Upload attempt %d/%d failed: %s", attempt + 1, retries, e)
                with self._lock:
                    self._connected = False
                    self._sftp = None
                time.sleep(min(2 ** attempt, 10))

        raise SFTPTransferError(
            f"Upload of '{local_path}' failed after {retries} attempts: {last_exception}"
        )

    def download_file(
        self,
        remote_path: str,
        local_path: str,
        retries: int = 3,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> None:
        """Download a single file via SFTP."""
        os.makedirs(os.path.dirname(os.path.abspath(local_path)) or ".", exist_ok=True)

        last_exception: Optional[Exception] = None

        for attempt in range(retries):
            try:
                sftp = self._get_sftp()
                sftp.get(remote_path, local_path, callback=progress_callback)

                # Verify size
                remote_size = sftp.stat(remote_path).st_size
                local_size = os.path.getsize(local_path)
                if remote_size != local_size:
                    raise SFTPTransferError(
                        f"Size mismatch: remote={remote_size} local={local_size}"
                    )

                logger.info("Downloaded %s:%s -> '%s' (%d bytes)",
                           self.credentials.host, remote_path, local_path, local_size)
                return

            except (SSHException, socket.error, OSError, SFTPTransferError) as e:
                last_exception = e
                logger.warning("Download attempt %d/%d failed: %s", attempt + 1, retries, e)
                with self._lock:
                    self._connected = False
                    self._sftp = None
                time.sleep(min(2 ** attempt, 10))

        raise SFTPTransferError(
            f"Download of '{remote_path}' failed after {retries} attempts: {last_exception}"
        )

    def _makedirs_remote(self, remote_dir: str) -> None:
        """Recursively create remote directories."""
        sftp = self._get_sftp()
        parts = remote_dir.strip("/").split("/")
        current = "/" if remote_dir.startswith("/") else ""

        for part in parts:
            if not part:
                continue
            current = posixpath.join(current, part) if current else part
            try:
                sftp.stat(current)
            except FileNotFoundError:
                try:
                    sftp.mkdir(current)
                except OSError:
                    sftp.stat(current)

    def upload_directory(
        self,
        local_dir: str,
        remote_dir: str,
        preserve_permissions: bool = True,
        retries: int = 3,
        exclude: Optional[Set[str]] = None
    ) -> List[str]:
        """Recursively upload a directory tree."""
        if not os.path.isdir(local_dir):
            raise NotADirectoryError(f"Local directory not found: {local_dir}")

        exclude = exclude or set()
        uploaded: List[str] = []
        local_root = Path(local_dir)

        self._makedirs_remote(remote_dir)

        for local_file in sorted(local_root.rglob("*")):
            if any(part in exclude for part in local_file.parts):
                continue

            relative = local_file.relative_to(local_root).as_posix()
            target_remote = posixpath.join(remote_dir, relative)

            if local_file.is_dir():
                self._makedirs_remote(target_remote)
                continue

            self.upload_file(
                str(local_file),
                target_remote,
                preserve_permissions=preserve_permissions,
                retries=retries
            )
            uploaded.append(target_remote)

        logger.info("Uploaded directory %s -> %s:%s (%d files)",
                   local_dir, self.credentials.host, remote_dir, len(uploaded))
        return uploaded

    def download_directory(
        self,
        remote_dir: str,
        local_dir: str,
        retries: int = 3
    ) -> List[str]:
        """Recursively download a directory tree."""
        sftp = self._get_sftp()
        os.makedirs(local_dir, exist_ok=True)
        downloaded: List[str] = []

        def walk(remote_path: str, local_path: str) -> None:
            os.makedirs(local_path, exist_ok=True)
            for entry in sftp.listdir_attr(remote_path):
                remote_entry = posixpath.join(remote_path, entry.filename)
                local_entry = os.path.join(local_path, entry.filename)

                if stat.S_ISDIR(entry.st_mode):
                    walk(remote_entry, local_entry)
                else:
                    self.download_file(
                        remote_entry,
                        local_entry,
                        retries=retries
                    )
                    downloaded.append(local_entry)

        walk(remote_dir, local_dir)
        logger.info("Downloaded %s:%s -> %s (%d files)",
                   self.credentials.host, remote_dir, local_dir, len(downloaded))
        return downloaded

    # ------------------------------------------------------------------ #
    # Diagnostics
    # ------------------------------------------------------------------ #

    def is_alive(self) -> bool:
        """Check if the connection is active."""
        try:
            self.ensure_connected()
            return self._is_transport_active()
        except SSHManagerError:
            return False

    def get_connection_info(self) -> Dict[str, Any]:
        """Get connection information."""
        return {
            "host": self.credentials.host,
            "port": self.credentials.port,
            "username": self.credentials.username,
            "connected": self._connected,
            "transport_active": self._is_transport_active() if self._client else False,
        }
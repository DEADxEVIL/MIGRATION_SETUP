from __future__ import annotations

import base64
import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("credential_manager")


class CredentialManagerError(Exception):
    """Raised for credential errors."""


class CredentialManager:
    """
    Secure credential storage with encryption.
    """
    
    def __init__(
        self,
        store_path: str = "credentials.encrypted",
        key_path: Optional[str] = None,
    ):
        self.store_path = store_path
        self.key_path = key_path or f"{store_path}.key"
        self._lock = threading.RLock()
        self._cache: Dict[str, Any] = {}
        self._cipher = None
        
        # Load or generate key
        self._load_key()
    
    def _load_key(self) -> None:
        """Load or generate encryption key."""
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        
        if os.path.exists(self.key_path):
            with open(self.key_path, "rb") as f:
                key = f.read()
        else:
            # Generate key from machine ID
            try:
                with open("/etc/machine-id", "r") as f:
                    machine_id = f.read().strip()
            except Exception:
                import uuid
                machine_id = str(uuid.uuid4())
            
            salt = os.urandom(16)
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=100000,
            )
            key = base64.urlsafe_b64encode(kdf.derive(machine_id.encode()))
            
            # Save key
            with open(self.key_path, "wb") as f:
                f.write(key)
            
            # Secure salt storage (simplified - in production use separate storage)
            with open(f"{self.key_path}.salt", "wb") as f:
                f.write(salt)
            
            os.chmod(self.key_path, 0o600)
        
        self._cipher = Fernet(key)
    
    # ------------------------------------------------------------------ #
    # Storage operations
    # ------------------------------------------------------------------ #
    
    def store_credentials(
        self,
        name: str,
        host: str,
        username: str,
        password: Optional[str] = None,
        private_key: Optional[str] = None,
        port: int = 22,
    ) -> None:
        """
        Store encrypted credentials.
        
        Args:
            name: Identifier for these credentials
            host: VPS IP or hostname
            username: SSH username
            password: SSH password (optional)
            private_key: SSH private key content (optional)
            port: SSH port
        """
        data = {
            "host": host,
            "username": username,
            "port": port,
        }
        
        if password:
            data["password"] = password
        if private_key:
            data["private_key"] = private_key
        
        self._store(name, data)
        logger.info(f"Stored credentials for: {name}")
    
    def get_credentials(self, name: str) -> Dict[str, Any]:
        """
        Get decrypted credentials.
        
        Returns dict with:
            - host
            - username
            - port
            - password (if set)
            - private_key (if set)
        """
        return self._get(name)
    
    def get_password(self, name: str) -> Optional[str]:
        """Get password for credentials."""
        creds = self.get_credentials(name)
        return creds.get("password")
    
    def get_private_key(self, name: str) -> Optional[str]:
        """Get private key for credentials."""
        creds = self.get_credentials(name)
        return creds.get("private_key")
    
    def delete_credentials(self, name: str) -> None:
        """Delete credentials."""
        with self._lock:
            store = self._load_store()
            if name in store:
                del store[name]
                self._save_store(store)
                if name in self._cache:
                    del self._cache[name]
                logger.info(f"Deleted credentials: {name}")
    
    def list_credentials(self) -> List[str]:
        """List available credential names."""
        store = self._load_store()
        return list(store.keys())
    
    # ------------------------------------------------------------------ #
    # Internal methods
    # ------------------------------------------------------------------ #
    
    def _load_store(self) -> Dict[str, str]:
        """Load encrypted store."""
        if not os.path.exists(self.store_path):
            return {}
        
        try:
            with open(self.store_path, "rb") as f:
                encrypted_data = f.read()
            
            decrypted = self._cipher.decrypt(encrypted_data)
            return json.loads(decrypted.decode())
        except Exception as e:
            logger.error(f"Failed to load store: {e}")
            return {}
    
    def _save_store(self, data: Dict[str, str]) -> None:
        """Save encrypted store."""
        json_data = json.dumps(data)
        encrypted = self._cipher.encrypt(json_data.encode())
        
        with open(self.store_path, "wb") as f:
            f.write(encrypted)
        
        os.chmod(self.store_path, 0o600)
    
    def _store(self, name: str, data: Dict[str, Any]) -> None:
        """Store encrypted data."""
        with self._lock:
            store = self._load_store()
            store[name] = json.dumps(data)
            self._save_store(store)
            
            # Clear cache
            if name in self._cache:
                del self._cache[name]
    
    def _get(self, name: str) -> Dict[str, Any]:
        """Get decrypted data."""
        with self._lock:
            # Check cache
            if name in self._cache:
                return self._cache[name].copy()
            
            store = self._load_store()
            if name not in store:
                raise CredentialManagerError(f"Credentials not found: {name}")
            
            try:
                data = json.loads(store[name])
                self._cache[name] = data.copy()
                return data
            except json.JSONDecodeError:
                raise CredentialManagerError(f"Corrupted credentials: {name}")
    
    def clear_cache(self) -> None:
        """Clear in-memory cache."""
        with self._lock:
            self._cache.clear()
    
    def get_ssh_credentials(self, name: str) -> Dict[str, Any]:
        """
        Get credentials in SSHCredentials format.
        
        Returns:
            dict with host, port, username, password, private_key_path
        """
        creds = self.get_credentials(name)
        
        result = {
            "host": creds.get("host"),
            "port": creds.get("port", 22),
            "username": creds.get("username", "root"),
        }
        
        if "password" in creds and creds["password"]:
            result["password"] = creds["password"]
        
        if "private_key" in creds and creds["private_key"]:
            # Save private key to temporary file
            import tempfile
            import os
            
            key_path = os.path.join(tempfile.gettempdir(), f"ssh_key_{name}.pem")
            with open(key_path, "w") as f:
                f.write(creds["private_key"])
            os.chmod(key_path, 0o600)
            result["private_key_path"] = key_path
        
        return result
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.clear_cache()


if __name__ == "__main__":
    # Test credential manager
    cm = CredentialManager("test_creds.encrypted")
    
    # Store credentials
    cm.store_credentials(
        name="test-vps",
        host="1.2.3.4",
        username="root",
        password="secret123",
        port=22,
    )
    
    # Get credentials
    creds = cm.get_credentials("test-vps")
    print(f"Retrieved: {creds}")
    
    # Get SSH format
    ssh_creds = cm.get_ssh_credentials("test-vps")
    print(f"SSH format: {ssh_creds}")
    
    # Cleanup
    cm.delete_credentials("test-vps")
    os.remove("test_creds.encrypted")
    os.remove("test_creds.encrypted.key")
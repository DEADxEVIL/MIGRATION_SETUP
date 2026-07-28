from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict, Any


@dataclass(frozen=True)
class ErrorPattern:
    """A known error pattern with remediation."""
    pattern: str  # Regex pattern (case-insensitive)
    category: str
    fix_commands: Tuple[str, ...]
    description: str
    confidence: float  # 0.0 - 1.0
    safe_to_auto_execute: bool
    retry_original: bool
    
    def __post_init__(self):
        self._compiled = None
    
    def compiled(self) -> re.Pattern:
        if not hasattr(self, '_compiled') or self._compiled is None:
            object.__setattr__(self, '_compiled', re.compile(self.pattern, re.IGNORECASE))
        return self._compiled


ERROR_CATEGORIES = (
    "missing_package",
    "permission_denied",
    "dependency_conflict",
    "network_timeout",
    "disk_full",
    "python_module_missing",
    "apt_lock",
    "pip_lock",
    "dns_failure",
    "ssl_failure",
    "git_clone_failure",
    "ssh_timeout",
    "no_space_left",
    "python_version_mismatch",
    "service_failure",
    "memory_exhaustion",
    "port_in_use",
    "file_not_found",
)


# Full error pattern library
ERROR_PATTERNS: List[ErrorPattern] = []

# Python module issues
ERROR_PATTERNS.extend([
    ErrorPattern(
        pattern=r"no module named ['\"]?dotenv['\"]?",
        category="python_module_missing",
        fix_commands=("pip install python-dotenv",),
        description="python-dotenv package is not installed",
        confidence=0.97,
        safe_to_auto_execute=True,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"no module named ['\"]?([a-zA-Z0-9_\-\.]+)['\"]?",
        category="python_module_missing",
        fix_commands=("pip install {module}",),
        description="Python module missing - install via pip",
        confidence=0.85,
        safe_to_auto_execute=True,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"modulenotfounderror",
        category="python_module_missing",
        fix_commands=("pip install -r requirements.txt",),
        description="Module not found - install requirements",
        confidence=0.75,
        safe_to_auto_execute=True,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"importerror: (?:dll load failed|cannot import name)",
        category="dependency_conflict",
        fix_commands=("pip install --force-reinstall --no-cache-dir {module}",),
        description="Import failed - reinstalling package",
        confidence=0.60,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"ensurepip is not available",
        category="missing_package",
        fix_commands=("apt-get update", "apt-get install -y python3-venv"),
        description="python3-venv required for virtual environments",
        confidence=0.98,
        safe_to_auto_execute=True,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"command 'python3(\.\d+)?' not found|command not found: python3",
        category="missing_package",
        fix_commands=("apt-get update", "apt-get install -y python3"),
        description="Python3 interpreter missing",
        confidence=0.95,
        safe_to_auto_execute=True,
        retry_original=True,
    ),
])

# Compiler/build issues
ERROR_PATTERNS.extend([
    ErrorPattern(
        pattern=r"error: microsoft visual c\+\+|error: command '.*gcc' failed|unable to execute 'gcc'|error: command 'gcc' failed",
        category="missing_package",
        fix_commands=("apt-get update", "apt-get install -y build-essential gcc g++"),
        description="C compiler toolchain required for native extensions",
        confidence=0.90,
        safe_to_auto_execute=True,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"python\.h: no such file or directory",
        category="missing_package",
        fix_commands=("apt-get update", "apt-get install -y python3-dev"),
        description="Python development headers missing",
        confidence=0.95,
        safe_to_auto_execute=True,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"ffi\.h: no such file or directory",
        category="missing_package",
        fix_commands=("apt-get update", "apt-get install -y libffi-dev"),
        description="libffi headers missing for cffi packages",
        confidence=0.90,
        safe_to_auto_execute=True,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"ssl\.h: no such file or directory|openssl/opensslv\.h",
        category="missing_package",
        fix_commands=("apt-get update", "apt-get install -y libssl-dev"),
        description="OpenSSL development headers missing",
        confidence=0.90,
        safe_to_auto_execute=True,
        retry_original=True,
    ),
])

# APT/Dpkg issues
ERROR_PATTERNS.extend([
    ErrorPattern(
        pattern=r"unable to locate package",
        category="missing_package",
        fix_commands=("apt-get update",),
        description="Package not found - update package list",
        confidence=0.85,
        safe_to_auto_execute=True,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"could not get lock /var/lib/dpkg/lock|could not get lock /var/lib/apt/lists/lock",
        category="apt_lock",
        fix_commands=("sleep 10", "fuser -k /var/lib/dpkg/lock-frontend || true", "dpkg --configure -a"),
        description="APT lock held by another process",
        confidence=0.85,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"dpkg was interrupted, you must manually run",
        category="apt_lock",
        fix_commands=("dpkg --configure -a",),
        description="DPKG interrupted - reconfigure",
        confidence=0.90,
        safe_to_auto_execute=True,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"e: unmet dependencies|you have held broken packages",
        category="dependency_conflict",
        fix_commands=("apt-get install -f -y", "apt-get autoremove -y"),
        description="Unmet package dependencies",
        confidence=0.80,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"failed to fetch .*(403|404)",
        category="network_timeout",
        fix_commands=("apt-get update",),
        description="APT mirror HTTP error",
        confidence=0.60,
        safe_to_auto_execute=True,
        retry_original=True,
    ),
])

# PIP issues
ERROR_PATTERNS.extend([
    ErrorPattern(
        pattern=r"could not find a version that satisfies the requirement",
        category="dependency_conflict",
        fix_commands=("pip install --upgrade pip", "pip install {package} --index-url https://pypi.org/simple"),
        description="PIP cannot find compatible version",
        confidence=0.70,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"resolutionimpossible|conflicting dependencies|incompatible.*versions? of",
        category="dependency_conflict",
        fix_commands=("pip install --upgrade pip setuptools wheel",),
        description="Dependency resolution conflict",
        confidence=0.65,
        safe_to_auto_execute=False,
        retry_original=False,
    ),
    ErrorPattern(
        pattern=r"error: externally-managed-environment",
        category="dependency_conflict",
        fix_commands=("python3 -m venv .venv", "source .venv/bin/activate && pip install -r requirements.txt"),
        description="Externally managed environment - use venv",
        confidence=0.95,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"could not open requirements file|no such file or directory: 'requirements.txt'",
        category="file_not_found",
        fix_commands=("touch requirements.txt",),
        description="requirements.txt not found",
        confidence=0.60,
        safe_to_auto_execute=False,
        retry_original=False,
    ),
    ErrorPattern(
        pattern=r"another instance of pip is running|could not acquire lock.*pip",
        category="pip_lock",
        fix_commands=("sleep 10",),
        description="PIP lock held by another process",
        confidence=0.80,
        safe_to_auto_execute=True,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"read timed out|connection timed out.*pypi|httpsconnectionpool.*read timed out",
        category="network_timeout",
        fix_commands=("pip install --default-timeout=120 {package}",),
        description="PIP timeout - retry with longer timeout",
        confidence=0.75,
        safe_to_auto_execute=True,
        retry_original=True,
    ),
])

# Network/DNS issues
ERROR_PATTERNS.extend([
    ErrorPattern(
        pattern=r"temporary failure in name resolution|name or service not known|failed to resolve",
        category="dns_failure",
        fix_commands=("cat /etc/resolv.conf", "echo 'nameserver 8.8.8.8' >> /etc/resolv.conf"),
        description="DNS resolution failure",
        confidence=0.85,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"could not resolve host",
        category="dns_failure",
        fix_commands=("ping -c1 8.8.8.8", "cat /etc/resolv.conf"),
        description="Host resolution failure",
        confidence=0.85,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"network is unreachable",
        category="network_timeout",
        fix_commands=("ip route", "systemctl restart networking"),
        description="Network unreachable",
        confidence=0.70,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"connection refused",
        category="network_timeout",
        fix_commands=("wait_for_service",),
        description="Connection refused - service not running",
        confidence=0.60,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
])

# SSL issues
ERROR_PATTERNS.extend([
    ErrorPattern(
        pattern=r"ssl.*certificate verify failed|certificate_verify_failed",
        category="ssl_failure",
        fix_commands=("apt-get install -y ca-certificates", "update-ca-certificates"),
        description="SSL certificate verification failed",
        confidence=0.80,
        safe_to_auto_execute=True,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"ssl: wrong_version_number|sslerror",
        category="ssl_failure",
        fix_commands=("apt-get install -y openssl", "openssl version"),
        description="SSL handshake error",
        confidence=0.55,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
])

# Git issues
ERROR_PATTERNS.extend([
    ErrorPattern(
        pattern=r"fatal: repository .* not found",
        category="git_clone_failure",
        fix_commands=("verify repository URL and access permissions",),
        description="Git repository not found or inaccessible",
        confidence=0.75,
        safe_to_auto_execute=False,
        retry_original=False,
    ),
    ErrorPattern(
        pattern=r"fatal: could not read username|permission denied \(publickey\)",
        category="git_clone_failure",
        fix_commands=("ssh-add -l", "verify deploy key or credentials"),
        description="Git authentication failure",
        confidence=0.75,
        safe_to_auto_execute=False,
        retry_original=False,
    ),
    ErrorPattern(
        pattern=r"fatal: unable to access.*could not resolve host",
        category="dns_failure",
        fix_commands=("cat /etc/resolv.conf",),
        description="Git DNS resolution failure",
        confidence=0.80,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"fatal: destination path .* already exists and is not an empty directory",
        category="file_not_found",
        fix_commands=("rm -rf {destination}",),
        description="Git clone destination not empty",
        confidence=0.70,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
])

# Disk/Memory issues
ERROR_PATTERNS.extend([
    ErrorPattern(
        pattern=r"no space left on device",
        category="disk_full",
        fix_commands=(
            "df -h",
            "apt-get clean",
            "journalctl --vacuum-size=100M",
            "find /tmp -type f -mtime +3 -delete",
        ),
        description="No disk space left",
        confidence=0.95,
        safe_to_auto_execute=True,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"disk quota exceeded",
        category="disk_full",
        fix_commands=("df -h", "quota -v"),
        description="Disk quota exceeded",
        confidence=0.80,
        safe_to_auto_execute=False,
        retry_original=False,
    ),
    ErrorPattern(
        pattern=r"cannot allocate memory|out of memory|oom.?killed?",
        category="memory_exhaustion",
        fix_commands=("free -h", "swapon --show", "fallocate -l 2G /swapfile && mkswap /swapfile && swapon /swapfile"),
        description="OOM - process killed due to memory exhaustion",
        confidence=0.80,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
])

# Permission issues
ERROR_PATTERNS.extend([
    ErrorPattern(
        pattern=r"permission denied",
        category="permission_denied",
        fix_commands=("chown -R $(whoami) {path}", "chmod -R u+rwX {path}"),
        description="Permission denied - need access",
        confidence=0.75,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"operation not permitted",
        category="permission_denied",
        fix_commands=("run the command with sudo",),
        description="Operation not permitted - need elevated privileges",
        confidence=0.65,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"sudo: a password is required|sudo: no tty present",
        category="permission_denied",
        fix_commands=("configure passwordless sudo",),
        description="Sudo requires password",
        confidence=0.85,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
])

# SSH issues
ERROR_PATTERNS.extend([
    ErrorPattern(
        pattern=r"connection timed out.*port 22|ssh: connect to host .* port 22",
        category="ssh_timeout",
        fix_commands=("verify firewall rules and that sshd is running",),
        description="SSH connection timeout",
        confidence=0.75,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"host key verification failed",
        category="ssh_timeout",
        fix_commands=("ssh-keygen -R {host}",),
        description="SSH host key mismatch",
        confidence=0.85,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"kex_exchange_identification|connection reset by peer",
        category="ssh_timeout",
        fix_commands=("retry after short delay",),
        description="SSH key exchange failed",
        confidence=0.60,
        safe_to_auto_execute=True,
        retry_original=True,
    ),
])

# Python version issues
ERROR_PATTERNS.extend([
    ErrorPattern(
        pattern=r"requires python '?[<>=!~]+\s*3\.\d+|python_requires",
        category="python_version_mismatch",
        fix_commands=("apt-get install -y python3.11 python3.11-venv",),
        description="Python version requirement not met",
        confidence=0.70,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"syntaxerror.*walrus|f-string.*expression part cannot include a backslash",
        category="python_version_mismatch",
        fix_commands=("verify correct Python interpreter version",),
        description="Syntax requires newer Python version",
        confidence=0.55,
        safe_to_auto_execute=False,
        retry_original=False,
    ),
])

# Service/Port issues
ERROR_PATTERNS.extend([
    ErrorPattern(
        pattern=r"address already in use",
        category="port_in_use",
        fix_commands=("ss -ltnp | grep {port}", "kill $(lsof -t -i:{port})"),
        description="Port already in use",
        confidence=0.85,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
    ErrorPattern(
        pattern=r"failed to start .*\.service|unit .*\.service not found",
        category="service_failure",
        fix_commands=("systemctl daemon-reload", "systemctl restart {service}", "journalctl -u {service} -n 50"),
        description="Systemd service failed to start",
        confidence=0.65,
        safe_to_auto_execute=False,
        retry_original=True,
    ),
])


def find_matches(text: str) -> List[ErrorPattern]:
    """Find all error patterns matching the given text."""
    matches = []
    for pattern in ERROR_PATTERNS:
        if pattern.compiled().search(text):
            matches.append(pattern)
    return sorted(matches, key=lambda p: p.confidence, reverse=True)


def best_match(text: str) -> Optional[ErrorPattern]:
    """Return the highest confidence match."""
    matches = find_matches(text)
    return matches[0] if matches else None


# Legacy mapping for compatibility
SIMPLE_PATTERN_MAP: Dict[str, str] = {
    ep.pattern: (ep.fix_commands[0] if ep.fix_commands else "manual investigation required")
    for ep in ERROR_PATTERNS
}


if __name__ == "__main__":
    # Test pattern matching
    test_text = """
    Traceback (most recent call last):
    File "main.py", line 10, in <module>
    ImportError: No module named 'dotenv'
    """
    
    matches = find_matches(test_text)
    print(f"Found {len(matches)} matches:")
    for m in matches[:3]:
        print(f"  {m.category}: {m.description} (confidence: {m.confidence})")
    
    best = best_match(test_text)
    if best:
        print(f"\nBest match: {best.category} - {best.fix_commands[0]}")
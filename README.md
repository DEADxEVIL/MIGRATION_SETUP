# VPS Migration System

A production-grade, modular Python 3.11+ system for migrating a project (e.g. a
Telegram bot) from a source VPS to a destination VPS, with automatic
recovery from common provisioning failures, structured logging, resumable
state, and rollback safety.

## Modules

| File | Purpose |
|---|---|
| `ssh_manager.py` | Thread-safe SSH/SFTP client (Paramiko): password/key auth, auto-reconnect, timeouts, sudo, real-time streaming, recursive SFTP upload/download, checksum verification, retries. Standalone and reusable in any script. |
| `migration_agent.py` | Multi-threaded TCP JSON agent that runs **on the destination VPS**: executes shell/python/apt/pip commands, reports CPU/RAM/disk/process stats, detects reboot, emits a 5-second heartbeat, can restart itself. |
| `dependency_installer.py` | Installs all project dependencies (`requirements.txt`, `requirements-dev.txt`, venv, build toolchain, git/ffmpeg/curl/wget/zip/unzip), detects missing packages and conflicts, retries failures. |
| `self_healing_engine.py` | Given a failed command, diagnoses it, executes safe fixes, retries the original command, keeps a never-repeat fix history, returns full JSON. |
| `ai_diagnosis.py` | Pure diagnosis (no execution): root cause, fixes, confidence, risk, retry recommendation, success probability — always JSON. |
| `error_pattern_library.py` | 45+ curated regex failure signatures (missing packages, permission errors, apt/pip locks, DNS/SSL/git/SSH failures, disk/memory exhaustion, etc.) with remediation commands. |
| `rollback_manager.py` | Stops/removes the destination deployment, restores prior state, supports partial and complete rollback. Never touches the source VPS. |
| `state_manager.py` | SQLite-backed persistence of every migration step (status, retries, stdout/stderr, timestamps) — enables resume after reboot or crash. |
| `process_manager.py` | Start/stop/restart processes via systemd, screen, tmux, or nohup; crash, zombie, and duplicate-process detection. |
| `health_checker.py` | Verifies the migrated service: process alive, CPU/RAM, Telegram polling started, no tracebacks, no restart loop, uptime, heartbeat, network connectivity. |
| `migration_manager.py` | Central orchestrator wiring every module above into one step-by-step, resumable, cancellable, schedulable migration workflow with concurrent log streaming. |
| `logger_service.py` | Centralized logging configuration (console + rotating file) shared by every module. |
| `retry_engine.py` | Generic retry helper with fixed/linear/exponential/jittered backoff strategies. |

## Quick start

```python
from ssh_manager import SSHManager, SSHCredentials

creds = SSHCredentials(host="203.0.113.10", username="root", password="secret")
with SSHManager(creds, connect_timeout=10, command_timeout=60) as ssh:
    result = ssh.run("uname -a")
    print(result.stdout, result.exit_code)

    ssh.upload_directory("./my_project", "/opt/my_project", verify_checksum=True)
```

```python
from migration_manager import MigrationManager, MigrationPlan, MigrationStep
from ssh_manager import SSHCredentials

plan = MigrationPlan(
    migration_id="mig-2026-07-26-001",
    source=SSHCredentials(host="1.2.3.4", username="root", password="..."),
    destination=SSHCredentials(host="5.6.7.8", username="root", private_key_path="~/.ssh/id_ed25519"),
    steps=[
        MigrationStep(name="apt_update", command="apt-get update -y", use_sudo=True),
        MigrationStep(name="clone_repo", command="git clone https://github.com/org/bot.git /opt/bot"),
        MigrationStep(name="install_deps", command="cd /opt/bot && pip install -r requirements.txt"),
        MigrationStep(name="start_service", command="systemctl start migrated-bot", use_sudo=True),
    ],
)

manager = MigrationManager(plan)
manager.subscribe_logs(lambda step, line: print(f"[{step}] {line}"))
success = manager.run()
manager.shutdown()
```

## Running the destination agent

```bash
python3 migration_agent.py --port 9911
```

Send it JSON commands over a TCP socket, one per line:

```json
{"type": "shell", "command": "df -h"}
{"type": "status"}
{"type": "processes", "limit": 20}
```

## Design notes

- All modules accept an **injected executor callback** rather than hard-wiring
  to SSH, so every module (dependency installer, process manager, health
  checker, rollback manager, self-healing engine) works identically whether
  the target is local or remote.
- `error_pattern_library.py` and `ai_diagnosis.py` are intentionally
  decoupled: the library is pure data, the diagnosis module is pure
  reasoning, and `self_healing_engine.py` is the only module that executes
  anything.
- `state_manager.py` is the single source of truth for resumability — the
  orchestrator never keeps step progress only in memory.

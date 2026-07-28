#!/usr/bin/env python3
"""
migrate.py
==========

CLI entry point for running migrations of the Content Publisher Bot.

Usage:
    python migrate.py --plan plan.json
    python migrate.py --status
    python migrate.py --rollback --migration-id mig-001
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Add migration_system to path
sys.path.insert(0, str(Path(__file__).parent))

from migration_system.logger_service import configure_root_logging, get_logger
from migration_system.state_manager import StateManager
from migration_system.ssh_manager import SSHCredentials
from migration_system.migration_manager import MigrationPlan, MigrationStep, MigrationManager

# Configure logging
configure_root_logging(level=logging.INFO)
logger = get_logger("migrate")


def load_plan(plan_file: str) -> dict:
    """Load migration plan from JSON file."""
    with open(plan_file, 'r') as f:
        return json.load(f)


def create_plan_from_config(plan_data: dict) -> MigrationPlan:
    """Create MigrationPlan from config dict."""
    # Parse credentials
    source = SSHCredentials(
        host=plan_data['source']['host'],
        port=plan_data['source'].get('port', 22),
        username=plan_data['source'].get('username', 'root'),
        password=plan_data['source'].get('password'),
        private_key_path=plan_data['source'].get('private_key_path'),
    )
    
    destination = SSHCredentials(
        host=plan_data['destination']['host'],
        port=plan_data['destination'].get('port', 22),
        username=plan_data['destination'].get('username', 'root'),
        password=plan_data['destination'].get('password'),
        private_key_path=plan_data['destination'].get('private_key_path'),
    )
    
    # Parse steps
    steps = []
    for step_data in plan_data.get('steps', []):
        steps.append(MigrationStep(
            name=step_data['name'],
            command=step_data['command'],
            use_sudo=step_data.get('use_sudo', False),
            timeout=step_data.get('timeout', 300),
            critical=step_data.get('critical', True),
            retries=step_data.get('retries', 2),
        ))
    
    return MigrationPlan(
        migration_id=plan_data.get('migration_id', f"mig-{int(time.time())}"),
        bot_name=plan_data.get('bot_name', 'ContentPublisherBot'),
        source=source,
        destination=destination,
        steps=steps,
        project_directory=plan_data.get('project_directory', '/opt/publisher_bot'),
        bot_service_name=plan_data.get('bot_service_name', 'publisher-bot'),
        log_path=plan_data.get('log_path', '/var/log/publisher-bot.log'),
        scheduled_start_time=plan_data.get('scheduled_start_time'),
        metadata=plan_data.get('metadata', {}),
    )


def generate_plan_from_bot() -> dict:
    """
    Generate a migration plan automatically from current bot configuration.
    Reads from .env and current directory structure.
    """
    # Read .env
    env_vars = {}
    if os.path.exists('.env'):
        with open('.env', 'r') as f:
            for line in f:
                if '=' in line and not line.startswith('#'):
                    key, value = line.strip().split('=', 1)
                    env_vars[key] = value
    
    # Get current directory
    project_dir = os.getcwd()
    bot_name = os.path.basename(project_dir)
    
    # Generate steps
    steps = [
        {
            "name": "create_project_dir",
            "command": f"mkdir -p /opt/{bot_name}",
            "use_sudo": True,
            "timeout": 30,
            "critical": True
        },
        {
            "name": "upload_project",
            "command": f"rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' {project_dir}/ /opt/{bot_name}/",
            "use_sudo": False,
            "timeout": 300,
            "critical": True
        },
        {
            "name": "create_venv",
            "command": f"cd /opt/{bot_name} && python3 -m venv .venv",
            "use_sudo": False,
            "timeout": 60,
            "critical": True
        },
        {
            "name": "install_dependencies",
            "command": f"cd /opt/{bot_name} && .venv/bin/pip install -r requirements.txt",
            "use_sudo": False,
            "timeout": 300,
            "critical": True
        },
        {
            "name": "setup_env",
            "command": f"cd /opt/{bot_name} && cat > .env << 'ENV_EOF'\n" + 
                      "\n".join([f"{k}={v}" for k, v in env_vars.items()]) + 
                      "\nENV_EOF",
            "use_sudo": False,
            "timeout": 30,
            "critical": True
        },
        {
            "name": "create_service",
            "command": (
                f"cat > /etc/systemd/system/{bot_name}.service << 'SERVICE_EOF'\n"
                f"[Unit]\n"
                f"Description={bot_name} Bot\n"
                f"After=network.target\n\n"
                f"[Service]\n"
                f"Type=simple\n"
                f"User=root\n"
                f"WorkingDirectory=/opt/{bot_name}\n"
                f"ExecStart=/opt/{bot_name}/.venv/bin/python3 /opt/{bot_name}/main.py\n"
                f"Restart=always\n"
                f"RestartSec=5\n\n"
                f"[Install]\n"
                f"WantedBy=multi-user.target\n"
                f"SERVICE_EOF\n"
                f"systemctl daemon-reload"
            ),
            "use_sudo": True,
            "timeout": 30,
            "critical": True
        },
        {
            "name": "start_bot",
            "command": f"systemctl start {bot_name}",
            "use_sudo": True,
            "timeout": 60,
            "critical": True
        },
        {
            "name": "health_check",
            "command": f"sleep 3 && systemctl is-active {bot_name}",
            "use_sudo": True,
            "timeout": 30,
            "critical": True
        }
    ]
    
    return {
        "migration_id": f"mig-{bot_name}-{int(time.time())}",
        "bot_name": bot_name,
        "source": {
            "host": "SOURCE_VPS_IP_HERE",
            "port": 22,
            "username": "root",
            "password": "SOURCE_PASSWORD_HERE"
        },
        "destination": {
            "host": "DESTINATION_VPS_IP_HERE",
            "port": 22,
            "username": "root",
            "password": "DESTINATION_PASSWORD_HERE"
        },
        "project_directory": f"/opt/{bot_name}",
        "bot_service_name": bot_name,
        "log_path": f"/var/log/{bot_name}.log",
        "steps": steps,
        "metadata": {
            "source_bot": bot_name,
            "python_version": sys.version,
            "generated_at": time.ctime()
        }
    }


def main():
    parser = argparse.ArgumentParser(
        description="Migrate Content Publisher Bot to another VPS"
    )
    
    parser.add_argument("--plan", help="Migration plan JSON file")
    parser.add_argument("--generate-plan", action="store_true", 
                       help="Generate a plan template from current bot")
    parser.add_argument("--status", help="Check migration status (provide migration ID)")
    parser.add_argument("--rollback", help="Rollback migration (provide migration ID)")
    parser.add_argument("--state-db", default="migration_state.db", 
                       help="State database path")
    parser.add_argument("--dry-run", action="store_true", 
                       help="Dry run without executing")
    
    args = parser.parse_args()
    
    if args.generate_plan:
        print("Generating migration plan from current bot...")
        plan = generate_plan_from_bot()
        print("\nPlan generated. Fill in source/destination VPS credentials:")
        print(json.dumps(plan, indent=2))
        
        # Save to file
        with open("migration_plan.json", "w") as f:
            json.dump(plan, f, indent=2)
        print("\n✅ Plan saved to migration_plan.json")
        print("Edit the file to add VPS credentials, then run:")
        print("  python migrate.py --plan migration_plan.json")
        return
    
    if args.status:
        show_status(args)
        return
    
    if args.rollback:
        run_rollback(args)
        return
    
    if not args.plan:
        parser.print_help()
        print("\nTip: First run --generate-plan to create a plan template")
        return
    
    run_migration(args)


def run_migration(args):
    """Run migration from plan file."""
    try:
        plan_data = load_plan(args.plan)
    except Exception as e:
        print(f"❌ Error loading plan: {e}")
        sys.exit(1)
    
    if args.dry_run:
        print("🔬 DRY RUN MODE")
        print("===============")
        print(f"Migration: {plan_data.get('migration_id', 'unknown')}")
        print(f"Bot: {plan_data.get('bot_name', 'unknown')}")
        print(f"Source: {plan_data.get('source', {}).get('host', 'unknown')}")
        print(f"Destination: {plan_data.get('destination', {}).get('host', 'unknown')}")
        print(f"Steps: {len(plan_data.get('steps', []))}")
        for i, step in enumerate(plan_data.get('steps', [])):
            print(f"  {i+1}. {step['name']}: {step['command'][:50]}...")
        print("\n✅ Dry run complete")
        return
    
    print(f"🚀 Starting migration...")
    print(f"   ID: {plan_data.get('migration_id', 'unknown')}")
    print(f"   Bot: {plan_data.get('bot_name', 'unknown')}")
    print(f"   From: {plan_data.get('source', {}).get('host', 'unknown')}")
    print(f"   To: {plan_data.get('destination', {}).get('host', 'unknown')}")
    print()
    
    # Create plan
    plan = create_plan_from_config(plan_data)
    
    # Create manager
    manager = MigrationManager(
        plan,
        state_db_path=args.state_db,
        max_step_retries=2,
        min_confidence_to_auto_heal=0.6,
        allow_risky_fixes=False,
    )
    
    # Log subscriber
    def log_callback(step: str, message: str):
        print(f"[{step}] {message}")
    
    manager.subscribe_logs(log_callback)
    
    try:
        success = manager.run()
        if success:
            print(f"\n✅ Migration {plan.migration_id} COMPLETED SUCCESSFULLY!")
            print(f"   Bot is now running on destination VPS")
        else:
            print(f"\n❌ Migration {plan.migration_id} FAILED!")
            print("   Check logs for details")
    except KeyboardInterrupt:
        print("\n⏹️ Migration interrupted by user")
        manager.cancel()
    finally:
        manager.shutdown()


def show_status(args):
    """Show migration status."""
    state = StateManager(args.state_db)
    
    migration = state.get_migration(args.status)
    if not migration:
        print(f"Migration {args.status} not found")
        state.close()
        return
    
    steps = state.get_steps(args.status)
    
    print(f"\n📊 Migration Status: {args.status}")
    print(f"   Bot: {migration.bot_name}")
    print(f"   Status: {migration.status.value.upper()}")
    print(f"   Started: {time.ctime(migration.started_at)}")
    if migration.completed_at:
        print(f"   Completed: {time.ctime(migration.completed_at)}")
    print(f"   Steps: {migration.completed_steps}/{migration.total_steps}")
    print(f"   Failed: {migration.failed_steps}")
    print(f"   Healed: {migration.healed_steps}")
    
    if steps:
        print("\n   Step Status:")
        for step in steps[-10:]:
            icon = "✅" if step.status.value == "succeeded" else "❌" if step.status.value == "failed" else "⏳"
            print(f"     {icon} {step.step_name}: {step.status.value}")
    
    state.close()


def run_rollback(args):
    """Rollback a migration."""
    state = StateManager(args.state_db)
    
    migration = state.get_migration(args.rollback)
    if not migration:
        print(f"Migration {args.rollback} not found")
        state.close()
        return
    
    print(f"\n🔄 Rolling back: {args.rollback}")
    print(f"   Destination: {migration.destination_vps}")
    print(f"   This will NOT affect source VPS")
    
    response = input("\nAre you sure? (yes/no): ")
    if response.lower() != 'yes':
        print("Cancelled")
        state.close()
        return
    
    # Update status
    from migration_system.state_manager import MigrationStatus
    state.update_migration_status(
        args.rollback,
        MigrationStatus.ROLLED_BACK,
        completed_at=time.time()
    )
    
    print("✅ Rollback initiated. Check destination VPS for cleanup.")
    state.close()


if __name__ == "__main__":
    main()
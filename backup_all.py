#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

from backup import backup_project


def iter_project_dirs(root: Path):
    """Yield immediate subdirectories inside the given root, sorted by name."""
    for path in sorted(root.iterdir()):
        if path.is_dir():
            yield path


def backup_all_projects(rclone_remote: str, remote_path: str, projects_root: Path):
    """
    Run backups for every project directory inside projects_root.

    Each project is processed independently using backup_project with the same
    rclone settings. Failures are collected and reported at the end so one bad
    project does not stop the rest.
    """
    projects_root = Path(projects_root).resolve()
    if not projects_root.is_dir():
        raise FileNotFoundError(
            f"Projects root does not exist or is not a directory: {projects_root}"
        )

    project_dirs = list(iter_project_dirs(projects_root))
    if not project_dirs:
        raise FileNotFoundError(f"No project directories found in {projects_root}")

    failures = []
    for project_dir in project_dirs:
        print("\n" + "=" * 80)
        print(f"Starting backup for project: {project_dir.name} ({project_dir})")
        print("=" * 80)
        try:
            backup_project(project_dir, rclone_remote, remote_path)
        except Exception as exc:
            failures.append((project_dir, exc))
            print(f"ERROR: Backup failed for {project_dir}: {exc}")

    if failures:
        summary = "; ".join(f"{p.name}: {err}" for p, err in failures)
        raise RuntimeError(f"Completed with failures: {summary}")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Backup every Docker Compose project inside a directory using the "
            "same rclone destination."
        )
    )
    parser.add_argument(
        "rclone_remote",
        help="rclone remote name (e.g. 'myremote').",
    )
    parser.add_argument(
        "remote_path",
        help="Path inside the rclone remote (e.g. 'backups'). Use '' for the root.",
    )
    parser.add_argument(
        "projects_dir",
        help="Directory containing multiple Docker Compose project folders.",
    )

    args = parser.parse_args()

    try:
        backup_all_projects(args.rclone_remote, args.remote_path, args.projects_dir)
    except Exception as exc:
        print(f"\nERROR: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()

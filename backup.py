#!/usr/bin/env python3
import argparse
import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml  # pip install pyyaml
except ImportError:
    print("ERROR: This script requires PyYAML. Install it with: pip install pyyaml")
    sys.exit(1)


def run_cmd(cmd, cwd=None):
    """Run a shell command and raise if it fails."""
    print(f"+ Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {' '.join(cmd)}")


def find_compose_file(project_dir: Path) -> Path:
    """Find a docker-compose file in the given directory."""
    candidates = [
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    ]
    for name in candidates:
        candidate = project_dir / name
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"No docker-compose file found in {project_dir}. "
        f"Tried: {', '.join(candidates)}"
    )


def extract_named_volumes(compose_path: Path):
    """
    Extract the Docker volume names used by services.

    Rules:
    - If service defines a volume reference like "volkey:/path"
    - Check top-level volumes for a matching key
    - If that volume defines "name:", use it
    - Otherwise use the key itself (default volume name)
    """
    with compose_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    services = (data or {}).get("services", {})
    top_volumes = (data or {}).get("volumes", {})
    named_volumes = set()

    for _, svc in services.items():
        svc_vols = (svc or {}).get("volumes", [])
        for v in svc_vols:

            # Parse volume source (left side before ":")
            if isinstance(v, str):
                volkey = v.split(":", 1)[0]
            elif isinstance(v, dict):
                volkey = v.get("source")
            else:
                continue

            if not volkey:
                continue

            # Skip host paths: they usually contain "/" or start with "." or "~"
            if "/" in volkey or volkey.startswith(".") or volkey.startswith("~"):
                continue

            # Check if this volume is defined in the top-level volumes section
            if volkey in top_volumes:
                vol_def = top_volumes[volkey]

                # If "name:" field exists, use it
                if isinstance(vol_def, dict) and "name" in vol_def:
                    named_volumes.add(vol_def["name"])
                    continue

            # Default: use the volume key (Docker auto-creates it)
            named_volumes.add(volkey)

    return sorted(named_volumes)



def detect_docker_compose_command():
    """
    Prefer 'docker compose' (plugin, v2). Fall back to 'docker-compose' if needed.
    """
    try:
        result = subprocess.run(
            ["docker", "compose", "version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return ["docker", "compose"]
    except FileNotFoundError:
        pass

    try:
        result = subprocess.run(
            ["docker-compose", "version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return ["docker-compose"]
    except FileNotFoundError:
        pass

    raise RuntimeError(
        "Neither 'docker compose' nor 'docker-compose' was found. "
        "Please install Docker Compose."
    )


def create_project_zip(project_dir: Path, timestamp: str) -> Path:
    """
    Create a zip archive of the entire project directory.

    IMPORTANT: The zip is created in the *parent directory* of the project
    so that any later-created temp directories inside the project are NOT
    included in this archive.
    """
    project_name = project_dir.name
    zip_base = project_dir.parent / f"{project_name}-project-{timestamp}"

    print(f"Creating project zip of {project_dir} at {zip_base}.zip")
    shutil.make_archive(
        base_name=str(zip_base),
        format="zip",
        root_dir=str(project_dir),
    )
    return zip_base.with_suffix(".zip")


def export_volume(volume_name: str, temp_dir: Path, timestamp: str) -> Path:
    """
    Export a Docker named volume to a .tar.gz file inside the temp directory.
    """
    archive_name = f"volume-{volume_name}-{timestamp}.tar.gz"
    archive_path = temp_dir / archive_name

    cmd = [
        "docker", "run", "--rm",
        "-v", f"{volume_name}:/volume",
        "-v", f"{str(temp_dir)}:/backup",
        "alpine",
        "sh", "-c",
        f"cd /volume && tar czf /backup/{archive_name} ."
    ]
    run_cmd(cmd)
    return archive_path


def create_final_zip_from_temp(project_dir: Path, temp_dir: Path, timestamp: str) -> Path:
    """
    Zip everything in temp_dir into a single zip located in the project directory.
    Name: <project_folder>-<timestamp>.zip
    """
    project_name = project_dir.name
    final_base = project_dir / f"{project_name}-{timestamp}"

    print(f"Creating final backup zip from {temp_dir} at {final_base}.zip")
    shutil.make_archive(
        base_name=str(final_base),
        format="zip",
        root_dir=str(temp_dir),
    )
    return final_base.with_suffix(".zip")


def rclone_copy_file(file_path: Path, remote: str, remote_path: str):
    """
    Copy a single file to the remote:path using rclone copy.
    """
    if remote_path:
        dest = f"{remote}:{remote_path}"
    else:
        dest = f"{remote}:"

    cmd = [
        "rclone",
        "copy",
        str(file_path),
        dest,
        "--verbose",
    ]
    run_cmd(cmd)


def rotate_project_backups(remote: str, remote_path: str, keep: int):
    """
    Keep only the newest `keep` backup files in the given remote path.
    Older backups are deleted using rclone deletefile.
    """
    if keep < 1:
        raise ValueError("keep must be at least 1")

    target = f"{remote}:{remote_path}" if remote_path else f"{remote}:"
    print(f"Listing backups at {target} ...")

    result = subprocess.run(
        ["rclone", "lsjson", target, "--files-only"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = result.stderr.strip() or "unknown error"
        raise RuntimeError(f"Failed to list backups via rclone: {err}")

    try:
        entries = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Unable to parse rclone output as JSON: {exc}") from exc

    # Limit to zip files to avoid touching unexpected data in the same folder.
    backups = [
        entry for entry in entries
        if not entry.get("IsDir") and str(entry.get("Path", "")).endswith(".zip")
    ]

    def _parse_time(mod_time: str):
        if not mod_time:
            return datetime.datetime.min
        try:
            # rclone uses RFC3339 with a trailing Z
            return datetime.datetime.fromisoformat(mod_time.replace("Z", "+00:00"))
        except ValueError:
            return datetime.datetime.min

    backups.sort(key=lambda e: _parse_time(e.get("ModTime")))

    if len(backups) <= keep:
        print(f"Found {len(backups)} backups; nothing to delete (keep={keep}).")
        return

    to_delete = backups[:-keep]
    print(f"Keeping newest {keep} backups, deleting {len(to_delete)} older backups...")

    for entry in to_delete:
        rel_path = entry.get("Path") or entry.get("Name")
        if not rel_path:
            continue

        if remote_path:
            delete_target = f"{remote}:{remote_path.rstrip('/')}/{rel_path}"
        else:
            delete_target = f"{remote}:{rel_path}"

        print(f"Deleting {delete_target}")
        run_cmd(["rclone", "deletefile", delete_target])


def backup_project(project_dir, rclone_remote, remote_path, backups_to_keep: int = 4):
    """
    Run the full backup workflow. Accepts strings or Path-like objects.

    Returns the final zip Path (even though it is removed locally after upload).
    Raises on any failure; callers can catch to handle errors.
    """
    project_dir = Path(project_dir).resolve()
    if not project_dir.is_dir():
        raise FileNotFoundError(f"Project directory does not exist or is not a directory: {project_dir}")

    remote = rclone_remote

    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    project_name = project_dir.name

    # 1) Create zip of whole project BEFORE any temp directory exists inside it
    project_zip_outside = create_project_zip(project_dir, timestamp)

    # 2) Now create temp directory *inside* project
    temp_dir = project_dir / f".docker-backup-temp-{timestamp}"
    try:
        temp_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print(f"ERROR: Temp backup directory already exists (unexpected): {temp_dir}")
        sys.exit(1)

    # Move the project zip into temp_dir (so all exports live together)
    project_zip = temp_dir / project_zip_outside.name
    shutil.move(str(project_zip_outside), str(project_zip))

    final_zip_path = None

    try:
        compose_file = find_compose_file(project_dir)
        print(f"Using compose file: {compose_file}")
        volumes = extract_named_volumes(compose_file)

        if volumes:
            print(f"Detected named volumes: {', '.join(volumes)}")
        else:
            print("WARNING: No named volumes detected in the compose file.")

        docker_compose_cmd = detect_docker_compose_command()
        print(f"Using Docker Compose command: {' '.join(docker_compose_cmd)}")

        # 3) docker compose down
        print("Bringing services down...")
        run_cmd(docker_compose_cmd + ["down"], cwd=str(project_dir))

        # 4) Export each named volume into temp_dir
        exported_archives = []
        for vol in volumes:
            print(f"Exporting volume: {vol}")
            archive_path = export_volume(vol, temp_dir, timestamp)
            exported_archives.append(archive_path)
            print(f"Volume {vol} exported to: {archive_path}")

        # 5) docker compose up -d
        print("Bringing services back up...")
        run_cmd(docker_compose_cmd + ["up", "-d"], cwd=str(project_dir))

        # 6) Zip all exports (project zip + volume tarballs) into one final zip
        final_zip_path = create_final_zip_from_temp(project_dir, temp_dir, timestamp)
        print(f"Final backup zip created: {final_zip_path}")

        # 7) Copy final zip to rclone remote
        print(f"Copying final backup zip to rclone remote {remote}:{remote_path or ''} ...")
        rclone_copy_file(final_zip_path, remote, remote_path)
        print("rclone copy completed.")

        # 8) Remove local exports (temp directory with intermediate files)
        print(f"Removing temp backup directory: {temp_dir}")
        shutil.rmtree(temp_dir)

        # 9) Remove local final backup zip as well
        if final_zip_path.exists():
            print(f"Removing local final backup zip: {final_zip_path}")
            final_zip_path.unlink()

        # 10) Rotate old backups on remote
        print(f"Rotating backups on remote, keeping last {backups_to_keep} archives...")
        rotate_project_backups(remote, remote_path, backups_to_keep)
        print("Backup rotation completed.")
        
        print("\nBackup completed successfully.")
        return final_zip_path

    except Exception as e:
        print(f"\nERROR: {e}")
        print(f"Temp backup directory preserved at: {temp_dir}")
        if final_zip_path:
            print(f"Final zip (if created) at: {final_zip_path}")
        raise


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Backup a Docker Compose project: project zip, volume exports, "
            "bundle into one zip, upload via rclone."
        )
    )
    parser.add_argument(
        "project_dir",
        help="Path to the Docker Compose project directory (where docker-compose.yml lives).",
    )
    parser.add_argument(
        "rclone_remote",
        help="rclone remote name (e.g. 'myremote').",
    )
    parser.add_argument(
        "remote_path",
        help="Path inside the rclone remote (e.g. 'backups/myproject'). Use '' for the root.",
    )
    parser.add_argument(
        "--backups-to-keep",
        type=int,
        default=4,
        help="Number of most recent backups to keep on the remote (default: 4).",
    )

    args = parser.parse_args()

    try:
        backup_project(
            args.project_dir,
            args.rclone_remote,
            args.remote_path,
            backups_to_keep=args.backups_to_keep,
        )
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()

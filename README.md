# Docker Compose Backup Script

Two helpers:
- `backup.py` – snapshot a single Docker Compose project, export its named volumes, bundle everything into a single zip, push to an rclone remote, and rotate old backups.
- `backup_all.py` – run the same workflow for every project directory inside a root folder, using a shared remote/path, and rotate per-project backups.

## Requirements
- Python 3.8+ with `pyyaml` installed (`pip install pyyaml`)
- Docker Engine with the Compose plugin (or legacy `docker-compose`)
- `rclone` configured with a remote (e.g., `rclone config`)
- Shell access to the host running the Compose project

## What the script does
1. Finds the compose file (`docker-compose.yml`, `compose.yml`, etc.) in the project directory.
2. Creates a zip of the whole project directory **before** creating any temp files.
3. Brings the stack down (`docker compose down`).
4. Exports each named volume into a `.tar.gz` inside a temp directory.
5. Brings the stack back up (`docker compose up -d`).
6. Zips all exports into `./<project>-YYYYMMDD-HHMMSS.zip`.
7. Uploads that final zip to the given rclone remote/path.
8. Rotates remote backups, keeping the newest N archives (default 4), deleting older ones.
9. Cleans up local temp data and the final zip. On errors, the temp directory is left in place for inspection.

Named volumes are discovered by scanning service `volumes:` entries and the top-level `volumes:` section. Host-path mounts are ignored.

## Usage
From the machine that hosts your Compose project:

Single project:
```bash
python backup.py /path/to/project myremote backups/myproject --backups-to-keep 4
```

All projects in a directory:
```bash
python backup_all.py myremote backups /path/to/projects_root --backups-to-keep 4
```

- `project_dir`: directory containing your compose file (single project).
- `projects_dir`: folder whose immediate subdirectories are Compose projects (all projects).
- `rclone_remote`: name configured in `rclone config` (e.g., `myremote`).
- `remote_path`: folder path inside that remote (use `''` to target the remote root).
- `--backups-to-keep`: number of latest backup archives to retain on the remote (default 4).

Example targeting the remote root:

```bash
python backup.py /opt/photoprism myremote '' --backups-to-keep 6
```

## Typical Compose file
Any stack with named volumes works. Example with an app and Postgres:

```yaml
services:
  app:
    image: ghcr.io/example/app:latest
    depends_on:
      - db
    environment:
      DATABASE_URL: postgres://app:app@db:5432/app
    volumes:
      - app-data:/var/app/data

  db:
    image: postgres:16
    environment:
      POSTGRES_DB: app
      POSTGRES_USER: app
      POSTGRES_PASSWORD: app
    volumes:
      - db-data:/var/lib/postgresql/data

volumes:
  app-data:
  db-data:
```

The script will export `app-data` and `db-data`, zip the project files, and upload the combined archive.

## Typical use case
- You run a small Compose-managed service and want point-in-time backups of code/config plus named volumes.
- You already use `rclone` to reach cloud/object storage (Backblaze, S3, Google Drive, etc.).
- You prefer a single archive you can download and restore later.

## Restore (brief)
1. Download and unzip the backup archive; inside you’ll see the project zip and volume `tar.gz` files.
2. Recreate volumes and restore contents, e.g.:
   ```bash
   docker volume create app-data
   docker run --rm -v app-data:/volume -v "$(pwd)":/backup alpine sh -c "cd /volume && tar xzf /backup/volume-app-data-*.tar.gz"
   ```
3. Extract the project zip to your desired location and start the stack with `docker compose up -d`.

## Tips
- Run during a maintenance window; containers are stopped briefly while volumes are exported.
- Ensure the target remote has enough space for the combined archive.
- If a failure occurs, the script prints the temp directory path so you can manually inspect or upload the artifacts.

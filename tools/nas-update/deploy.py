#!/usr/bin/env python3
"""Pull tested public releases without giving GitHub access to the NAS."""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import time
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


FILES = ("backend.tar.gz", "frontend.tar.gz", "templates.tar.gz")
SHA = re.compile(r"^[0-9a-f]{40}$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def run(args, capture=True):
    result = subprocess.run([str(arg) for arg in args], check=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, text=True)
    return result.stdout.strip() if capture else ""


def write_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    os.chmod(temp, 0o600)
    temp.replace(path)


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def fetch(url, destination=None):
    request = urllib.request.Request(url, headers={"User-Agent": "plan-cards-nas-updater/1"})
    with urllib.request.urlopen(request, timeout=180) as response:
        if destination:
            with destination.open("wb") as stream:
                shutil.copyfileobj(response, stream)
        else:
            return json.load(response)


def validate_manifest(manifest, repository, tag):
    commit = manifest.get("commit", "")
    if manifest.get("schema") != 1 or not SHA.fullmatch(commit):
        raise ValueError("Invalid release manifest")
    if manifest.get("repository", "").lower() != repository.lower() or tag != "nas-" + commit:
        raise ValueError("Release repository/commit mismatch")
    if set(manifest.get("files", {})) != set(FILES):
        raise ValueError("Unexpected release assets")
    if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in manifest["files"].values()):
        raise ValueError("Invalid asset checksum")
    return commit


def extract_templates(archive, destination):
    with tarfile.open(archive, "r:gz") as source:
        for member in source.getmembers():
            relative = Path(member.name)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise ValueError("Unsafe template path")
            if relative.parts[0] != "card_templates" or not (member.isdir() or member.isfile()):
                raise ValueError("Unexpected template archive member")
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                os.chmod(target, 0o755)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as stream, target.open("wb") as output:
                    shutil.copyfileobj(stream, output)
                os.chmod(target, 0o644)


def database_inventory(data):
    database = data / "cards.db"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Database foreign-key check failed")
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        protected = ("cards", "card_events", "card_benefits", "profiles", "users", "card_bonuses", "card_secrets")
        rows = {name: {row[0] for row in connection.execute('SELECT id FROM "' + name + '"')}
                for name in protected if name in names}
    keys = {path.name: digest(path) for path in data.iterdir()
            if path.is_file() and ("key" in path.name.lower() or path.name == ".admin_token")}
    return rows, keys


def assert_preserved(before, after):
    old_rows, old_keys = before
    new_rows, new_keys = after
    for table, ids in old_rows.items():
        if not ids <= new_rows.get(table, set()):
            raise RuntimeError("Existing records disappeared from " + table)
    if any(new_keys.get(name) != value for name, value in old_keys.items()):
        raise RuntimeError("An existing encryption/signing key changed")


def copy_owned(source, destination):
    result = shutil.copy2(source, destination)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        metadata = os.stat(source)
        os.chown(destination, metadata.st_uid, metadata.st_gid)
    return result


def copy_data(source, destination):
    destination.mkdir(mode=0o700)
    for path in source.iterdir():
        if path.name == "backups":
            continue
        if path.is_symlink():
            raise RuntimeError("Unexpected symlink in application data")
        if path.is_dir():
            shutil.copytree(path, destination / path.name, copy_function=copy_owned)
        else:
            copy_owned(path, destination / path.name)


def compose_spec(base, backend, frontend, templates):
    def env(info):
        return dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item)
    return {
        "version": "3.8",
        "services": {
            "backend": {
                "image": backend["Image"], "restart": "unless-stopped",
                "environment": env(backend), "expose": ["8000"],
                "volumes": [str(base / "data") + ":/data", str(templates) + ":/card_templates:ro"],
            },
            "frontend": {
                "image": frontend["Image"], "restart": "unless-stopped",
                "environment": env(frontend), "ports": ["0.0.0.0:32730:3000"],
                "depends_on": {"backend": {"condition": "service_healthy"}},
            },
        },
    }


class Deployer:
    def __init__(self, config):
        self.repository = config["repository"]
        if not REPO.fullmatch(self.repository):
            raise ValueError("Invalid GitHub repository")
        self.base = Path(config["base"]).resolve()
        if not self.base.is_absolute() or self.base.name != "plan-cards":
            raise ValueError("Unexpected deployment directory")
        self.root = self.base / "auto-update"
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.docker = config.get("docker", "/usr/local/bin/docker")
        self.compose = config.get("compose", "/usr/local/bin/docker-compose")
        self.state_path = self.root / "state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    def inspect(self, name):
        return json.loads(run([self.docker, "inspect", name]))[0]

    def compose_run(self, path, *args):
        return run([self.compose, "-p", "plan-cards", "-f", path, *args])

    def healthy(self, name):
        for _ in range(90):
            state = self.inspect(name)["State"]
            if state.get("Health", {}).get("Status") == "healthy":
                return
            if state.get("Status") in ("exited", "dead"):
                break
            time.sleep(2)
        raise RuntimeError("Container did not become healthy: " + name)

    def save_state(self, **values):
        self.state.update(values)
        self.state["checked_at"] = datetime.now(timezone.utc).isoformat()
        write_json(self.state_path, self.state)

    def execute(self, check_only=False, retry=False):
        release = fetch("https://api.github.com/repos/" + self.repository + "/releases/latest")
        if release.get("draft") or release.get("prerelease"):
            raise ValueError("Release is not deployable")
        tag = release["tag_name"]
        assets = {asset["name"]: asset["browser_download_url"] for asset in release["assets"]}
        prefix = "https://github.com/" + self.repository + "/releases/download/" + tag + "/"
        for name in FILES + ("manifest.json",):
            if assets.get(name) != prefix + name:
                raise ValueError("Missing or unexpected asset URL: " + name)
        manifest = fetch(assets["manifest.json"])
        commit = validate_manifest(manifest, self.repository, tag)
        if check_only:
            print(json.dumps({"available": commit, "current": self.state.get("current"), "status": self.state.get("status")}))
            return
        if self.state.get("current") == commit:
            self.save_state(status="up_to_date")
            return
        if self.state.get("status") in ("deploying", "rollback_failed"):
            raise RuntimeError("Interrupted deployment or failed rollback; inspect the saved backup before continuing")
        if self.state.get("failed") == commit and not retry:
            print("This release previously failed; explicit --retry is required.")
            return

        staging = self.root / "releases" / commit
        staging.mkdir(parents=True, exist_ok=True)
        for name in FILES:
            path = staging / name
            if not path.exists() or digest(path) != manifest["files"][name]:
                fetch(assets[name], path)
            if digest(path) != manifest["files"][name]:
                raise RuntimeError("Downloaded asset checksum mismatch: " + name)
        extract_templates(staging / "templates.tar.gz", staging)
        # Containers run as non-root and must be able to traverse the template path.
        for path in (self.root, self.root / "releases", staging):
            os.chmod(path, 0o711)
        for component in ("backend", "frontend"):
            run([self.docker, "load", "-i", staging / (component + ".tar.gz")])
            image = self.inspect("plan-cards-" + component + ":" + commit)
            if image["Config"].get("Labels", {}).get("org.opencontainers.image.revision") != commit:
                raise RuntimeError("Docker image revision mismatch")

        backend = self.inspect("plan-cards-backend-1")
        frontend = self.inspect("plan-cards-frontend-1")
        mounts = {mount["Destination"]: mount for mount in backend["Mounts"]}
        if Path(mounts["/data"]["Source"]).resolve() != self.base / "data":
            raise RuntimeError("Unexpected production data mount")
        ports = frontend["HostConfig"]["PortBindings"]
        if ports != {"3000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "32730"}]}:
            raise RuntimeError("Production ports changed; manual review required")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = self.root / "backups" / (stamp + "-" + commit[:12])
        backup.mkdir(parents=True, mode=0o700)
        old_spec = compose_spec(self.base, backend, frontend, Path(mounts["/card_templates"]["Source"]))
        old_file = backup / "previous-compose.json"
        write_json(old_file, old_spec)
        for component, info in (("backend", backend), ("frontend", frontend)):
            run([self.docker, "tag", info["Image"], "plan-cards-" + component + ":rollback-" + stamp])
        new_spec = compose_spec(self.base, backend, frontend, staging / "card_templates")
        for component in ("backend", "frontend"):
            new_spec["services"][component]["image"] = "plan-cards-" + component + ":" + commit
        runtime = self.root / "runtime.json"
        write_json(runtime, new_spec)
        self.save_state(status="deploying", candidate=commit, backup=str(backup))
        stopped = False
        backup_complete = False
        try:
            stopped = True
            run([self.docker, "stop", "plan-cards-frontend-1", "plan-cards-backend-1"])
            before = database_inventory(self.base / "data")
            copy_data(self.base / "data", backup / "data")
            assert_preserved(before, database_inventory(backup / "data"))
            backup_complete = True
            env_file = self.base / ".env"
            if env_file.exists():
                shutil.copy2(env_file, backup / ".env")
                os.chmod(backup / ".env", 0o600)
            self.compose_run(runtime, "up", "-d", "--no-build", "backend")
            self.healthy("plan-cards-backend-1")
            assert_preserved(before, database_inventory(self.base / "data"))
            self.compose_run(runtime, "up", "-d", "--no-build", "frontend")
            self.healthy("plan-cards-frontend-1")
            with urllib.request.urlopen("http://127.0.0.1:32730/", timeout=15) as response:
                if response.status != 200:
                    raise RuntimeError("Frontend HTTP probe failed")
            assert_preserved(before, database_inventory(self.base / "data"))
            self.save_state(status="deployed", current=commit, failed=None)
            print("Deployed " + commit + "; backup: " + str(backup))
        except Exception:
            self.save_state(status="failed", failed=commit)
            if stopped:
                try:
                    self.compose_run(runtime, "stop")
                    if backup_complete:
                        # Preserve the failed database too; never discard writes made
                        # during the brief cutover or a failed migration.
                        failed_data = backup / "failed-data"
                        copy_data(self.base / "data", failed_data)
                        for name in ("cards.db-wal", "cards.db-shm", "cards.db-journal"):
                            sidecar = self.base / "data" / name
                            if sidecar.exists() and not (backup / "data" / name).exists():
                                sidecar.unlink()
                        for path in (backup / "data").iterdir():
                            if path.is_file():
                                copy_owned(path, self.base / "data" / path.name)
                            elif path.is_dir():
                                shutil.copytree(path, self.base / "data" / path.name, dirs_exist_ok=True,
                                                copy_function=copy_owned)
                    self.compose_run(old_file, "up", "-d", "--no-build")
                    self.healthy("plan-cards-backend-1")
                    self.healthy("plan-cards-frontend-1")
                    self.save_state(status="rolled_back")
                except Exception:
                    self.save_state(status="rollback_failed")
            raise


def main():
    import fcntl
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--retry", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(Path(args.config).read_text())
    if not config.get("enabled", False) and not args.check:
        print("Automatic deployment is disabled.")
        return
    deployer = Deployer(config)
    with (deployer.root / "update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another update is running.")
            return
        try:
            deployer.execute(args.check, args.retry)
        except Exception as error:
            deployer.save_state(last_error=str(error))
            raise


if __name__ == "__main__":
    main()

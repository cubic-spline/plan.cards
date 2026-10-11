import io
import json
import sqlite3
import tarfile
import tempfile
import unittest
from contextlib import closing
from pathlib import Path, PurePosixPath
from unittest.mock import patch, MagicMock

import deploy


class ReleaseTests(unittest.TestCase):
    def manifest(self):
        return {"schema": 1, "repository": "example/plan.cards", "commit": "a" * 40,
                "files": {name: "b" * 64 for name in deploy.FILES}}

    def test_manifest_accepts_exact_release(self):
        self.assertEqual(deploy.validate_manifest(self.manifest(), "example/plan.cards", "nas-" + "a" * 40), "a" * 40)

    def test_manifest_rejects_wrong_repository_tag_or_assets(self):
        for field, value in (("repository", "attacker/project"), ("commit", "../escape"),
                             ("files", {"payload.sh": "b" * 64}), ("schema", 2)):
            item = self.manifest()
            item[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                deploy.validate_manifest(item, "example/plan.cards", "nas-" + "a" * 40)

    def test_archive_only_allows_regular_template_files(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("/tmp/escape", tarfile.REGTYPE),
                           ("card_templates/link", tarfile.SYMTYPE), ("tools/run.sh", tarfile.REGTYPE)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                archive = Path(temp) / "input.tar.gz"
                with tarfile.open(archive, "w:gz") as stream:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
                    stream.addfile(member, io.BytesIO())
                with self.assertRaises(ValueError):
                    deploy.extract_templates(archive, Path(temp) / "output")

    def test_archive_extracts_valid_template(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "input.tar.gz"
            with tarfile.open(archive, "w:gz") as stream:
                member = tarfile.TarInfo("card_templates/test/card.yaml")
                member.size = 7
                stream.addfile(member, io.BytesIO(b"fee: 0\n"))
            deploy.extract_templates(archive, Path(temp) / "out")
            self.assertEqual((Path(temp) / "out/card_templates/test/card.yaml").read_text(), "fee: 0\n")


class BackupTests(unittest.TestCase):
    def make_data(self, root):
        root.mkdir()
        with closing(sqlite3.connect(root / "cards.db")) as connection, connection:
            connection.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY, name TEXT)")
            connection.execute("CREATE TABLE card_events (id INTEGER PRIMARY KEY, card_id INTEGER REFERENCES cards(id))")
            connection.execute("INSERT INTO cards VALUES (1, 'test')")
            connection.execute("INSERT INTO card_events VALUES (10, 1)")
        (root / ".encryption_key").write_bytes(b"test-key-not-a-secret")
        (root / "backups").mkdir()
        (root / "backups/old.db").write_bytes(b"older backup")

    def test_backup_preserves_database_and_key_without_recursion(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            self.make_data(source)
            destination = Path(temp) / "backup"
            deploy.copy_data(source, destination)
            deploy.assert_preserved(deploy.database_inventory(source), deploy.database_inventory(destination))
            self.assertFalse((destination / "backups").exists())
            self.assertEqual((destination / ".encryption_key").read_bytes(), b"test-key-not-a-secret")

    def test_guard_rejects_lost_history_or_changed_key(self):
        before = ({"cards": {1}, "card_events": {10}}, {".encryption_key": "abc"})
        deploy.assert_preserved(before, ({"cards": {1, 2}, "card_events": {10, 11}}, {".encryption_key": "abc"}))
        for after in (({"cards": {1}, "card_events": set()}, {".encryption_key": "abc"}),
                      ({"cards": {1}, "card_events": {10}}, {".encryption_key": "different"})):
            with self.assertRaises(RuntimeError):
                deploy.assert_preserved(before, after)

    def test_compose_keeps_local_data_and_does_not_expose_backend(self):
        info = {"Image": "sha256:test", "Config": {"Env": ["SECRET_KEY=test-only"]}}
        spec = deploy.compose_spec(PurePosixPath("/volume1/docker/plan-cards"), info, info, PurePosixPath("/templates"))
        self.assertNotIn("ports", spec["services"]["backend"])
        self.assertEqual(spec["services"]["frontend"]["ports"], ["0.0.0.0:32730:3000"])
        self.assertIn("/volume1/docker/plan-cards/data:/data", spec["services"]["backend"]["volumes"])

    def test_inventory_supports_card_id_and_composite_primary_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            self.make_data(source)
            with closing(sqlite3.connect(source / "cards.db")) as connection, connection:
                connection.execute("CREATE TABLE card_secrets (card_id INTEGER PRIMARY KEY REFERENCES cards(id))")
                connection.execute("INSERT INTO card_secrets VALUES (1)")
                connection.execute("CREATE TABLE user_settings (user_id INTEGER, name TEXT, PRIMARY KEY(user_id, name))")
                connection.execute("INSERT INTO user_settings VALUES (2, 'language')")
            rows, _ = deploy.database_inventory(source)
            self.assertEqual(rows["card_secrets"], {(1,)})
            self.assertEqual(rows["user_settings"], {(2, "language")})

    def test_inventory_rejects_table_without_primary_key(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "plan-cards"
            base.mkdir()
            self.make_data(base / "data")
            with closing(sqlite3.connect(base / "data/cards.db")) as connection, connection:
                connection.execute("CREATE TABLE card_secrets (card_id INTEGER)")
            with self.assertRaisesRegex(RuntimeError, "no primary key"):
                deploy.database_inventory(base / "data")

class DeploymentFailureTests(BackupTests):
    def setup_release(self, temp):
        base = Path(temp) / "plan-cards"
        base.mkdir()
        self.make_data(base / "data")
        updater = deploy.Deployer({"repository": "example/plan.cards", "base": str(base)})
        commit = "a" * 40
        stage = updater.root / "releases" / commit
        stage.mkdir(parents=True)
        (stage / "backend.tar.gz").write_bytes(b"mock backend image")
        (stage / "frontend.tar.gz").write_bytes(b"mock frontend image")
        with tarfile.open(stage / "templates.tar.gz", "w:gz") as stream:
            item = tarfile.TarInfo("card_templates/test/card.yaml")
            item.size = 7
            stream.addfile(item, io.BytesIO(b"fee: 0\n"))
        manifest = {"schema": 1, "repository": updater.repository, "commit": commit,
                    "files": {name: deploy.digest(stage / name) for name in deploy.FILES}}
        prefix = "https://github.com/example/plan.cards/releases/download/nas-" + commit + "/"
        release = {"tag_name": "nas-" + commit,
                   "assets": [{"name": name, "browser_download_url": prefix + name}
                              for name in deploy.FILES + ("manifest.json",)]}

        def inspect(name):
            if name.endswith(commit):
                return {"Config": {"Labels": {"org.opencontainers.image.revision": commit}}}
            result = {"Image": "sha256:old", "Config": {"Env": ["SECRET_KEY=test-only"]}}
            if name == "plan-cards-backend-1":
                result["Mounts"] = [{"Destination": "/data", "Source": str(base / "data")},
                                    {"Destination": "/card_templates", "Source": str(base / "card_templates")}]
            else:
                result["HostConfig"] = {"PortBindings": {"3000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "32730"}]}}
            return result

        updater.inspect = inspect
        updater.compose_run = MagicMock()
        updater.healthy = MagicMock()
        return updater, release, manifest

    def test_schema_preflight_failure_never_stops_containers(self):
        with tempfile.TemporaryDirectory() as temp:
            updater, release, manifest = self.setup_release(temp)
            with patch.object(deploy, "fetch", side_effect=[release, manifest]), \
                    patch.object(deploy, "database_inventory", side_effect=RuntimeError("unsupported schema")), \
                    patch.object(deploy, "run") as commands:
                with self.assertRaisesRegex(RuntimeError, "unsupported schema"):
                    updater.execute()
            commands.assert_not_called()
            updater.compose_run.assert_not_called()

    def test_incomplete_backup_never_restores_partial_data(self):
        with tempfile.TemporaryDirectory() as temp:
            updater, release, manifest = self.setup_release(temp)
            before = deploy.digest(updater.base / "data/cards.db")

            def partial_copy(source, destination):
                destination.mkdir()
                (destination / "cards.db").write_bytes(b"incomplete")
                raise OSError("simulated disk full")

            with patch.object(deploy, "fetch", side_effect=[release, manifest]), \
                    patch.object(deploy, "run"), patch.object(deploy, "copy_data", side_effect=partial_copy):
                with self.assertRaises(OSError):
                    updater.execute()
            self.assertEqual(deploy.digest(updater.base / "data/cards.db"), before)
            self.assertEqual(updater.state["status"], "rolled_back")
            self.assertTrue(updater.compose_run.called)

    def test_failed_health_check_restores_old_state_and_preserves_failed_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            updater, release, manifest = self.setup_release(temp)
            before = deploy.database_inventory(updater.base / "data")
            updater.healthy.side_effect = [RuntimeError("simulated unhealthy candidate"), None, None]
            with patch.object(deploy, "fetch", side_effect=[release, manifest]), patch.object(deploy, "run"):
                with self.assertRaises(RuntimeError):
                    updater.execute()
            self.assertEqual(updater.state["status"], "rolled_back")
            deploy.assert_preserved(before, deploy.database_inventory(updater.base / "data"))
            self.assertTrue((Path(updater.state["backup"]) / "failed-data/cards.db").exists())
            self.assertIsNone(updater.state.get("current"))

    def test_failed_release_does_not_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            updater, release, manifest = self.setup_release(temp)
            updater.state["failed"] = manifest["commit"]
            with patch.object(deploy, "fetch", side_effect=[release, manifest]), patch.object(deploy, "run") as commands:
                updater.execute()
            commands.assert_not_called()


if __name__ == "__main__":
    unittest.main()

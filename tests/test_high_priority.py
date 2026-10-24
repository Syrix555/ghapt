import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import ghapt


TESTS = Path(__file__).resolve().parent
ASSETS = TESTS / "assets"


def project(name, package, project_type, assets):
    return SimpleNamespace(
        name=name,
        package=package,
        type=project_type,
        assets=assets,
        strip_prefix="v",
        binaries=[package],
        description=f"{package} test package",
        depends="",
        section="utils",
        repo=f"testorg/{name}",
    )


class HighPriorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ghapt-tests-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.st = SimpleNamespace(
            repo_dir=self.repo,
            state_file=self.root / "state.json",
            gpg_home=self.root / "gnupg",
            component="main",
            token="",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_idempotent_sync_project_returns_an_empty_touched_list(self):
        existing = self.repo / "pool" / "main" / "t" / "tooldeb" / "tooldeb_1.0.0_amd64.deb"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"already present")
        p = project("tooldeb", "tooldeb", "deb", {"amd64": r".*"})
        state = {
            "tooldeb": {
                "tag": "v1.0.0",
                "files": {"amd64": existing.relative_to(self.repo).as_posix()},
            }
        }
        release = {"tag_name": "v1.0.0", "assets": []}
        with mock.patch.object(ghapt, "latest_release", return_value=release):
            touched = ghapt.sync_project(p, self.st, state, force=False)
        self.assertEqual(touched, [])

    def test_deb_mode_uses_the_package_version_and_validates_metadata(self):
        fixture = ASSETS / "tooldeb_1.0.0_amd64.deb"
        p = project("tooldeb", "tooldeb", "deb", {"amd64": r"^tooldeb"})
        asset = {
            "name": fixture.name,
            "browser_download_url": "fixture://tooldeb",
            "size": fixture.stat().st_size,
        }

        def copy_fixture(_url, dest, _size=None, _digest=None):
            shutil.copy2(fixture, dest)

        with mock.patch.object(
            ghapt, "latest_release",
            return_value={"tag_name": "release-1.0.0", "assets": [asset]},
        ), mock.patch.object(ghapt, "download", side_effect=copy_fixture):
            state = {}
            touched = ghapt.sync_project(p, self.st, state, force=False)

        self.assertEqual(len(touched), 1)
        self.assertEqual(state["tooldeb"]["tag"], "release-1.0.0")
        self.assertTrue(
            (self.repo / "pool/main/t/tooldeb/tooldeb_1.0.0_amd64.deb").exists()
        )

    def test_deb_mode_rejects_package_or_architecture_mismatch(self):
        fixture = ASSETS / "multia_1.0.0_arm64.deb"
        p = project("tooldeb", "tooldeb", "deb", {"amd64": r"^multia"})
        asset = {
            "name": fixture.name,
            "browser_download_url": "fixture://multia",
            "size": fixture.stat().st_size,
        }

        def copy_fixture(_url, dest, _size=None, _digest=None):
            shutil.copy2(fixture, dest)

        with mock.patch.object(
            ghapt, "latest_release",
            return_value={"tag_name": "release-1.0.0", "assets": [asset]},
        ), mock.patch.object(ghapt, "download", side_effect=copy_fixture):
            with self.assertRaisesRegex(RuntimeError, "Package"):
                ghapt.sync_project(p, self.st, {}, force=False)

        self.assertEqual(list(self.repo.rglob("*.deb")), [])

    def test_multiarch_pool_commit_rolls_back_when_a_later_architecture_fails(self):
        amd64 = ASSETS / "multia_1.0.0_amd64.deb"
        arm64 = ASSETS / "multia_1.0.0_arm64.deb"
        p = project(
            "multia",
            "multia",
            "deb",
            {"amd64": r"^multia_.*_amd64", "arm64": r"^multia_.*_arm64"},
        )
        assets = [
            {
                "name": amd64.name,
                "browser_download_url": "fixture://amd64",
                "size": amd64.stat().st_size,
            },
            {
                "name": arm64.name,
                "browser_download_url": "fixture://arm64",
                "size": arm64.stat().st_size,
            },
        ]
        fixtures = {"fixture://amd64": amd64, "fixture://arm64": arm64}

        def copy_fixture(url, dest, _size=None, _digest=None):
            shutil.copy2(fixtures[url], dest)

        original_replace = ghapt.os.replace

        def fail_arm64(src, dst):
            if str(dst).endswith("multia_1.0.0_arm64.deb"):
                raise OSError("simulated second architecture failure")
            return original_replace(src, dst)

        with mock.patch.object(
            ghapt,
            "latest_release",
            return_value={"tag_name": "release-1.0.0", "assets": assets},
        ), mock.patch.object(ghapt, "download", side_effect=copy_fixture), mock.patch.object(
            ghapt.os, "replace", side_effect=fail_arm64
        ):
            with self.assertRaisesRegex(OSError, "second architecture"):
                ghapt.sync_project(p, self.st, {}, force=False)

        self.assertEqual(list(self.repo.rglob("*.deb")), [])
        self.assertEqual(list(self.repo.glob(".ghapt-*")), [])

    def test_multiarch_all_packages_cannot_share_one_pool_target(self):
        amd64 = ASSETS / "multia_1.0.0_amd64.deb"
        metadata = {
            "Package": "multia",
            "Version": "1.0.0",
            "Architecture": "all",
        }
        with self.assertRaisesRegex(RuntimeError, "同一个 pool 文件"):
            ghapt._stage_and_commit_debs(
                self.st,
                [
                    ("amd64", amd64, metadata),
                    ("arm64", amd64, metadata),
                ],
            )

        self.assertEqual(list(self.repo.rglob("*.deb")), [])
        self.assertEqual(list(self.repo.glob(".ghapt-*")), [])

    def test_index_matches_rejects_invalid_signatures(self):
        dist = self.repo / "dists" / "stable"
        dist.mkdir(parents=True)
        index_files = {"main/binary-amd64/Packages": b"Package: tooldeb\n"}
        index_path = dist / "main/binary-amd64/Packages"
        index_path.parent.mkdir(parents=True)
        index_path.write_bytes(index_files["main/binary-amd64/Packages"])
        entries = []
        for name, func in (
            ("MD5Sum", hashlib.md5),
            ("SHA1", hashlib.sha1),
            ("SHA256", hashlib.sha256),
        ):
            data = index_files["main/binary-amd64/Packages"]
            entries.append(f"{name}:\n {func(data).hexdigest()} {len(data)} main/binary-amd64/Packages")
        (dist / "Release").write_text("\n".join(entries) + "\n")
        (dist / "Release.gpg").write_bytes(b"not a signature")
        (dist / "InRelease").write_bytes(b"not a clearsigned file")
        (self.repo / "ghapt.gpg").write_bytes(b"not a keyring")

        ok, reason = ghapt.index_matches(
            dist, index_files, st=self.st, fpr="expected-fingerprint"
        )

        self.assertFalse(ok)
        self.assertIn("签名", reason)

    def test_publish_failure_restores_old_release_and_cleans_staging(self):
        dist = self.repo / "dists" / "stable"
        old = dist / "main" / "binary-amd64" / "Packages"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old packages")
        old_asc = self.repo / "ghapt.asc"
        old_gpg = self.repo / "ghapt.gpg"
        old_asc.write_bytes(b"old armored key")
        old_gpg.write_bytes(b"old binary key")

        def fake_gpg(cmd, binary=False, **_kwargs):
            if "--clearsign" in cmd:
                Path(cmd[cmd.index("-o") + 1]).write_bytes(b"new inrelease")
            elif "-abs" in cmd:
                Path(cmd[cmd.index("-o") + 1]).write_bytes(b"new detached signature")
            elif "--export" in cmd:
                return SimpleNamespace(stdout=b"public key")
            return SimpleNamespace(stdout=b"")

        original_replace = ghapt.os.replace
        calls = 0

        def fail_key_commit(src, dst):
            nonlocal calls
            calls += 1
            # dist -> backup, staged dist -> dist, then both old key backups
            # and the armored-key install have already succeeded.
            if calls == 6:
                raise OSError("simulated publish failure")
            return original_replace(src, dst)

        with mock.patch.object(ghapt, "run_checked", side_effect=fake_gpg), mock.patch.object(
            ghapt.os, "replace", side_effect=fail_key_commit
        ):
            with self.assertRaisesRegex(RuntimeError, "publish"):
                ghapt.publish(
                    self.st,
                    "expected-fingerprint",
                    dist,
                    {"main/binary-amd64/Packages": b"new packages"},
                    b"new release\n",
                )

        self.assertEqual(old.read_bytes(), b"old packages")
        self.assertEqual(old_asc.read_bytes(), b"old armored key")
        self.assertEqual(old_gpg.read_bytes(), b"old binary key")
        self.assertEqual(list(self.repo.rglob("*.new")), [])
        self.assertEqual(list(self.repo.glob(".ghapt-*")), [])


if __name__ == "__main__":
    unittest.main()

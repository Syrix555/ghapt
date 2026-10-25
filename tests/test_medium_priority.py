import json
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import ghapt


class MediumPriorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ghapt-medium-tests-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.st = SimpleNamespace(
            repo_dir=self.repo,
            state_file=self.root / "state.json",
            component="main",
            suite="stable",
            architectures=["amd64"],
            origin="ghapt",
            label="ghapt",
            description="test repository",
            token="",
            github_mirror="",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_schema_must_be_an_object_with_safe_project_records(self):
        self.st.state_file.write_text("[]")
        with self.assertRaises(ghapt.StateError):
            ghapt.load_state(self.st, strict=True)
        self.assertEqual(ghapt.load_state(self.st), {})

        self.st.state_file.write_text(json.dumps({
            "tool": {"tag": "v1", "files": {"amd64": "../outside.deb"}}
        }))
        with self.assertRaises(ghapt.StateError):
            ghapt.load_state(self.st, strict=True)

    def test_state_file_and_parent_symlinks_are_rejected(self):
        target = self.root / "real-state.json"
        target.write_text("{}")
        self.st.state_file.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "符号链接"):
            ghapt.load_state(self.st, strict=True)

        self.st.state_file.unlink()
        real_parent = self.root / "real-state-dir"
        real_parent.mkdir()
        linked_parent = self.root / "linked-state-dir"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        self.st.state_file = linked_parent / "state.json"
        with self.assertRaisesRegex(RuntimeError, "符号链接"):
            ghapt.save_state(self.st, {})

    def test_save_state_rejects_empty_or_multiline_records(self):
        valid = {"tool": {"tag": "v1", "files": {"amd64": "pool/main/t/tool/tool_1.0_amd64.deb"}}}
        for record in (
            {"tag": "", "files": valid["tool"]["files"]},
            {"tag": "v1\nnext", "files": valid["tool"]["files"]},
            {"tag": "v1", "files": {}},
        ):
            with self.subTest(record=record), self.assertRaises(ghapt.StateError):
                ghapt.save_state(self.st, {"tool": record})

        with self.assertRaises(ghapt.StateError):
            ghapt.save_state(
                self.st,
                {"tool\nnext": valid["tool"]},
            )

    def test_state_lock_rejects_symlink_and_releases_descriptor(self):
        target = self.root / "real.lock"
        target.write_text("")
        lock_path = self.st.state_file.with_suffix(".lock")
        lock_path.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "符号链接"):
            with ghapt.state_lock(self.st, "test"):
                pass

        lock_path.unlink()
        with ghapt.state_lock(self.st, "outer"):
            with self.assertRaisesRegex(RuntimeError, "正在运行"):
                with ghapt.state_lock(self.st, "inner"):
                    pass
        with ghapt.state_lock(self.st, "after"):
            pass

    def test_hash_cache_discards_malformed_entries_and_non_object_roots(self):
        cache = self.root / ghapt.HASH_CACHE_NAME
        cache.write_text("[]")
        self.assertEqual(ghapt.load_hash_cache(self.st), {})

        cache.write_text(json.dumps({
            "pool/main/t/tool/tool_1.0_amd64.deb": [
                10, 20, 30, 40, 50, "a" * 32, "b" * 40, "c" * 64
            ],
            "bad-size": ["10", 20, 30, 40, 50, "a" * 32, "b" * 40, "c" * 64],
            "bad-digest": [10, 20, 30, 40, 50, "x" * 32, "b" * 40, "c" * 64],
        }))
        self.assertEqual(
            ghapt.load_hash_cache(self.st),
            {"pool/main/t/tool/tool_1.0_amd64.deb": [
                10, 20, 30, 40, 50, "a" * 32, "b" * 40, "c" * 64
            ]},
        )

    def test_config_syntax_errors_are_reported_as_configuration_errors(self):
        config = self.root / "broken.ini"
        config.write_text("[settings]\nrepo_dir = /tmp/repo\nbroken line\n")
        with self.assertRaisesRegex(SystemExit, "配置文件无法解析"):
            ghapt.load_config(config)

        config.write_text(
            "[settings]\n"
            "suite = ../stable\n"
            "component = main\n"
            "architectures = amd64\n"
        )
        with self.assertRaisesRegex(SystemExit, "suite"):
            ghapt.load_config(config)

    def test_prerelease_lookup_follows_github_link_pagination(self):
        first = ([{"draft": True, "tag_name": "draft", "assets": []}], {
            "Link": '<https://api.github.com/repos/o/r/releases?page=2>; rel="next"'
        })
        second = ([{"draft": False, "tag_name": "v2", "assets": []}], {})
        calls = []

        def fake_json(path, token):
            calls.append(path)
            return first if len(calls) == 1 else second

        project = SimpleNamespace(repo="o/r", prerelease=True)
        with mock.patch.object(ghapt, "gh_json_response", side_effect=fake_json):
            result = ghapt.latest_release(project, "")

        self.assertEqual(result["tag_name"], "v2")
        self.assertEqual(calls, [
            "/repos/o/r/releases?per_page=10",
            "https://api.github.com/repos/o/r/releases?page=2",
        ])

    def test_github_mirror_rewrites_only_supported_https_hosts(self):
        mirror = "https://gh.syrix.space/"
        supported = (
            "github.com",
            "raw.githubusercontent.com",
            "gist.githubusercontent.com",
            "gist.github.com",
            "api.github.com",
            "codeload.github.com",
            "github.githubassets.com",
        )
        for host in supported:
            with self.subTest(host=host):
                original = f"https://{host}/a/path?x=1"
                self.assertEqual(
                    ghapt.mirror_github_url(original, mirror),
                    mirror.rstrip("/") + "/" + original,
                )

        self.assertEqual(
            ghapt.mirror_github_url("http://github.com/a", mirror),
            "http://github.com/a",
        )
        self.assertEqual(
            ghapt.mirror_github_url("https://example.com/a", mirror),
            "https://example.com/a",
        )
        self.assertEqual(
            ghapt.mirror_github_url("file:///tmp/release.json", mirror),
            "file:///tmp/release.json",
        )
        self.assertEqual(
            ghapt.mirror_github_url("https://github.com/a", ""),
            "https://github.com/a",
        )

    def test_github_mirror_config_is_validated_and_normalized(self):
        config = self.root / "mirror.ini"
        config.write_text(
            "[settings]\n"
            "architectures = amd64\n"
            "github_mirror = https://gh.syrix.space/\n"
        )
        with mock.patch.dict("os.environ", {"GITHUB_MIRROR": ""}, clear=False):
            st, _projects, _broken = ghapt.load_config(config)
        self.assertEqual(st.github_mirror, "https://gh.syrix.space")

        for value in (
            "http://gh.syrix.space",
            "gh.syrix.space",
            "https://user@gh.syrix.space",
            "https://gh.syrix.space/?token=secret",
            "https://gh.syrix.space/#fragment",
        ):
            with self.subTest(value=value):
                config.write_text(
                    "[settings]\n"
                    "architectures = amd64\n"
                    f"github_mirror = {value}\n"
                )
                with self.assertRaisesRegex(SystemExit, "github_mirror"):
                    ghapt.load_config(config)

    def test_github_mirror_is_applied_to_api_requests(self):
        class Response(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                self.close()

        requests = []

        def fake_urlopen(request, timeout):
            requests.append((request, timeout))
            return Response(b'{"tag_name": "v1", "assets": []}')

        with mock.patch.object(
            ghapt.urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            result = ghapt.gh_json_response(
                "/repos/o/r/releases/latest", "", "https://gh.syrix.space"
            )

        self.assertEqual(result[0]["tag_name"], "v1")
        self.assertEqual(
            requests[0][0].full_url,
            "https://gh.syrix.space/https://api.github.com/repos/o/r/releases/latest",
        )
        self.assertEqual(requests[0][1], 60)

    def test_github_mirror_is_applied_to_release_asset_download(self):
        fixture = Path(__file__).parent / "assets" / "tooldeb_1.0.0_amd64.deb"
        project = SimpleNamespace(
            name="tool",
            repo="o/r",
            type="deb",
            package="tooldeb",
            assets={"amd64": r"^tooldeb"},
            strip_prefix="v",
        )
        asset = {
            "name": fixture.name,
            "browser_download_url": "https://github.com/o/r/releases/download/v1/tool.deb",
            "size": fixture.stat().st_size,
        }
        calls = []

        def copy_fixture(url, dest, _size=None, _digest=None):
            calls.append(url)
            shutil.copy2(fixture, dest)

        self.st.github_mirror = "https://gh.syrix.space"
        with mock.patch.object(
            ghapt, "latest_release",
            return_value={"tag_name": "release-1", "assets": [asset]},
        ), mock.patch.object(ghapt, "download", side_effect=copy_fixture):
            ghapt.sync_project(project, self.st, {}, force=False)

        self.assertEqual(
            calls,
            ["https://gh.syrix.space/https://github.com/o/r/releases/download/v1/tool.deb"],
        )

    def test_generate_indexes_only_reads_the_configured_component(self):
        old = self.repo / "pool" / "oldcomp" / "t" / "tool"
        current = self.repo / "pool" / "main" / "t" / "tooldeb"
        old.mkdir(parents=True)
        current.mkdir(parents=True)
        old_deb = Path(__file__).parent / "assets" / "tooldeb_1.0.0_amd64.deb"
        (old / old_deb.name).write_bytes(old_deb.read_bytes())
        (current / old_deb.name).write_bytes(old_deb.read_bytes())

        _dist, index_files, _release = ghapt.generate_indexes(self.st)
        packages = index_files["main/binary-amd64/Packages"].decode()
        self.assertIn("Filename: pool/main/t/tooldeb/", packages)
        self.assertNotIn("Filename: pool/oldcomp/", packages)

    def test_pool_scan_rejects_noncanonical_layout(self):
        cases = (
            ("pool/main/tool_1.0_amd64.deb", "错误层级"),
            ("pool/main/A/tool/tool_1.0_amd64.deb", "首字母目录格式错误"),
            ("pool/main/u/tool/tool_1.0_amd64.deb", "首字母目录"),
            ("pool/main/t/tool/nested/tool_1.0_amd64.deb", "嵌套目录"),
            ("pool/main/t/tool/tool_1.0_amd64.deb.new", "临时文件"),
            ("pool/main/t/tool/tool_1.0_amd64.deb.part", "临时文件"),
        )
        for rel, message in cases:
            with self.subTest(rel=rel):
                path = self.repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"placeholder")
                with self.assertRaisesRegex(RuntimeError, message):
                    ghapt._pool_deb_files(self.st, self.repo / "pool" / "main")
                shutil.rmtree(self.repo / "pool", ignore_errors=True)

    def test_pool_scan_accepts_numeric_package_prefix(self):
        package_dir = self.repo / "pool" / "main" / "7" / "7zip"
        package_dir.mkdir(parents=True)
        deb = package_dir / "7zip_1.0_amd64.deb"
        deb.write_bytes(b"placeholder")

        self.assertEqual(ghapt._pool_deb_files(
            self.st, self.repo / "pool" / "main"), [deb])

    def test_pool_scan_rejects_symlinks_without_following_them(self):
        outside = self.root / "outside"
        outside.mkdir()
        target = outside / "tool_1.0_amd64.deb"
        target.write_bytes(b"outside")
        link = self.repo / "pool" / "main" / "t" / "tool"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "符号链接"):
            ghapt._pool_deb_files(self.st, self.repo / "pool" / "main")
        self.assertTrue(target.exists())

    def test_cleanup_old_suite_unlinks_external_symlink_only(self):
        dists = self.repo / "dists"
        dists.mkdir()
        outside = self.root / "old-suite-target"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        (dists / "old").symlink_to(outside, target_is_directory=True)

        removed = ghapt.cleanup_old_suites(self.st)

        self.assertEqual([path.name for path in removed], ["old"])
        self.assertFalse((dists / "old").exists())
        self.assertTrue((outside / "keep").exists())

    def test_corrupt_deb_fails_sync_with_a_controlled_error(self):
        config = self.root / "config.ini"
        config.write_text(
            "[settings]\n"
            f"repo_dir = {self.repo}\n"
            f"state_file = {self.st.state_file}\n"
            "architectures = amd64\n"
            "\n"
            "[project:tool]\n"
            "repo = o/r\n"
            "asset.amd64 = ^tool\n"
        )
        corrupt = self.repo / "pool" / "main" / "t" / "tool" / "tool_1.0_amd64.deb"
        corrupt.parent.mkdir(parents=True)
        corrupt.write_bytes(b"not a deb")
        self.st.state_file.write_text(json.dumps({
            "tool": {
                "tag": "v1",
                "files": {"amd64": corrupt.relative_to(self.repo).as_posix()},
            }
        }))

        args = SimpleNamespace(config=str(config), project=None, force=False)
        with mock.patch.object(
            ghapt, "latest_release",
            return_value={"tag_name": "v1", "assets": []},
        ):
            with self.assertRaises(SystemExit) as raised:
                ghapt.cmd_sync(args)

        self.assertEqual(raised.exception.code, 1)


if __name__ == "__main__":
    unittest.main()

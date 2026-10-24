import json
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

    def test_hash_cache_discards_malformed_entries_and_non_object_roots(self):
        cache = self.root / ghapt.HASH_CACHE_NAME
        cache.write_text("[]")
        self.assertEqual(ghapt.load_hash_cache(self.st), {})

        cache.write_text(json.dumps({
            "pool/main/t/tool_1.0_amd64.deb": [
                10, 20, "a" * 32, "b" * 40, "c" * 64
            ],
            "bad-size": ["10", 20, "a" * 32, "b" * 40, "c" * 64],
            "bad-digest": [10, 20, "x" * 32, "b" * 40, "c" * 64],
        }))
        self.assertEqual(
            ghapt.load_hash_cache(self.st),
            {"pool/main/t/tool_1.0_amd64.deb": [
                10, 20, "a" * 32, "b" * 40, "c" * 64
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
        first = ([{"draft": True, "tag_name": "draft"}], {
            "Link": '<https://api.github.com/repos/o/r/releases?page=2>; rel="next"'
        })
        second = ([{"draft": False, "tag_name": "v2"}], {})
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

    def test_generate_indexes_only_reads_the_configured_component(self):
        old = self.repo / "pool" / "oldcomp" / "t" / "tool"
        current = self.repo / "pool" / "main" / "t" / "tool"
        old.mkdir(parents=True)
        current.mkdir(parents=True)
        old_deb = Path(__file__).parent / "assets" / "tooldeb_1.0.0_amd64.deb"
        (old / old_deb.name).write_bytes(old_deb.read_bytes())
        (current / old_deb.name).write_bytes(old_deb.read_bytes())

        _dist, index_files, _release = ghapt.generate_indexes(self.st)
        packages = index_files["main/binary-amd64/Packages"].decode()
        self.assertIn("Filename: pool/main/t/tool/", packages)
        self.assertNotIn("Filename: pool/oldcomp/", packages)

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

"""Exercise persistence against disposable local Git remotes; no network."""
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from save_state import STATE_FILES, save_state


def run(directory, *arguments):
    return subprocess.run(["git", *arguments], cwd=directory, check=True,
                          text=True, capture_output=True, timeout=15).stdout.strip()


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = self.root / "remote.git"
        run(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.worker = self.root / "worker"
        run(self.root, "clone", str(self.remote), str(self.worker))
        self.configure(self.worker)
        for name in STATE_FILES:
            (self.worker / name).write_text('{"cycle": 0}\n')
        (self.worker / "README.md").write_text("original code\n")
        run(self.worker, "add", ".")
        run(self.worker, "commit", "-m", "initial")
        run(self.worker, "push", "origin", "main")
        self.editor = self.root / "editor"
        run(self.root, "clone", str(self.remote), str(self.editor))
        self.configure(self.editor)

    def configure(self, directory):
        run(directory, "config", "user.name", "Test")
        run(directory, "config", "user.email", "test@example.invalid")
        run(directory, "config", "commit.gpgsign", "false")

    def modify_worker(self):
        for name in STATE_FILES:
            (self.worker / name).write_text('{"cycle": 1}\n')

    def edit_remote(self, path, content):
        (self.editor / path).write_text(content)
        run(self.editor, "add", path)
        run(self.editor, "commit", "-m", "concurrent edit")
        run(self.editor, "push", "origin", "main")

    def remote_content(self, path):
        return run(self.root, "--git-dir", str(self.remote), "show", f"main:{path}")

    def test_normal_save_publishes_all_three_outputs(self):
        self.modify_worker()
        self.assertTrue(save_state(self.worker))
        for path in STATE_FILES:
            self.assertEqual(self.remote_content(path), '{"cycle": 1}')

    def test_noop_does_not_create_commit(self):
        before = run(self.worker, "rev-parse", "HEAD")
        self.assertFalse(save_state(self.worker))
        self.assertEqual(run(self.worker, "rev-parse", "HEAD"), before)

    def test_concurrent_code_edit_is_preserved(self):
        self.modify_worker()
        self.edit_remote("README.md", "new code\n")
        self.assertTrue(save_state(self.worker))
        self.assertEqual(self.remote_content("README.md"), "new code")
        self.assertEqual(self.remote_content("signal.json"), '{"cycle": 1}')

    def test_concurrent_state_edit_is_not_overwritten(self):
        self.modify_worker()
        self.edit_remote("crypto_daytrade_state.json", '{"cycle": 99}\n')
        with self.assertRaisesRegex(RuntimeError, "changed remotely"):
            save_state(self.worker)
        self.assertEqual(self.remote_content("crypto_daytrade_state.json"), '{"cycle": 99}')
        self.assertEqual(self.remote_content("signal.json"), '{"cycle": 0}')

    def test_unexpected_staged_file_is_not_published(self):
        self.modify_worker()
        (self.worker / "private.txt").write_text("not a state file")
        run(self.worker, "add", "private.txt")
        with self.assertRaisesRegex(RuntimeError, "Unexpected staged"):
            save_state(self.worker)
        self.assertEqual(self.remote_content("signal.json"), '{"cycle": 0}')

    def test_detached_checkout_is_supported(self):
        run(self.worker, "checkout", "--detach")
        self.modify_worker()
        self.assertTrue(save_state(self.worker))
        self.assertEqual(self.remote_content("signal.json"), '{"cycle": 1}')

    def test_no_force_push_or_broad_staging_in_saver(self):
        source = (Path(__file__).resolve().parents[1] / "save_state.py").read_text()
        self.assertNotIn('"--force"', source)
        self.assertNotIn('"--force-with-lease"', source)
        self.assertNotIn('"add", "."', source)


class WorkflowGuards(unittest.TestCase):
    def test_save_precedes_notify_and_failed_outputs_are_retained(self):
        path = Path(__file__).resolve().parents[1] / ".github/workflows/hourly-scan.yml"
        source = path.read_text()
        self.assertLess(source.index("python validate_outputs.py"), source.index("python save_state.py"))
        self.assertLess(source.index("python save_state.py"), source.index("python github_notify.py"))
        self.assertIn("ref: main", source)
        self.assertIn("if: github.ref == 'refs/heads/main'", source)
        self.assertIn('cron: "13,43 * * * *"', source)
        self.assertIn("if: ${{ failure() }}", source)
        self.assertIn("actions/upload-artifact@v4", source)


if __name__ == "__main__":
    unittest.main()

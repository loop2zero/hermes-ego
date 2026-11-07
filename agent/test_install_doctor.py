"""Installer token handling and doctor checks. No live relay required."""

from __future__ import annotations

import os
import stat
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import doctor  # noqa: E402
import install  # noqa: E402


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self._tmpdir())
        self.config = self.tmp / "config"
        self.state = self.tmp / "state"
        self.units = self.tmp / "units"
        self.notes = self.tmp / "notes"
        self.env = {
            "HERMES_EGO_SKIP_SYNC": "1",
            "EGO_RELAY_TOKEN": "",
        }

    def _tmpdir(self) -> str:
        import tempfile
        path = tempfile.mkdtemp(prefix="hermes-ego-install-")
        self.addCleanup(lambda: __import__("shutil").rmtree(path, ignore_errors=True))
        return path

    def _install(self, *extra: str, token: str = "a" * 24) -> str:
        import unittest.mock
        argv = [
            "--repo", str(ROOT),
            "--role", "both",
            "--no-start",
            "--config-dir", str(self.config),
            "--state-dir", str(self.state),
            "--unit-dir", str(self.units),
            "--save-root", str(self.notes),
            "--save-tz", "UTC",
            "--image-fetch", "relay",
            "--image-budget-ms", "90000",
            "--token", token,
            "--device", "unit-mac",
        ]
        argv.extend(extra)
        from io import StringIO
        from contextlib import redirect_stdout
        buf = StringIO()
        with unittest.mock.patch.dict(os.environ, self.env, clear=False):
            with redirect_stdout(buf):
                install.main(argv)
        return buf.getvalue()

    def test_install_keeps_an_existing_token_and_does_not_print_it(self) -> None:
        first = self._install(token="relay-token-value-1")
        env_path = self.config / "relay.env"
        toml_path = self.config / "config.toml"
        self.assertNotIn("relay-token-value-1", first)
        self.assertEqual(install.env_value(install.read_env(env_path), "EGO_RELAY_TOKEN"), "relay-token-value-1")
        self.assertEqual(install.read_toml_token(toml_path), "relay-token-value-1")
        mode = stat.S_IMODE(env_path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(stat.S_IMODE(toml_path.stat().st_mode), 0o600)
        second = self._install(token="relay-token-value-2")
        self.assertIn("kept existing token", second)
        self.assertNotIn("relay-token-value-2", second)
        self.assertEqual(install.env_value(install.read_env(env_path), "EGO_RELAY_TOKEN"), "relay-token-value-1")
        self.assertEqual(install.read_toml_token(toml_path), "relay-token-value-1")
        self.assertEqual(install.env_value(install.read_env(env_path), "EGO_IMAGE_FETCH"), "relay")
        self.assertEqual(install.env_value(install.read_env(env_path), "EGO_IMAGE_BUDGET_MS"), "90000")
        toml_path.unlink()
        third = self._install(token="relay-token-value-3")
        self.assertNotIn("relay-token-value-3", third)
        self.assertEqual(install.read_toml_token(toml_path), "relay-token-value-1")
        self.assertEqual(install.env_value(install.read_env(env_path), "EGO_RELAY_TOKEN"), "relay-token-value-1")
        unit = (self.units / "hermes-ego-relay.service").read_text(encoding="utf-8")
        self.assertIn("EnvironmentFile=", unit)
        self.assertNotIn("relay-token-value-1", unit)
        self.assertTrue(self.notes.is_dir())

    def test_placeholder_token_is_replaced(self) -> None:
        env_path = self.config / "relay.env"
        self.config.mkdir()
        env_path.write_text("EGO_RELAY_TOKEN=replace-with-a-long-random-string\n", encoding="utf-8")
        os.chmod(env_path, 0o600)
        text = self._install(token="relay-token-value-9")
        self.assertNotIn("relay-token-value-9", text)
        self.assertEqual(install.env_value(install.read_env(env_path), "EGO_RELAY_TOKEN"), "relay-token-value-9")


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        doctor.CHECKS.clear()

    def test_placeholder_and_journal_and_save_root(self) -> None:
        self.assertFalse(install.token_is_real(""))
        self.assertFalse(install.token_is_real("replace-with-the-relay-token"))
        self.assertTrue(install.token_is_real("x" * 16))
        level, message = doctor.journal_verdict([("system", "failed")], "")
        self.assertEqual(level, "FAIL")
        self.assertIn("failed", message)
        level, message = doctor.journal_verdict([("user", "active")], "Traceback (most recent call last)\n")
        self.assertEqual(level, "FAIL")
        self.assertIn("traceback", message)
        level, message = doctor.journal_verdict([("user", "inactive")], "")
        self.assertEqual(level, "SKIP")

        from io import StringIO
        from contextlib import redirect_stdout
        with redirect_stdout(StringIO()):
            doctor.check_save_root("relay", {})
        self.assertTrue(any("unset" in msg for _lvl, msg in doctor.CHECKS))

    def test_doctor_fails_closed_without_printing_the_token(self) -> None:
        import tempfile
        from io import StringIO
        from contextlib import redirect_stdout
        tmp = Path(tempfile.mkdtemp(prefix="hermes-ego-doctor-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        config = tmp / "config"
        config.mkdir()
        notes = tmp / "notes"
        notes.mkdir()
        token = "doctor-token-value-77"
        (config / "relay.env").write_text(
            "\n".join([
                f'EGO_RELAY_TOKEN="{token}"',
                "EGO_RELAY_HOST=127.0.0.1",
                "EGO_RELAY_PORT=9",
                f"EGO_SAVE_ROOT={notes}",
                "EGO_IMAGE_FETCH=relay",
                "EGO_IMAGE_BUDGET_MS=90000",
            ]) + "\n",
            encoding="utf-8",
        )
        buf = StringIO()
        with redirect_stdout(buf):
            code = doctor.main([
                "--repo", str(ROOT),
                "--role", "relay",
                "--config-dir", str(config),
                "--port", "9",
            ])
        text = buf.getvalue()
        self.assertEqual(code, 1)
        self.assertNotIn(token, text)
        self.assertIn("FAIL relay is not reachable", text)
        self.assertIn("FAIL MCP tool list skipped", text)
        self.assertIn("PASS relay token is set", text)
        self.assertTrue(any(line.startswith("PASS EGO_SAVE_ROOT") for line in text.splitlines()))


if __name__ == "__main__":
    unittest.main()

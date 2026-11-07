import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from seat_reserver.batch_gui import BatchApp, ProfileEditor
from seat_reserver.batch_model import BatchSettings, Profile, load_batch
from seat_reserver.gui import SeatApp
from seat_reserver.model import Settings
from seat_reserver.protocol_runner import ProtocolRunner
from seat_reserver.runner import ReservationRunner
from seat_reserver_batch import main as batch_main


class BatchGuiTests(unittest.TestCase):
    def test_load_never_autostarts_and_only_masked_accounts_appear_in_table(self):
        root = tk.Tk()
        config = BatchSettings([Profile(settings=Settings(account="12345678901", password="fixture-password", seats="061", autostart=True))])
        try:
            with patch("seat_reserver.batch_gui.load_batch", return_value=config), patch("seat_reserver.batch_gui.BatchRunner") as runner:
                app = BatchApp(root)
                root.update()
                values = app.table.item(config.profiles[0].id)["values"]
                self.assertNotIn("12345678901", str(values))
                self.assertNotIn("fixture-password", str(values))
                self.assertIsNone(app.runner)
                self.assertFalse(app.protocol_var.get())
                runner.assert_not_called()
                self.assertLessEqual(app.log_text.winfo_y() + app.log_text.winfo_height(), app.log_text.master.winfo_height())
        finally:
            app.close()

    def test_profile_editor_only_runs_preview_and_preserves_floor_preferences(self):
        root = tk.Tk()
        profile = Profile(name="客户 A", settings=Settings(account="fixture-edit", password="fixture-password", floor="2F", seats="110,120"))
        committed = []
        try:
            editor = ProfileEditor(root, profile, committed.append)
            self.assertEqual(editor.collect().seats, "110,120")
            with patch.object(SeatApp, "start") as start:
                editor.start(test_mode=False, save=True)
                start.assert_called_once_with(test_mode=True, save=False)
            with patch.object(editor, "close"):
                editor.save()
            self.assertEqual(committed[0].settings.account, profile.settings.account)
            self.assertEqual(committed[0].settings.seats, "110,120")
            self.assertFalse(committed[0].settings.autostart)
        finally:
            editor.close()

    def test_add_and_edit_profiles_persist_encrypted_without_touching_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            path = Path(directory) / "batch.json"
            try:
                app = BatchApp(root, config_path=path)
                profile = Profile(settings=Settings(account="fixture-add", password="fixture-secret", seats="061"))
                app.commit(profile)
                profile.settings.floor = "2F"
                profile.settings.seats = "110"
                app.commit(profile)
                loaded = load_batch(path)
                self.assertEqual(len(loaded.profiles), 1)
                self.assertEqual(loaded.profiles[0].settings.seats, "110")
                self.assertNotIn("fixture-secret", path.read_text(encoding="utf-8"))
                self.assertFalse((Path(directory) / "settings.json").exists())
            finally:
                app.close()

    def test_protocol_flag_preselects_without_loading_accounts_or_starting(self):
        root = tk.Tk()
        try:
            with patch("seat_reserver.batch_gui.load_batch") as load, patch("seat_reserver.batch_gui.BatchRunner") as runner:
                app = BatchApp(root, preview=True, protocol=True)
                root.update()
                self.assertTrue(app.protocol_var.get())
                load.assert_not_called()
                runner.assert_not_called()
                self.assertIsNone(app.runner)
        finally:
            app.close()

    def test_gui_selects_factory_for_next_batch_and_locks_mode_during_run(self):
        root = tk.Tk()
        config = BatchSettings([Profile(settings=Settings(account="fixture-mode", password="fixture-password", seats="061"))])
        try:
            with patch("seat_reserver.batch_gui.load_batch", return_value=config):
                app = BatchApp(root)
            with patch.object(app, "persist"), patch("seat_reserver.batch_gui.BatchRunner") as runner:
                runner.return_value.running = False
                runner.return_value.snapshot.return_value = []
                app.start("login")
                self.assertIs(runner.call_args.kwargs["runner_factory"], ReservationRunner)
                runner.return_value.start.assert_called_with(app.collect(), mode="login")
                self.assertIn("disabled", app.protocol_check.state())
                app.set_running(False)
                app.protocol_var.set(True)
                app.start("reserve")
                self.assertIs(runner.call_args.kwargs["runner_factory"], ProtocolRunner)
                self.assertIn("disabled", app.protocol_check.state())
                runner.return_value.start.assert_called_with(app.collect(), mode="reserve")
        finally:
            app.close()

    def test_cli_protocol_gui_flag_is_forwarded(self):
        with patch("seat_reserver.batch_gui.main") as gui:
            self.assertEqual(batch_main(["--preview", "--protocol"]), 0)
            gui.assert_called_once_with(preview=True, config_path=None, protocol=True)

    def test_cli_protocol_selects_factory_without_running_accounts(self):
        config = BatchSettings([Profile(settings=Settings(account="fixture-cli", password="fixture-password", seats="061"))])
        with patch("seat_reserver_batch.load_batch", return_value=config), patch("seat_reserver_batch.BatchRunner") as batch, patch("builtins.print"):
            batch.return_value.wait.return_value = True
            batch.return_value.snapshot.return_value = [{"name": "dummy", "account": "fix***cli", "code": "LOGIN_READY"}]
            self.assertEqual(batch_main(["--protocol", "--test-login"]), 0)
            self.assertIs(batch.call_args.kwargs["runner_factory"], ProtocolRunner)
            batch.return_value.start.assert_called_once_with(config, mode="login")


if __name__ == "__main__":
    unittest.main()

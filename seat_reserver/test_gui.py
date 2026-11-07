import tkinter as tk
import unittest
from unittest.mock import patch

from seat_reserver.gui import SeatApp
from seat_reserver.model import Settings, floor_key


class GuiTests(unittest.TestCase):
    def test_explicit_test_start_never_schedules_a_reservation(self):
        root = tk.Tk()
        settings = Settings(account="fixture-user", password="fixture-password", seats="061", autostart=True)
        try:
            with patch("seat_reserver.gui.load_settings", return_value=settings), patch.object(SeatApp, "start") as start:
                app = SeatApp(root, test_seats=True)
                root.after(700, root.quit)
                root.mainloop()
                start.assert_called_once_with(test_mode=True, save=False)
        finally:
            app.close()

    def test_form_is_visible_and_password_is_masked(self):
        root = tk.Tk()
        try:
            app = SeatApp(root, preview=True)
            app.remember_catalog({"kind": "catalog", "campus": app.vars["campus"].get(), "floor": "3F", "day": "2026-10-10", "interval": "20:00-21:30", "labels": ["001", "061"]})
            root.update()
            for widget, _ in app.form_widgets:
                parent = widget.master
                self.assertLessEqual(widget.winfo_y() + widget.winfo_height(), parent.winfo_height(), str(widget))
                self.assertLessEqual(widget.winfo_x() + widget.winfo_width(), parent.winfo_width(), str(widget))
            password = next(w for w, _ in app.form_widgets if str(w.cget("textvariable")) == str(app.vars["password"]))
            self.assertTrue(password.cget("show"))
            self.assertEqual(str(app.date_entry.cget("state")), "disabled")
            self.assertFalse(app.worker)
        finally:
            app.close()

    def test_floor_change_clears_and_restores_only_its_own_candidates(self):
        root = tk.Tk()
        try:
            app = SeatApp(root, preview=True)
            app.vars["seats"].set("584,487")
            app.vars["floor"].set("2F")
            self.assertEqual(app.vars["seats"].get(), "")
            app.vars["seats"].set("110,120")
            app.vars["floor"].set("3F")
            self.assertEqual(app.vars["seats"].get(), "584,487")
            saved = app.collect()
            self.assertEqual(saved.floor_seats[floor_key(saved.campus, "2F")], "110,120")
            app.remember_catalog({"kind": "catalog", "campus": saved.campus, "floor": "2F", "day": "2026-10-10", "labels": ["110", "120"]})
            self.assertEqual(str(app.pick_button.cget("state")), "disabled")
            app.vars["floor"].set("2F")
            self.assertEqual(str(app.pick_button.cget("state")), "normal")
            app.vars["seats"].set("584")
            with self.assertRaises(ValueError):
                app.validate_catalog(app.collect())
        finally:
            app.close()

    def test_picker_uses_this_floor_catalog_and_preserves_priority(self):
        root = tk.Tk()
        try:
            app = SeatApp(root, preview=True)
            app.vars["floor"].set("2F")
            app.vars["seats"].set("120,110")
            app.remember_catalog({"kind": "catalog", "campus": app.vars["campus"].get(), "floor": "2F", "day": "2026-10-10", "labels": ["0007", "110", "120"]})
            app.choose_seats()
            root.update()
            def descendants(widget):
                result = list(widget.winfo_children())
                for child in widget.winfo_children():
                    result.extend(descendants(child))
                return result
            lists = [w for w in descendants(app.picker_window) if isinstance(w, tk.Listbox)]
            self.assertEqual(lists[0].get(0, "end"), ("0007", "110", "120"))
            self.assertEqual(lists[1].get(0, "end"), ("120", "110"))
            button = next(w for w in descendants(app.picker_window) if w.winfo_class() == "TButton" and w.cget("text") == "使用这些候选座位")
            button.invoke()
            self.assertEqual(app.vars["seats"].get(), "120,110")
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()

import importlib.util, json, pathlib, sqlite3, subprocess, tempfile, unittest, xml.etree.ElementTree as ET
from unittest.mock import patch

p = pathlib.Path(__file__).resolve().parents[1] / "pixel_uploader.py"
spec = importlib.util.spec_from_file_location("uploader", p)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class SafetyTests(unittest.TestCase):
    def test_missing_backup_status_recovers_then_requires_completion(self):
        clock = {"now": 0}

        def wait(seconds):
            clock["now"] += seconds

        readings = [(False, None, ["Backup is on", "Original"])] * 5 + [
            (True, None, [])
        ] * 2
        with (
            patch.object(mod.time, "monotonic", side_effect=lambda: clock["now"]),
            patch.object(mod, "temperature", return_value=35),
            patch.object(mod, "backup_view", side_effect=readings),
            patch.object(mod, "publish"),
            patch.object(mod, "rest", side_effect=wait),
            patch.object(mod, "adb") as adb,
        ):
            mod.await_backup(None, [])
            self.assertEqual(
                sum("force-stop" in call.args for call in adb.call_args_list), 1
            )
            self.assertGreaterEqual(clock["now"], 130)

    def test_persistently_missing_backup_status_stops_after_bounded_recovery(self):
        clock = {"now": 0}

        def wait(seconds):
            clock["now"] += seconds

        with (
            patch.object(mod.time, "monotonic", side_effect=lambda: clock["now"]),
            patch.object(mod, "temperature", return_value=35),
            patch.object(
                mod,
                "backup_view",
                return_value=(False, None, ["Backup is on", "Original"]),
            ),
            patch.object(mod, "publish"),
            patch.object(mod, "rest", side_effect=wait),
            patch.object(mod, "adb") as adb,
        ):
            with self.assertRaises(RuntimeError):
                mod.await_backup(None, [])
            self.assertEqual(
                sum("force-stop" in call.args for call in adb.call_args_list), 2
            )

    def test_fast_completion_checks_keep_initial_indexing_grace(self):
        clock = {"now": 0}
        waits = []

        def wait(seconds):
            clock["now"] += seconds
            waits.append(seconds)

        with (
            patch.object(mod.time, "monotonic", side_effect=lambda: clock["now"]),
            patch.object(mod, "temperature", return_value=35),
            patch.object(mod, "backup_view", return_value=(True, None, [])) as view,
            patch.object(mod, "publish"),
            patch.object(mod, "rest", side_effect=wait),
        ):
            mod.await_backup(None, [])
            self.assertGreaterEqual(clock["now"], 65)
            self.assertEqual(waits[-1], 5)
            self.assertGreaterEqual(view.call_count, 2)

    def test_incomplete_backup_resets_fast_completion_checks(self):
        clock = {"now": 0}
        readings = [
            (False, 30, []),
            (False, 30, []),
            (True, None, []),
            (False, 1, []),
            (True, None, []),
            (True, None, []),
        ]

        def wait(seconds):
            clock["now"] += seconds

        with (
            patch.object(mod.time, "monotonic", side_effect=lambda: clock["now"]),
            patch.object(mod, "temperature", return_value=35),
            patch.object(mod, "backup_view", side_effect=readings) as view,
            patch.object(mod, "publish"),
            patch.object(mod, "rest", side_effect=wait),
        ):
            mod.await_backup(None, [])
            self.assertEqual(view.call_count, 6)

    def test_backup_failure_still_stops_before_cleanup(self):
        with (
            patch.object(mod, "temperature", return_value=35),
            patch.object(
                mod, "backup_view", return_value=(False, None, ["Backup failed"])
            ),
            patch.object(mod, "publish"),
            patch.object(mod, "rest") as wait,
        ):
            with self.assertRaises(RuntimeError):
                mod.await_backup(None, [])
            wait.assert_not_called()

    def test_copy_verification_reads_only_active_files(self):
        items = [
            {"phone": mod.PHONE + "/000001_abcdef012345.jpg", "uploadSha256": "a" * 64}
        ]
        output = "a" * 64 + "  " + items[0]["phone"] + "\n"
        with patch.object(mod, "adb", return_value=output) as adb:
            mod.verify_phone_copies(items)
            self.assertEqual(
                adb.call_args.args, ("shell", "sha256sum " + items[0]["phone"])
            )

    def test_missing_or_changed_phone_copy_blocks_registration(self):
        items = [
            {"phone": mod.PHONE + "/000001_abcdef012345.jpg", "uploadSha256": "a" * 64}
        ]
        for output in ("", "b" * 64 + "  " + items[0]["phone"] + "\n"):
            with patch.object(mod, "adb", return_value=output):
                with self.assertRaises(RuntimeError):
                    mod.verify_phone_copies(items)

    def test_copy_verification_covers_all_large_batch_files(self):
        items = [
            {
                "phone": mod.PHONE + f"/{index:06d}_abcdef012345.jpg",
                "uploadSha256": "a" * 64,
            }
            for index in range(400)
        ]

        def hashes(*args, **kwargs):
            return "\n".join("a" * 64 + "  " + path for path in args[1].split()[1:])

        with patch.object(mod, "adb", side_effect=hashes) as adb:
            mod.verify_phone_copies(items)
            scanned = [
                path for call in adb.call_args_list for path in call.args[1].split()[1:]
            ]
            self.assertEqual(scanned, [row["phone"] for row in items])

    def test_media_scan_batches_adb_calls_and_covers_each_file(self):
        items = [
            {"phone": mod.PHONE + f"/{index:06d}_abcdef012345.jpg"}
            for index in range(21)
        ]
        with (
            patch.object(mod, "adb") as adb,
            patch.object(mod, "publish"),
            patch.object(mod, "temperature", return_value=35),
            patch.object(mod, "stop_requested", return_value=False),
        ):
            mod.register_media(None, items)
            self.assertEqual(adb.call_count, 2)
            scripts = [call.args[1] for call in adb.call_args_list]
            for row in items:
                self.assertEqual(
                    sum(
                        script.count("file://" + row["phone"] + " ")
                        for script in scripts
                    ),
                    1,
                )

    def test_media_scan_rejects_unexpected_path_before_any_command(self):
        with patch.object(mod, "adb") as adb:
            with self.assertRaises(RuntimeError):
                mod.register_media(None, [{"phone": mod.PHONE + "/bad;file.jpg"}])
            adb.assert_not_called()

    def test_media_scan_failure_does_not_continue_registration(self):
        items = [{"phone": mod.PHONE + "/000001_abcdef012345.jpg"}]
        with (
            patch.object(mod, "adb", side_effect=RuntimeError("connection lost")),
            patch.object(mod, "publish") as publish,
            patch.object(mod, "stop_requested", return_value=False),
        ):
            with self.assertRaises(RuntimeError):
                mod.register_media(None, items)
            publish.assert_not_called()

    def test_empty_prepared_journal_advances_to_next_pending_batch(self):
        with tempfile.TemporaryDirectory() as folder:
            state = pathlib.Path(folder)
            active = state / "active-batch.json"
            active.write_text(json.dumps({"phase": "prepared", "items": []}))
            db = sqlite3.connect(":memory:")
            db.row_factory = sqlite3.Row
            db.execute("CREATE TABLE media(id INTEGER,state TEXT,path TEXT,note TEXT)")
            db.execute("INSERT INTO media VALUES(1,'pending','a.jpg',NULL)")

            def prepare(database):
                self.assertFalse(active.exists())
                database.execute("UPDATE media SET state='done'")
                return []

            with (
                patch.object(mod, "STATE", state),
                patch.object(mod, "ROOT", state),
                patch.object(mod, "build_catalog"),
                patch.object(mod, "temperature", return_value=35),
                patch.object(mod, "free_bytes", return_value=10**10),
                patch.object(mod, "unlock_phone"),
                patch.object(mod, "adb"),
                patch.object(mod, "backup_view"),
                patch.object(mod, "cool_down"),
                patch.object(mod, "stop_requested", return_value=False),
                patch.object(mod, "publish"),
                patch.object(mod, "prepare_batch", side_effect=prepare) as next_batch,
            ):
                mod.run(db)
                next_batch.assert_called_once()
            db.close()

    def test_wake_screen_checks_awake_display(self):
        with (
            patch.object(
                mod,
                "adb",
                side_effect=["", "mWakefulness=Awake\nDisplay Power: state=ON"],
            ) as adb,
            patch.object(mod.time, "sleep"),
        ):
            mod.wake_screen()
            self.assertEqual(adb.call_count, 2)
            self.assertIn("KEYCODE_WAKEUP", adb.call_args_list[0].args)

    def test_wake_screen_retries_without_power_toggle(self):
        with (
            patch.object(
                mod,
                "adb",
                side_effect=["", "mWakefulness=Asleep\nDisplay Power: state=OFF"] * 3,
            ) as adb,
            patch.object(mod.time, "sleep"),
        ):
            with self.assertRaises(RuntimeError):
                mod.wake_screen()
            self.assertEqual(adb.call_count, 6)
            self.assertFalse(
                any("KEYCODE_POWER" in call.args for call in adb.call_args_list)
            )

    def test_cooling_keeps_screen_on(self):
        with (
            patch.object(mod, "temperature", side_effect=[43, 39]),
            patch.object(mod, "adb") as adb,
            patch.object(mod, "wake_screen") as wake,
            patch.object(mod, "unlock_phone"),
        ):
            mod.cool_down(None)
            wake.assert_called_once()
            self.assertFalse(
                any("KEYCODE_SLEEP" in call.args for call in adb.call_args_list)
            )

    def test_metadata_failure_isolated_to_known_file(self):
        result = subprocess.CompletedProcess(
            [],
            1,
            "    1 image files updated\n    0 image files updated\n    1 files weren't updated due to errors\n",
            "Error: Bad IFD - /stage/b.png\n",
        )
        self.assertEqual(
            mod.metadata_failures(result, ["/stage/a.jpg", "/stage/b.png"]),
            {"/stage/b.png"},
        )

    def test_unknown_metadata_failure_stops(self):
        result = subprocess.CompletedProcess([], 1, "", "Error: Cannot load tool\n")
        with self.assertRaises(RuntimeError):
            mod.metadata_failures(result, ["/stage/a.jpg"])

    def test_metadata_error_matches_windows_path_separators(self):
        destination = r"C:\Work\stage\bad.png"
        result = subprocess.CompletedProcess(
            [],
            1,
            "    0 image files updated\n",
            "Error: Bad IFD - C:/Work/stage/bad.png\n",
        )
        self.assertEqual(mod.metadata_failures(result, [destination]), {destination})

    def test_missing_metadata_success_stops(self):
        result = subprocess.CompletedProcess([], 0, "    1 image files updated\n", "")
        with self.assertRaises(RuntimeError):
            mod.metadata_failures(result, ["/stage/a.jpg", "/stage/b.png"])

    def test_metadata_failure_excludes_working_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            state = pathlib.Path(folder)
            local = state / "bad.png"
            local.write_bytes(b"working copy")
            db = sqlite3.connect(":memory:")
            db.execute("CREATE TABLE media(id INTEGER,state TEXT,note TEXT)")
            db.execute("INSERT INTO media VALUES(1,'pending',NULL)")
            row = {"id": 1, "local": str(local)}
            result = subprocess.CompletedProcess(
                [], 1, "    0 image files updated\n", f"Error: Bad IFD - {local}\n"
            )
            with (
                patch.object(mod, "STATE", state),
                patch.object(mod.subprocess, "run", return_value=result),
            ):
                mod.write_metadata(
                    db, [row], ["-overwrite_original", str(local), "-execute"]
                )
            self.assertTrue(row["review"])
            self.assertFalse(local.exists())
            self.assertEqual(
                db.execute("SELECT state FROM media").fetchone()[0], "review"
            )
            db.close()

    def test_server_connection_failure_retries(self):
        error = RuntimeError("adb.exe: cannot connect to daemon at tcp:5037")
        with (
            patch.object(mod, "command", side_effect=[error, "ok"]) as command,
            patch.object(mod, "rest") as rest,
        ):
            self.assertEqual(mod.adb("devices"), "ok")
            self.assertEqual(command.call_count, 2)
            rest.assert_called_once_with(5)

    def test_server_start_failure_retries(self):
        error = RuntimeError(
            "could not read ok from ADB Server\n* failed to start daemon\nadb.exe: cannot connect to daemon"
        )
        with (
            patch.object(mod, "command", side_effect=[error, "ok"]) as command,
            patch.object(mod, "rest"),
        ):
            self.assertEqual(mod.adb("devices"), "ok")
            self.assertEqual(command.call_count, 2)

    def test_server_connection_retries_are_bounded(self):
        error = RuntimeError("adb.exe: cannot connect to daemon at tcp:5037")
        with (
            patch.object(mod, "command", side_effect=error) as command,
            patch.object(mod, "rest"),
        ):
            with self.assertRaises(RuntimeError):
                mod.adb("devices")
            self.assertEqual(command.call_count, 3)

    def test_uncertain_phone_command_is_not_replayed(self):
        with (
            patch.object(
                mod, "command", side_effect=RuntimeError("device offline")
            ) as command,
            patch.object(mod, "rest") as rest,
        ):
            with self.assertRaises(RuntimeError):
                mod.adb("shell", "input", "tap", "10", "20")
            command.assert_called_once()
            rest.assert_not_called()

    def test_background_app_recovers_once(self):
        background = ET.fromstring(
            '<hierarchy><node package="com.android.settings"/></hierarchy>'
        )
        foreground = ET.fromstring(
            '<hierarchy><node package="com.google.android.apps.photos"/></hierarchy>'
        )
        with (
            patch.object(mod, "raw_ui", side_effect=[background, foreground]),
            patch.object(mod, "unlock_phone") as unlock,
            patch.object(mod, "adb") as adb,
            patch.object(mod, "rest"),
        ):
            self.assertIs(mod.ui(), foreground)
            unlock.assert_called_once()
            adb.assert_called_once()

    def test_failed_foreground_recovery_stops(self):
        background = ET.fromstring(
            '<hierarchy><node package="com.android.settings"/></hierarchy>'
        )
        with (
            patch.object(mod, "raw_ui", return_value=background) as raw,
            patch.object(mod, "unlock_phone") as unlock,
            patch.object(mod, "adb"),
            patch.object(mod, "rest"),
        ):
            with self.assertRaises(RuntimeError):
                mod.ui()
            unlock.assert_called_once()
            self.assertEqual(raw.call_count, 4)

    def test_photos_not_responding_restarts_only_photos(self):
        stalled = ET.fromstring(
            '<hierarchy><node package="android" resource-id="android:id/alertTitle" text="Photos isn\'t responding"/></hierarchy>'
        )
        foreground = ET.fromstring(
            '<hierarchy><node package="com.google.android.apps.photos"/></hierarchy>'
        )
        with (
            patch.object(mod, "raw_ui", side_effect=[stalled, foreground]),
            patch.object(mod, "unlock_phone"),
            patch.object(mod, "adb") as adb,
            patch.object(mod, "rest"),
        ):
            self.assertIs(mod.ui(), foreground)
            self.assertEqual(
                adb.call_args_list[0].args,
                ("shell", "am", "force-stop", "com.google.android.apps.photos"),
            )
            self.assertEqual(adb.call_count, 2)

    def test_other_app_not_responding_is_not_force_stopped(self):
        other = ET.fromstring(
            '<hierarchy><node package="android" resource-id="android:id/alertTitle" text="Maps isn\'t responding"/></hierarchy>'
        )
        with (
            patch.object(mod, "raw_ui", return_value=other),
            patch.object(mod, "unlock_phone"),
            patch.object(mod, "adb") as adb,
            patch.object(mod, "rest"),
        ):
            with self.assertRaises(RuntimeError):
                mod.ui()
            self.assertFalse(
                any("force-stop" in call.args for call in adb.call_args_list)
            )

    def test_repeated_photos_hang_has_bounded_restarts(self):
        stalled = ET.fromstring(
            '<hierarchy><node package="android" resource-id="android:id/alertTitle" text="Photos isn\'t responding"/></hierarchy>'
        )
        with (
            patch.object(mod, "raw_ui", return_value=stalled),
            patch.object(mod, "unlock_phone"),
            patch.object(mod, "adb") as adb,
            patch.object(mod, "rest"),
        ):
            with self.assertRaises(RuntimeError):
                mod.ui()
            self.assertEqual(
                sum("force-stop" in call.args for call in adb.call_args_list), 2
            )

    def test_incomplete_backup_cannot_clean(self):
        with (
            patch.object(mod, "backup_view", return_value=(False, 1, ["1 item left"])),
            patch.object(mod, "tap") as tap,
        ):
            with self.assertRaises(RuntimeError):
                mod.cleanup(None, [])
            tap.assert_not_called()

    def test_wrong_account_cannot_continue(self):
        xml = ET.fromstring(
            '<hierarchy><node text="Backup is on"/><node text="Original"/><node text="other@example.com" resource-id="photos_backup_overview_account"/></hierarchy>'
        )
        with patch.object(mod, "ui", return_value=xml):
            with self.assertRaises(RuntimeError):
                mod.backup_view()

    def test_cleanup_requires_safely_backed_up_message(self):
        menu = ET.fromstring(
            '<hierarchy><node text="Free up space on this device" bounds="[0,0][10,10]"/></hierarchy>'
        )
        unsafe = ET.fromstring(
            '<hierarchy><node text="Delete photos" resource-id="free_up_button" bounds="[0,0][10,10]"/></hierarchy>'
        )
        with (
            patch.object(mod, "backup_view", return_value=(True, None, [])),
            patch.object(mod, "account_menu", return_value=menu),
            patch.object(mod, "ui", return_value=unsafe),
            patch.object(mod, "tap") as tap,
        ):
            with self.assertRaises(RuntimeError):
                mod.cleanup(None, [])
            self.assertEqual(tap.call_count, 1)  # Opens cleanup; never confirms.

    def test_retained_file_is_not_marked_done(self):
        menu = ET.fromstring(
            '<hierarchy><node text="Free up space on this device" bounds="[0,0][10,10]"/></hierarchy>'
        )
        safe = ET.fromstring(
            '<hierarchy><node text="These items are already safely backed up"/><node resource-id="free_up_button" bounds="[0,0][10,10]"/></hierarchy>'
        )
        done = ET.fromstring(
            '<hierarchy><node resource-id="free_up_space_completed_title"/></hierarchy>'
        )
        expected = [{"phone": "/phone/item.jpg"}]
        with (
            patch.object(mod, "backup_view", return_value=(True, None, [])),
            patch.object(mod, "account_menu", return_value=menu),
            patch.object(mod, "ui", side_effect=[safe, done]),
            patch.object(mod, "tap"),
            patch.object(mod, "local_files", return_value={"/phone/item.jpg"}),
        ):
            self.assertEqual(mod.cleanup(None, expected), expected)

    def test_nothing_to_free_requires_saved_cleanup_phase(self):
        menu = ET.fromstring(
            '<hierarchy><node text="Free up space on this device" bounds="[0,0][10,10]"/></hierarchy>'
        )
        empty = ET.fromstring(
            '<hierarchy><node text="Nothing to free up"/></hierarchy>'
        )
        expected = [{"phone": "/phone/item.jpg"}]
        with (
            patch.object(mod, "backup_view", return_value=(True, None, [])),
            patch.object(mod, "account_menu", return_value=menu),
            patch.object(mod, "ui", return_value=empty),
            patch.object(mod, "tap"),
            patch.object(mod, "local_files", return_value={"/phone/item.jpg"}),
        ):
            with self.assertRaises(RuntimeError):
                mod.cleanup(None, expected)
            self.assertEqual(mod.cleanup(None, expected, allow_empty=True), expected)

    def test_finish_batch_keeps_retained_item_in_review(self):
        with tempfile.TemporaryDirectory() as folder:
            state = pathlib.Path(folder)
            stage = state / "staging"
            stage.mkdir()
            items = []
            for item_id in (1, 2):
                local = stage / f"{item_id}.jpg"
                local.write_bytes(b"working copy")
                items.append(
                    {
                        "id": item_id,
                        "local": str(local),
                        "phone": f"/phone/{item_id}.jpg",
                    }
                )
            db = sqlite3.connect(":memory:")
            db.execute("CREATE TABLE media(id INTEGER,state TEXT,note TEXT)")
            db.executemany(
                "INSERT INTO media VALUES(?,?,?)",
                [(1, "transferred", None), (2, "transferred", None)],
            )
            with (
                patch.object(mod, "STATE", state),
                patch.object(mod, "cleanup", return_value=[items[1]]),
                patch.object(mod, "publish"),
                patch.object(mod, "adb") as adb,
            ):
                mod.finish_batch(db, items)
                self.assertEqual(
                    db.execute("SELECT id,state FROM media ORDER BY id").fetchall(),
                    [(1, "done"), (2, "review")],
                )
                adb.assert_not_called()  # Never deletes the retained phone copy.
            db.close()


if __name__ == "__main__":
    unittest.main()

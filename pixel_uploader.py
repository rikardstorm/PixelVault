"""Takeout -> original Pixel, using Google Photos' own local cleanup.

Never deletes cloud items or deletes phone media with adb rm. Originals remain
inside the two supplied ZIP archives. Unknown UI states stop the run.
"""

import argparse, datetime as dt, hashlib, json, logging, os, pathlib, re
import shutil, sqlite3, subprocess, sys, time, zipfile
import xml.etree.ElementTree as ET

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
ROOT = PROJECT_ROOT / ".pixel-uploader"
WORK = ROOT
STATE = ROOT
ADB = pathlib.Path("adb")
EXIF = pathlib.Path("exiftool")
ARCHIVES = []
PHONE = "/storage/emulated/0/Pictures/PixelTakeout"
ACCOUNT = "example@gmail.com"
SERIAL = ""
MEDIA = {
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".webp",
    ".gif",
    ".avif",
    ".dng",
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
    ".mkv",
    ".3gp",
    ".mts",
    ".m2ts",
    ".mpg",
    ".mpeg",
    ".wmv",
}
VIDEO = {
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
    ".mkv",
    ".3gp",
    ".mts",
    ".m2ts",
    ".mpg",
    ".mpeg",
    ".wmv",
}
MAX_FILES, MAX_BYTES, MIN_FREE = 200, 500_000_000, 5_000_000_000
HOT, COOL, START_MAX = 43.0, 40.0, 41.0
LOG = logging.getLogger("pixel")


def command(args, timeout=120):
    result = subprocess.run(
        [str(x) for x in args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(
            f"Command failed: {args[0]}: {result.stderr[-500:]} {result.stdout[-500:]}"
        )
    return result.stdout


def adb(*args, timeout=40):
    for attempt in range(3):
        try:
            return command([ADB, "-s", SERIAL, *args], timeout)
        except RuntimeError as error:
            # Retry only explicit host-server connection failures: the phone
            # command was not sent. Never replay an uncertain tap or PIN key.
            message = str(error).lower()
            if "cannot connect to daemon" not in message or attempt == 2:
                raise
            LOG.warning(
                "ADB server unavailable; retrying connection (%d/2).", attempt + 1
            )
            rest(5)


def shell(script):
    return adb("shell", script)


def file_hash(path):
    digest = hashlib.sha256()
    with pathlib.Path(path).open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def publish(db, phase, **extra):
    counts = dict(db.execute("SELECT state,count(*) FROM media GROUP BY state"))
    state = {
        "updated": dt.datetime.now().isoformat(),
        "phase": phase,
        "counts": counts,
        **extra,
    }
    atomic_json(STATE / "status.json", state)
    labels = {
        "done": "Done",
        "pending": "Pending",
        "duplicate": "Duplicates",
        "prepared": "Prepared",
        "transferred": "On phone",
        "review": "Needs review",
    }
    lines = [
        "PIXEL TAKEOUT UPLOADER",
        f'Updated: {state["updated"]}',
        f"Status: {phase}",
        "",
    ]
    lines += [f"{labels.get(k,k)}: {v}" for k, v in counts.items()]
    if "batteryC" in extra:
        lines.append(f'Battery temperature: {extra["batteryC"]} °C')
    if extra.get("remaining") is not None:
        lines.append(f'Photos reports remaining: {extra["remaining"]}')
    if extra.get("error"):
        lines += ["", f'Stopped because: {extra["error"]}']
    (ROOT / "status.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def stop_requested():
    return (ROOT / "STOP.txt").exists()


def rest(seconds=30):
    for _ in range(int(seconds)):
        if stop_requested():
            raise InterruptedError("Stop requested; resume data retained.")
        time.sleep(1)


def temperature():
    text = adb("shell", "dumpsys", "battery")
    m = re.search(r"temperature:\s*(\d+)", text)
    if not m:
        raise RuntimeError("Battery temperature unavailable")
    return int(m[1]) / 10


def raw_ui():
    adb("shell", "uiautomator", "dump", "/sdcard/pixel-uploader-ui.xml")
    xml = adb("shell", "cat", "/sdcard/pixel-uploader-ui.xml")
    (STATE / "last-ui.xml").write_text(xml, encoding="utf-8")
    tree = ET.fromstring(xml)
    return tree


def photos_not_responding(tree):
    return any(
        n.get("package") == "android"
        and n.get("resource-id") == "android:id/alertTitle"
        and n.get("text") == "Photos isn't responding"
        for n in tree.iter("node")
    )


def ui():
    tree = raw_ui()

    def available(t):
        return not photos_not_responding(t) and any(
            n.get("package") == "com.google.android.apps.photos" for n in t.iter("node")
        )

    if available(tree):
        return tree
    LOG.info("Restoring Google Photos after it left the foreground.")
    unlock_phone()
    restarts = 0
    if photos_not_responding(tree):
        LOG.warning("Photos is not responding; restarting app with media retained.")
        adb("shell", "am", "force-stop", "com.google.android.apps.photos")
        restarts += 1
    adb(
        "shell",
        "monkey",
        "-p",
        "com.google.android.apps.photos",
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
    )
    for attempt in range(3):
        rest(5)  # Allow the old phone time to launch before reading its UI.
        tree = raw_ui()
        if available(tree):
            return tree
        if photos_not_responding(tree) and restarts < 2 and attempt < 2:
            LOG.warning("Photos is still not responding; retrying app restart.")
            adb("shell", "am", "force-stop", "com.google.android.apps.photos")
            restarts += 1
            adb(
                "shell",
                "monkey",
                "-p",
                "com.google.android.apps.photos",
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            )
    raise RuntimeError(
        "Google Photos remains unavailable after bounded recovery; copies retained"
    )


def wake_screen():
    for attempt in range(3):
        # WAKEUP is idempotent, unlike POWER which can turn an awake screen off.
        adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        time.sleep(1)
        power = adb("shell", "dumpsys", "power")
        if (
            re.search(r"mWakefulness=Awake\b", power)
            and "Display Power: state=ON" in power
        ):
            return
    raise RuntimeError(
        "Phone display did not wake after three attempts; copies retained"
    )


def unlock_phone():
    wake_screen()
    policy = adb("shell", "dumpsys", "window", "policy")
    showing = re.search(r"mIsShowing=(true|false)", policy)
    if showing is None:
        raise RuntimeError("Cannot determine phone lock state")
    if showing[1] == "false":
        return
    from phone_credentials import load

    pin = load(SERIAL)
    if pin is None:
        raise RuntimeError("Phone is locked; unlock it manually")
    tree = raw_ui()
    entry = find(tree, rid=":id/pinEntry")
    if entry is None:
        # Use actual observed display bounds to reveal the PIN keypad.
        first = next(tree.iter("node"))
        x1, y1, x2, y2 = map(int, re.findall(r"\d+", first.get("bounds", "")))
        adb(
            "shell",
            "input",
            "swipe",
            str((x1 + x2) // 2),
            str(int(y2 * 0.8)),
            str((x1 + x2) // 2),
            str(int(y2 * 0.3)),
            "400",
        )
        tree = raw_ui()
        entry = find(tree, rid=":id/pinEntry")
    if entry is None or entry.get("package") != "com.android.systemui":
        raise RuntimeError(
            "Expected Android PIN screen is not visible; unlock not attempted"
        )
    # Never put the full PIN in an adb command line or in the run log.
    keys = {
        digit: find(tree, rid=f"com.android.systemui:id/key{digit}")
        for digit in set(pin)
    }
    enter = find(tree, rid="com.android.systemui:id/key_enter")
    if any(node is None for node in keys.values()) or enter is None:
        raise RuntimeError("Unknown PIN keypad; unlock not attempted")
    for digit in pin:
        tap(keys[digit])
    tap(enter)
    # One attempt only; never retry a rejected PIN.
    time.sleep(1)
    policy = adb("shell", "dumpsys", "window", "policy")
    if "mIsShowing=false" not in policy:
        raise RuntimeError("Unlock did not succeed; no further attempts will be made")


def find(tree, text=None, rid=None, desc=None):
    found = [
        n
        for n in tree.iter("node")
        if (text is None or n.get("text") == text)
        and (rid is None or n.get("resource-id", "").endswith(rid))
        and (desc is None or n.get("content-desc") == desc)
    ]
    return found[0] if len(found) == 1 else None


def tap(node):
    if node is None:
        raise RuntimeError("Expected Photos control is missing or ambiguous")
    x1, y1, x2, y2 = map(int, re.findall(r"\d+", node.get("bounds", "")))
    if x2 <= x1 or y2 <= y1:
        raise RuntimeError("Photos control is off screen")
    adb("shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))


def texts(tree):
    return [n.get("text") for n in tree.iter("node") if n.get("text")]


def account_menu():
    for _ in range(6):
        tree = ui()
        if (
            find(tree, text="Free up space on this device") is not None
            and find(tree, text="Backup") is not None
        ):
            if ACCOUNT not in texts(tree):
                raise RuntimeError("Unexpected Photos account")
            return tree
        disc = find(tree, rid="selected_account_disc")
        if disc is not None:
            if ACCOUNT not in disc.get("content-desc", ""):
                raise RuntimeError("Unexpected Photos account")
            tap(disc)
            continue
        done = find(tree, rid="done_button")
        if done is not None:
            tap(done)
            continue
        adb("shell", "input", "keyevent", "KEYCODE_BACK")
    raise RuntimeError("Cannot navigate to Photos account menu")


def backup_view():
    tree = ui()
    if find(tree, rid="photos_backup_overview_account") is not None:
        pass
    else:
        tree = account_menu()
        tap(find(tree, text="Backup"))
        tree = ui()
    values = texts(tree)
    if (
        ACCOUNT not in values
        or "Original" not in values
        or "Backup is on" not in values
    ):
        raise RuntimeError("Wrong account, quality, or backup disabled")
    remaining = None
    for value in values:
        m = re.search(r"([\d,]+) items? left", value)
        if not m:
            m = re.search(r"Backing up ([\d,]+) items?", value)
        if m:
            remaining = int(m[1].replace(",", ""))
    complete = "Your backup is complete" in values
    return complete, remaining, values


def local_files():
    output = shell(f"find {PHONE} -maxdepth 1 -type f")
    return {line.strip() for line in output.splitlines() if line.strip()}


def free_bytes():
    output = adb("shell", "df", "-k", "/sdcard").splitlines()
    return int(output[-1].split()[3]) * 1024


def cool_down(db, ceiling=START_MAX):
    if temperature() < ceiling:
        return
    LOG.info("Cooling pause; Google Photos temporarily stopped.")
    adb("shell", "am", "force-stop", "com.google.android.apps.photos")
    adb("shell", "settings", "put", "system", "screen_brightness", "1")
    wake_screen()
    deadline = time.monotonic() + 3600
    while temperature() > COOL:
        publish(db, "Cooling", batteryC=temperature())
        if time.monotonic() > deadline:
            raise RuntimeError(
                "Phone did not cool within one hour. Disconnect charging or use a fan."
            )
        rest(30)
    unlock_phone()
    adb("shell", "settings", "put", "system", "screen_brightness", "5")
    adb(
        "shell",
        "monkey",
        "-p",
        "com.google.android.apps.photos",
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
    )


def cleanup(db, expected, allow_empty=False):
    # Google Photos decides which copies are safely backed up. Never rm media.
    complete, remaining, _ = backup_view()
    if not complete or remaining not in (None, 0):
        raise RuntimeError("Backup is not complete before local cleanup")
    tree = account_menu()
    tap(find(tree, text="Free up space on this device"))
    tree = ui()
    values = texts(tree)
    button = find(tree, rid="free_up_button")
    if allow_empty and "Nothing to free up" in values:
        # On a saved cleanup phase, removed copies were already accepted by
        # Photos. Copies it still keeps remain unverified and go to review.
        remaining_files = local_files()
        return [r for r in expected if r["phone"] in remaining_files]
    if button is None or not any("already safely backed up" in v for v in values):
        raise RuntimeError(
            "Photos did not offer verified local cleanup; copies retained"
        )
    LOG.info("Photos cleanup: %s", button.get("text"))
    tap(button)
    deadline = time.monotonic() + 300
    while True:
        tree = ui()
        if find(tree, rid="free_up_space_completed_title") is not None:
            break
        if time.monotonic() > deadline:
            raise RuntimeError("Photos local cleanup timed out")
        rest(5)
    remaining_files = local_files()
    retained = [r for r in expected if r["phone"] in remaining_files]
    done = find(tree, rid="done_button")
    if done is not None:
        tap(done)
    return retained


def open_database():
    db = sqlite3.connect(STATE / "catalog.sqlite")
    db.row_factory = sqlite3.Row
    db.execute(
        'CREATE TABLE IF NOT EXISTS media(id INTEGER PRIMARY KEY,archive INTEGER,path TEXT,bytes INTEGER,crc INTEGER,metadata TEXT,sha TEXT,phone TEXT,state TEXT DEFAULT "pending",note TEXT,UNIQUE(archive,path))'
    )
    db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT)")
    return db


def metadata_failures(result, destinations):
    """Accept only failures attributed to individual known working copies."""
    failed = set()
    error_lines = [
        line for line in result.stderr.splitlines() if line.startswith("Error:")
    ]
    for line in error_lines:
        normalized = line.replace("\\", "/").casefold()
        matches = [
            path
            for path in destinations
            if normalized.endswith(" - " + path.replace("\\", "/").casefold())
        ]
        if len(matches) != 1:
            raise RuntimeError("Unidentified metadata error: " + line[-500:])
        failed.add(matches[0])
    updated = sum(
        int(n)
        for n in re.findall(r"^\s*(\d+) image files updated\s*$", result.stdout, re.M)
    )
    if updated != len(destinations) - len(failed) or (result.returncode and not failed):
        raise RuntimeError(
            "Metadata write result could not be verified: "
            + result.stderr[-500:]
            + " "
            + result.stdout[-500:]
        )
    return failed


def write_metadata(db, selected, arg_lines):
    destinations = [
        arg_lines[index - 1]
        for index, line in enumerate(arg_lines)
        if line == "-execute"
    ]
    argfile = STATE / "metadata.args"
    argfile.write_text("\n".join(arg_lines) + "\n", encoding="utf-8")
    result = subprocess.run(
        [str(EXIF), "-charset", "filename=UTF8", "-@", str(argfile)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    failed = metadata_failures(result, destinations)
    for row in selected:
        if row["local"] in failed:
            db.execute(
                "UPDATE media SET state='review',note='ExifTool could not safely write capture date; original preserved in ZIP' WHERE id=?",
                (row["id"],),
            )
            pathlib.Path(row["local"]).unlink()
            row["review"] = True
            LOG.warning(
                "Item %d queued for review after metadata write failure.", row["id"]
            )
    db.commit()


def build_catalog(db):
    if db.execute("SELECT value FROM settings WHERE key='catalog_done'").fetchone():
        return
    LOG.info("Indexing configured ZIP archives and Takeout JSON metadata.")
    zs = [zipfile.ZipFile(p) for p in ARCHIVES]
    metadata = {}
    direct = {}
    for z in zs:
        for e in z.infolist():
            if e.filename.endswith(".json"):
                try:
                    obj = json.loads(z.read(e))
                except (ValueError, UnicodeError):
                    continue
                if obj.get("title") and obj.get("photoTakenTime"):
                    folder = str(pathlib.PurePosixPath(e.filename).parent)
                    metadata.setdefault((folder, obj["title"]), []).append(obj)
                    direct[e.filename] = obj
    for part, z in enumerate(zs):
        for e in z.infolist():
            suffix = pathlib.PurePosixPath(e.filename).suffix.lower()
            if suffix not in MEDIA:
                continue
            key = (
                str(pathlib.PurePosixPath(e.filename).parent),
                pathlib.PurePosixPath(e.filename).name,
            )
            matches = metadata.get(key, [])
            exact = direct.get(
                e.filename + ".supplemental-metadata.json"
            ) or direct.get(e.filename + ".json")
            if exact:
                matches = [exact]
            times = {m.get("photoTakenTime", {}).get("timestamp") for m in matches}
            chosen = matches[0] if matches and len(times) == 1 else None
            db.execute(
                "INSERT OR IGNORE INTO media(archive,path,bytes,crc,metadata) VALUES(?,?,?,?,?)",
                (
                    part,
                    e.filename,
                    e.file_size,
                    e.CRC,
                    json.dumps(chosen, ensure_ascii=False) if chosen else None,
                ),
            )
        db.commit()
    for z in zs:
        z.close()
    LOG.info("Checking content duplicates among CRC/size candidates.")
    groups = db.execute(
        "SELECT bytes,crc FROM media GROUP BY bytes,crc HAVING count(*)>1"
    ).fetchall()
    zs = [zipfile.ZipFile(p) for p in ARCHIVES]
    for group in groups:
        hashes = {}
        for r in db.execute(
            "SELECT * FROM media WHERE bytes=? AND crc=? ORDER BY id", tuple(group)
        ).fetchall():
            h = hashlib.sha256()
            with zs[r["archive"]].open(r["path"]) as stream:
                while chunk := stream.read(4 * 1024 * 1024):
                    h.update(chunk)
            digest = h.hexdigest()
            duplicate = hashes.get(digest)
            db.execute(
                "UPDATE media SET sha=?,state=?,note=? WHERE id=?",
                (
                    digest,
                    "duplicate" if duplicate else "pending",
                    f"Identical to item {duplicate}" if duplicate else None,
                    r["id"],
                ),
            )
            hashes.setdefault(digest, r["id"])
        db.commit()
        if stop_requested():
            raise InterruptedError("Stopped during catalog preparation")
    for z in zs:
        z.close()
    db.execute("INSERT OR REPLACE INTO settings VALUES('catalog_done','1')")
    db.commit()
    publish(db, "Catalog ready")


def prepare_batch(db):
    stage = STATE / "staging"
    stage.mkdir(exist_ok=True)
    publish(db, "Preparing next batch")
    # No active manifest means transfer never started. Recover only scratch
    # files whose names were generated by this program, retaining ZIP originals.
    for leftover in stage.iterdir():
        if not leftover.is_file() or not re.fullmatch(
            r"\d{6}_[0-9a-f]{12}\.[a-z0-9]+", leftover.name
        ):
            raise RuntimeError("Unexpected staging file; manual inspection required")
        leftover.unlink()
    db.execute("UPDATE media SET state='pending' WHERE state='prepared'")
    db.commit()
    candidates = db.execute(
        "SELECT * FROM media WHERE state='pending' ORDER BY archive,id"
    ).fetchall()
    selected = []
    total = 0
    zs = [zipfile.ZipFile(p) for p in ARCHIVES]
    try:
        for row in candidates:
            suffix = pathlib.PurePosixPath(row["path"]).suffix.lower()
            limit = 10_000_000_000 if suffix in VIDEO else 200_000_000
            if row["bytes"] > limit:
                db.execute(
                    "UPDATE media SET state='review',note='Exceeds Photos file size limit' WHERE id=?",
                    (row["id"],),
                )
                continue
            if selected and (
                len(selected) >= MAX_FILES or total + row["bytes"] > MAX_BYTES
            ):
                break
            if not selected and row["bytes"] > MAX_BYTES:
                if row["bytes"] > free_bytes() - MIN_FREE:
                    db.execute(
                        "UPDATE media SET state='review',note='Too large for current phone free space' WHERE id=?",
                        (row["id"],),
                    )
                    continue
            # Stream large videos rather than keeping gigabytes in RAM.
            temporary = stage / f'{row["id"]:06d}_000000000000{suffix}'
            with (
                zs[row["archive"]].open(row["path"]) as src,
                temporary.open("wb") as dst,
            ):
                shutil.copyfileobj(src, dst, 4 * 1024 * 1024)
            digest = file_hash(temporary)
            with temporary.open("rb") as src:
                data = src.read(16)
            # Takeout can contain JPEG bytes with a .png filename. Preserve the
            # bytes, but give the working copy its actual format extension.
            if data.startswith(b"\xff\xd8\xff"):
                suffix = ".jpg"
            elif data.startswith(b"\x89PNG\r\n\x1a\n"):
                suffix = ".png"
            elif data.startswith((b"GIF87a", b"GIF89a")):
                suffix = ".gif"
            elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                suffix = ".webp"
            safe = f'{row["id"]:06d}_{digest[:12]}{suffix}'
            local = stage / safe
            temporary.replace(local)
            selected.append(
                {
                    "id": row["id"],
                    "local": str(local),
                    "phone": PHONE + "/" + safe,
                    "originalSha256": digest,
                    "metadata": (
                        json.loads(row["metadata"]) if row["metadata"] else None
                    ),
                }
            )
            total += row["bytes"]
        db.commit()
    finally:
        for z in zs:
            z.close()
    if not selected:
        return []
    # Read embedded dates in one ExifTool process. Add dates only when missing.
    read_args = [
        "-j",
        "-DateTimeOriginal",
        "-CreateDate",
        "-CreationDate",
        "-MediaCreateDate",
        "-TrackCreateDate",
        "-ImageWidth",
        "-ImageHeight",
        *[r["local"] for r in selected],
    ]
    # Avoid the Windows command-line length limit for larger batches.
    read_argfile = STATE / "read-metadata.args"
    read_argfile.write_text("\n".join(read_args) + "\n", encoding="utf-8")
    date_info = {
        pathlib.Path(r["SourceFile"]).name: r
        for r in json.loads(
            command([EXIF, "-charset", "filename=UTF8", "-@", read_argfile], 300)
        )
    }
    arg_lines = []
    for r in selected:
        info = date_info[pathlib.Path(r["local"]).name]
        if info.get("Error") or (
            pathlib.Path(r["local"]).suffix not in VIDEO
            and min(
                int(info.get("ImageWidth", 9999)), int(info.get("ImageHeight", 9999))
            )
            < 256
        ):
            db.execute(
                "UPDATE media SET state='review',note='Invalid media or image smaller than Photos minimum' WHERE id=?",
                (r["id"],),
            )
            pathlib.Path(r["local"]).unlink()
            r["review"] = True
            continue
        existing = any(
            info.get(k) and not str(info[k]).startswith("0000:")
            for k in (
                "DateTimeOriginal",
                "CreateDate",
                "CreationDate",
                "MediaCreateDate",
                "TrackCreateDate",
            )
        )
        meta = r["metadata"]
        stamp = meta.get("photoTakenTime", {}).get("timestamp") if meta else None
        if not existing and stamp:
            value = dt.datetime.fromtimestamp(int(stamp), dt.timezone.utc).strftime(
                "%Y:%m:%d %H:%M:%S"
            )
            ext = pathlib.Path(r["local"]).suffix.lower()
            tags = [f"-FileModifyDate={value}+00:00"]
            if ext in (".mp4", ".mov", ".m4v", ".heic", ".heif"):
                tags += [
                    "-api",
                    "QuickTimeUTC=1",
                    f"-QuickTime:CreateDate={value}",
                    f"-QuickTime:ModifyDate={value}",
                    f"-Keys:CreationDate={value}+00:00",
                ]
            elif ext in (".jpg", ".jpeg", ".png", ".webp", ".dng"):
                tags += [
                    f"-EXIF:DateTimeOriginal={value}",
                    f"-EXIF:CreateDate={value}",
                    "-EXIF:OffsetTimeOriginal=+00:00",
                    "-EXIF:OffsetTimeDigitized=+00:00",
                ]
            else:
                # GIF and unsupported formats retain bytes; mtime supplies fallback.
                os.utime(r["local"], (int(stamp), int(stamp)))
                tags = []
            if tags:
                arg_lines += [
                    "-overwrite_original",
                    "-P",
                    *tags,
                    r["local"],
                    "-execute",
                ]
        elif not existing:
            db.execute(
                "UPDATE media SET state='review',note='No unambiguous capture date' WHERE id=?",
                (r["id"],),
            )
            pathlib.Path(r["local"]).unlink()
            r["review"] = True
    if arg_lines:
        write_metadata(db, selected, arg_lines)
    selected = [r for r in selected if not r.get("review")]
    if not selected:
        db.commit()
        return []  # An empty prepared manifest would prevent the next batch.
    for r in selected:
        r["uploadSha256"] = file_hash(r["local"])
        db.execute(
            "UPDATE media SET sha=?,phone=?,state='prepared' WHERE id=?",
            (r["originalSha256"], r["phone"], r["id"]),
        )
    atomic_json(STATE / "active-batch.json", {"phase": "prepared", "items": selected})
    db.commit()
    return selected


def transfer(db, items):
    if (
        free_bytes()
        < sum(pathlib.Path(r["local"]).stat().st_size for r in items) + MIN_FREE
    ):
        raise RuntimeError("Insufficient free phone space")
    # Stop Photos until each copied file has been verified and indexed.
    adb("shell", "am", "force-stop", "com.google.android.apps.photos")
    publish(db, "Copying to phone", activeFiles=len(items), batteryC=temperature())
    adb("push", str(STATE / "staging") + "/.", PHONE + "/", timeout=1800)
    verify_phone_copies(items)
    register_media(db, items)
    for r in items:
        db.execute("UPDATE media SET state='transferred' WHERE id=?", (r["id"],))
    db.commit()
    atomic_json(STATE / "active-batch.json", {"phase": "transferred", "items": items})
    adb(
        "shell",
        "monkey",
        "-p",
        "com.google.android.apps.photos",
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
    )


def validate_phone_paths(items):
    for row in items:
        if not re.fullmatch(
            re.escape(PHONE) + r"/\d{6}_[0-9a-f]{12}\.[a-z0-9]+", row["phone"]
        ):
            raise RuntimeError("Unexpected phone media path")


def verify_phone_copies(items):
    validate_phone_paths(items)
    for index in range(0, len(items), 50):
        chunk = items[index : index + 50]
        # Verify every active copy; don't repeatedly read unrelated review files.
        output = adb(
            "shell", "sha256sum " + " ".join(row["phone"] for row in chunk), timeout=180
        )
        hashes = {}
        for line in output.splitlines():
            digest, path = line.split(None, 1)
            hashes[path.strip()] = digest
        if any(hashes.get(row["phone"]) != row["uploadSha256"] for row in chunk):
            raise RuntimeError("Phone copy hash mismatch")


def register_media(db, items):
    # Keep the same per-file Android scan, but amortize PC-side ADB startup.
    validate_phone_paths(items)
    for index in range(0, len(items), 20):
        if stop_requested():
            raise InterruptedError("Stop requested; copies retained")
        chunk = items[index : index + 20]
        script = " && ".join(
            "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://"
            + row["phone"]
            + " >/dev/null"
            for row in chunk
        )
        adb("shell", script, timeout=120)
        publish(
            db,
            "Registering phone files",
            registered=index + len(chunk),
            activeFiles=len(items),
            batteryC=temperature(),
        )


def await_backup(db, items):
    deadline = time.monotonic() + 6 * 3600
    streak = 0
    started = time.monotonic()
    unknown_since = None
    status_restarts = 0
    while time.monotonic() < deadline:
        if temperature() >= HOT:
            cool_down(db, HOT)
        complete, remaining, values = backup_view()
        if any(
            "failed" in t.lower() or "not enough storage" in t.lower() for t in values
        ):
            raise RuntimeError("Photos reported backup failure")
        waiting_markers = (
            "getting ready",
            "preparing",
            "waiting for",
            "backup paused",
            "backing up",
        )
        has_status = (
            complete
            or remaining is not None
            or any(
                marker in value.lower()
                for value in values
                for marker in waiting_markers
            )
        )
        if has_status:
            unknown_since = None
        else:
            if unknown_since is None:
                unknown_since = time.monotonic()
            if time.monotonic() - unknown_since >= 120:
                if status_restarts >= 2:
                    raise RuntimeError(
                        "Photos backup status remains missing after two restarts; copies retained"
                    )
                status_restarts += 1
                streak = 0
                unknown_since = None
                LOG.warning(
                    "Photos backup status missing for two minutes; restarting app (%d/2).",
                    status_restarts,
                )
                publish(
                    db,
                    "Recovering missing backup status",
                    activeFiles=len(items),
                    batteryC=temperature(),
                )
                adb("shell", "am", "force-stop", "com.google.android.apps.photos")
                adb(
                    "shell",
                    "monkey",
                    "-p",
                    "com.google.android.apps.photos",
                    "-c",
                    "android.intent.category.LAUNCHER",
                    "1",
                )
                rest(5)
                continue
        # Keep the initial indexing grace period. Once a real batch has finished,
        # recheck promptly; Photos cleanup independently validates each copy.
        streak = (
            streak + 1
            if complete and remaining in (None, 0) and time.monotonic() - started >= 60
            else 0
        )
        phase = "Confirming backup completion" if streak else "Backing up"
        publish(
            db,
            phase,
            activeFiles=len(items),
            remaining=remaining,
            batteryC=temperature(),
            completionChecks=streak,
        )
        if streak == 1:
            LOG.info(
                "Photos reports backup complete; quick recheck before safe cleanup."
            )
        if streak >= 2:
            return
        rest(5 if streak else 10 if remaining is not None and remaining <= 20 else 30)
    raise RuntimeError("Backup timed out; phone copies retained")


def finish_batch(db, items, resuming_cleanup=False):
    retained = []
    remaining_files = local_files() if resuming_cleanup else set()
    if not resuming_cleanup or any(r["phone"] in remaining_files for r in items):
        atomic_json(STATE / "active-batch.json", {"phase": "cleaning", "items": items})
        retained = cleanup(db, items, allow_empty=resuming_cleanup)
    retained_ids = {r["id"] for r in retained}
    if retained:
        LOG.warning(
            "%d files retained by Photos; left on phone and queued for review.",
            len(retained),
        )
    for r in items:
        if r["id"] in retained_ids:
            db.execute(
                "UPDATE media SET state='review',note='Photos retained local copy after safe cleanup; backup not independently confirmed; phone copy left untouched' WHERE id=?",
                (r["id"],),
            )
        else:
            db.execute(
                "UPDATE media SET state='done',note='Removed locally by Photos safe cleanup' WHERE id=?",
                (r["id"],),
            )
    db.commit()
    stage = (STATE / "staging").resolve()
    for r in items:
        local = pathlib.Path(r["local"]).resolve()
        if local.parent != stage:
            raise RuntimeError("Unexpected staging path")
        local.unlink(missing_ok=True)
    (STATE / "active-batch.json").unlink(missing_ok=True)
    publish(db, "Batch complete")


def run(db, max_batches=None):
    build_catalog(db)
    battery = temperature()
    LOG.info("Battery %.1f C; free %.1f GB", battery, free_bytes() / 1e9)
    unlock_phone()
    adb(
        "shell",
        "monkey",
        "-p",
        "com.google.android.apps.photos",
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
    )
    backup_view()
    batches = 0
    while not stop_requested():
        cool_down(db)
        active = STATE / "active-batch.json"
        if active.exists():
            saved = json.loads(active.read_text(encoding="utf-8"))
            items = saved["items"]
            phase = saved["phase"]
        else:
            items = prepare_batch(db)
            phase = "prepared"
        if not items:
            if active.exists():
                if phase != "prepared":
                    raise RuntimeError(
                        "Unexpected empty transferred or cleanup journal"
                    )
                LOG.warning(
                    "Discarding empty prepared batch journal; advancing to pending files."
                )
                active.unlink()
            pending = db.execute(
                "SELECT count(*) FROM media WHERE state='pending'"
            ).fetchone()[0]
            if pending:
                continue
            review = [
                dict(r)
                for r in db.execute(
                    "SELECT id,path,note FROM media WHERE state='review'"
                )
            ]
            atomic_json(ROOT / "files-to-review.json", review)
            publish(
                db,
                "Finished" if not review else "Finished; review items remain",
                reviewFiles=len(review),
            )
            return
        LOG.info("Batch %s: %d files", batches + 1, len(items))
        if phase == "prepared":
            transfer(db, items)
        if phase != "cleaning":
            await_backup(db, items)
        finish_batch(db, items, resuming_cleanup=phase == "cleaning")
        batches += 1
        if max_batches and batches >= max_batches:
            publish(db, "Validation batch finished")
            return
        rest(5)
    raise InterruptedError("Stop requested")


def configure(config_path):
    global ADB, EXIF, ARCHIVES, ACCOUNT, SERIAL, PHONE
    global MAX_FILES, MAX_BYTES, MIN_FREE, HOT, COOL, START_MAX
    path = pathlib.Path(config_path).resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    ACCOUNT = config["account"]
    SERIAL = config["serial"]
    if (
        not isinstance(ACCOUNT, str)
        or "@" not in ACCOUNT
        or ACCOUNT == "example@gmail.com"
    ):
        raise ValueError("Set your actual Google Photos account in config.json")
    if (
        not isinstance(SERIAL, str)
        or not re.fullmatch(r"[A-Za-z0-9._:-]+", SERIAL)
        or SERIAL == "YOUR_PIXEL_SERIAL"
    ):
        raise ValueError("Set the authorized Pixel serial from adb devices")

    def local(value):
        candidate = pathlib.Path(value).expanduser()
        return candidate if candidate.is_absolute() else path.parent / candidate

    archives = config["archives"]
    if not isinstance(archives, list) or not archives:
        raise ValueError("Configure at least one Takeout ZIP")
    ARCHIVES = [local(value).resolve() for value in archives]
    if len(set(ARCHIVES)) != len(ARCHIVES):
        raise ValueError("Archive paths must be unique")
    for archive in ARCHIVES:
        if archive.suffix.lower() != ".zip" or not archive.is_file():
            raise ValueError("A configured Takeout ZIP does not exist")
    PHONE = config.get("phone_folder", PHONE)
    if not isinstance(PHONE, str) or not re.fullmatch(
        r"/storage/emulated/0/Pictures/[A-Za-z0-9_-]+", PHONE
    ):
        raise ValueError("phone_folder must be a simple dedicated Pictures subfolder")
    tools = config.get("tools", {})

    def tool(value):
        return local(value) if "/" in value or "\\" in value else pathlib.Path(value)

    ADB = tool(tools.get("adb", "adb"))
    EXIF = tool(tools.get("exiftool", "exiftool"))
    batch = config.get("batch", {})
    MAX_FILES = batch.get("max_files", 200)
    MAX_BYTES = batch.get("max_bytes", 500_000_000)
    MIN_FREE = batch.get("min_free_bytes", 5_000_000_000)
    if any(
        type(value) is not int or value <= 0
        for value in (MAX_FILES, MAX_BYTES, MIN_FREE)
    ):
        raise ValueError("Batch sizes and free-space reserve must be positive integers")
    if MAX_FILES > 400 or MAX_BYTES > 1_000_000_000 or MIN_FREE < 5_000_000_000:
        raise ValueError(
            "Supported limits: up to 400 files / 1 GB and at least 5 GB reserved"
        )
    temperatures = config.get("temperature", {})
    COOL = temperatures.get("resume_c", 40)
    START_MAX = temperatures.get("new_batch_c", 41)
    HOT = temperatures.get("pause_c", 43)
    if (
        any(type(value) not in (int, float) for value in (COOL, START_MAX, HOT))
        or not 0 < COOL < START_MAX <= HOT <= 43
    ):
        raise ValueError(
            "Temperatures must satisfy 0 < resume < new_batch <= pause <= 43 C"
        )
    binding = {
        "account": ACCOUNT,
        "serial": SERIAL,
        "phone_folder": PHONE,
        "archives": [
            {
                "path": str(archive),
                "size": archive.stat().st_size,
                "mtime_ns": archive.stat().st_mtime_ns,
            }
            for archive in ARCHIVES
        ],
    }
    STATE.mkdir(parents=True, exist_ok=True)
    bound = STATE / "session-binding.json"
    if bound.exists() and json.loads(bound.read_text(encoding="utf-8")) != binding:
        raise ValueError(
            "This saved session belongs to different archives/account/device/folder. Use a separate checkout."
        )
    return binding


def verify_device():
    device = adb("shell", "getprop", "ro.product.device").strip()
    if device not in ("marlin", "sailfish"):
        raise RuntimeError(
            "This project requires an original 2016 Pixel or Pixel XL; connected device is unsupported"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Batch Google Takeout backups through an original Pixel (Windows only)."
    )
    parser.add_argument(
        "--config", type=pathlib.Path, default=PROJECT_ROOT / "config.json"
    )
    parser.add_argument("--catalog-only", action="store_true")
    parser.add_argument(
        "--max-batches", type=int, help="Stop after this many completed batches"
    )
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This release supports Windows only")
    if args.max_batches is not None and args.max_batches < 1:
        parser.error("--max-batches must be positive")
    try:
        binding = configure(args.config)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    STATE.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(STATE / "run.log", encoding="utf-8"),
        ],
    )
    lock = STATE / "running.lock"
    # File locking is released by Windows on process exit, including crashes.
    import msvcrt

    handle = lock.open("a+b")
    handle.seek(0)
    handle.write(b"0")
    handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        raise SystemExit("Another uploader is already running")
    try:
        bound = STATE / "session-binding.json"
        if bound.exists() and json.loads(bound.read_text(encoding="utf-8")) != binding:
            raise ValueError("Saved session identity changed while acquiring the lock")
        atomic_json(bound, binding)
    except (OSError, ValueError) as error:
        handle.close()
        parser.error(str(error))
    (ROOT / "STOP.txt").unlink(missing_ok=True)
    db = open_database()
    screen = {}
    stay_awake = None
    import ctypes

    ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    try:
        if not args.catalog_only:
            verify_device()
            for key in (
                "screen_brightness",
                "screen_brightness_mode",
                "screen_off_timeout",
                "accelerometer_rotation",
                "user_rotation",
            ):
                screen[key] = adb("shell", "settings", "get", "system", key).strip()
            adb("shell", "settings", "put", "system", "screen_brightness_mode", "0")
            adb("shell", "settings", "put", "system", "screen_brightness", "5")
            adb(
                "shell", "settings", "put", "system", "screen_off_timeout", "2147483647"
            )
            adb("shell", "settings", "put", "system", "user_rotation", "0")
            adb("shell", "settings", "put", "system", "accelerometer_rotation", "0")
            stay_awake = adb(
                "shell", "settings", "get", "global", "stay_on_while_plugged_in"
            ).strip()
            adb("shell", "svc", "power", "stayon", "usb")
        if args.catalog_only:
            build_catalog(db)
        else:
            publish(db, "Checking phone and resuming")
            run(db, args.max_batches)
    except (Exception, KeyboardInterrupt) as error:
        LOG.error("%s", error)
        publish(db, "Stopped", error=str(error))
        return 1
    finally:
        for key, value in screen.items():
            try:
                adb("shell", "settings", "put", "system", key, value)
            except Exception:
                pass
        if screen:
            try:
                adb(
                    "shell",
                    "settings",
                    "put",
                    "global",
                    "stay_on_while_plugged_in",
                    stay_awake or "0",
                )
            except Exception:
                pass
        db.close()
        handle.close()
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    return 0


if __name__ == "__main__":
    sys.exit(main())

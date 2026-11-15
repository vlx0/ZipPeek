"""Regression tests using real archives; verification must never extract them."""

import hashlib
import pathlib
import struct
import tempfile
import threading
import unittest
import warnings
import zipfile

from zippeek.archive import ArchiveChanged, inspect_archive, verify


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.path = self.root / "fixture.zip"

    def make_zip(self, entries, compression=zipfile.ZIP_STORED):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.path, "w", compression=compression) as zf:
                for name, data in entries:
                    zf.writestr(name, data)
        return self.path

    def snapshot(self):
        return {
            str(p.relative_to(self.root)): (
                "dir" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
            )
            for p in self.root.rglob("*")
        }

    def inspect(self):
        before = self.snapshot()
        try:
            return inspect_archive(self.path)
        finally:
            self.assertEqual(self.snapshot(), before, "inspection modified the fixture")

    def check(self, archive, **kwargs):
        before = self.snapshot()
        try:
            return verify(archive, kwargs.pop("stop", threading.Event()), **kwargs)
        finally:
            self.assertEqual(
                self.snapshot(), before, "verification changed files or extracted content"
            )

    def test_inspection_preserves_member_metadata_and_order(self):
        self.make_zip([("folder/", b""), ("folder/hello.txt", b"hello"), ("empty", b"")])
        archive = self.inspect()
        self.assertEqual(pathlib.Path(archive.path), self.path)
        self.assertIsNotNone(archive.signature)
        self.assertIsInstance(archive.members, tuple)
        self.assertEqual([m.index for m in archive.members], [0, 1, 2])
        self.assertEqual([m.name for m in archive.members], ["folder/", "folder/hello.txt", "empty"])
        self.assertEqual([m.size for m in archive.members], [0, 5, 0])
        self.assertEqual([m.packed for m in archive.members], [0, 5, 0])
        self.assertEqual([m.is_dir for m in archive.members], [True, False, False])
        self.assertTrue(all(not m.encrypted for m in archive.members))
        self.assertTrue(all(m.method == zipfile.ZIP_STORED for m in archive.members))

    def test_valid_archive_checks_all_files_without_extracting(self):
        self.make_zip([("nested/file.txt", b"abc"), ("other.bin", b"12345"), ("zero", b"")])
        result = self.check(self.inspect())
        self.assertEqual(result.statuses, {0: "ok", 1: "ok", 2: "ok"})
        self.assertEqual(result.read_bytes, 8)
        self.assertEqual(result.errors, {})
        self.assertTrue(result.complete)
        self.assertFalse(result.cancelled)
        self.assertFalse(result.limited)

    def test_deflated_data_counts_uncompressed_bytes(self):
        data = b"compressible" * 10000
        self.make_zip([("data", data)], zipfile.ZIP_DEFLATED)
        archive = self.inspect()
        self.assertLess(archive.members[0].packed, len(data))
        result = self.check(archive, max_bytes=None)
        self.assertEqual(result.statuses, {0: "ok"})
        self.assertEqual(result.read_bytes, len(data))
        self.assertTrue(result.complete)

    def test_empty_archive_is_complete(self):
        self.make_zip([])
        archive = self.inspect()
        self.assertEqual(archive.members, ())
        result = self.check(archive)
        self.assertEqual(result.statuses, {})
        self.assertEqual(result.errors, {})
        self.assertEqual(result.read_bytes, 0)
        self.assertTrue(result.complete)
        self.assertFalse(result.cancelled)
        self.assertFalse(result.limited)

    def test_invalid_archive_is_rejected(self):
        self.path.write_bytes(b"This is not a ZIP archive.")
        with self.assertRaises(zipfile.BadZipFile):
            self.inspect()

    def test_truncated_central_directory_is_rejected(self):
        self.make_zip([("hello", b"payload")])
        raw = self.path.read_bytes()
        central = raw.index(b"PK\x01\x02")
        self.path.write_bytes(raw[:central + 12])
        with self.assertRaises(zipfile.BadZipFile):
            self.inspect()

    def corrupt_payload(self, member_index):
        with zipfile.ZipFile(self.path) as zf:
            info = zf.infolist()[member_index]
        raw = bytearray(self.path.read_bytes())
        name_len, extra_len = struct.unpack_from("<HH", raw, info.header_offset + 26)
        payload = info.header_offset + 30 + name_len + extra_len
        raw[payload] ^= 0xFF
        self.path.write_bytes(raw)

    def test_corrupt_stored_payload_reports_crc_error_and_continues(self):
        self.make_zip([("broken", b"broken data"), ("good", b"healthy")])
        self.corrupt_payload(0)
        result = self.check(self.inspect())
        self.assertEqual(result.statuses, {0: "error", 1: "ok"})
        self.assertEqual(set(result.errors), {0})
        self.assertTrue(result.errors[0])
        self.assertFalse(result.cancelled)
        self.assertFalse(result.limited)

    def test_duplicate_names_are_checked_by_zipinfo_not_name(self):
        self.make_zip([("same.txt", b"first payload"), ("same.txt", b"second payload")])
        self.corrupt_payload(0)
        archive = self.inspect()
        self.assertEqual([m.name for m in archive.members], ["same.txt", "same.txt"])
        result = self.check(archive)
        # ZipFile.open("same.txt") selects the last entry, hiding the first CRC failure.
        self.assertEqual(result.statuses, {0: "error", 1: "ok"})
        self.assertEqual(set(result.errors), {0})

    def test_valid_duplicate_names_read_each_entry_once(self):
        self.make_zip([("same", b"first"), ("same", b"a longer second entry")])
        result = self.check(self.inspect())
        self.assertEqual(result.statuses, {0: "ok", 1: "ok"})
        self.assertEqual(result.read_bytes, len(b"first") + len(b"a longer second entry"))
        self.assertEqual(result.errors, {})
        self.assertTrue(result.complete)

    def test_preexisting_cancellation_leaves_members_unchecked(self):
        self.make_zip([("one", b"123"), ("two", b"456")])
        stop = threading.Event()
        stop.set()
        result = self.check(self.inspect(), stop=stop)
        self.assertEqual(result.statuses, {})
        self.assertEqual(result.errors, {})
        self.assertEqual(result.read_bytes, 0)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.complete)
        self.assertFalse(result.limited)

    def test_progress_can_cancel_before_all_members_are_checked(self):
        self.make_zip([("one", b"a" * 200000), ("two", b"b" * 200000)])
        stop = threading.Event()
        calls = []

        def progress(index, read_bytes):
            calls.append((index, read_bytes))
            stop.set()

        result = self.check(self.inspect(), stop=stop, progress=progress)
        self.assertTrue(calls, "verification never reported progress")
        self.assertTrue(all(isinstance(i, int) and isinstance(n, int) for i, n in calls))
        self.assertTrue(all(i in (0, 1) and n >= 0 for i, n in calls))
        self.assertEqual([n for _, n in calls], sorted(n for _, n in calls))
        self.assertTrue(result.cancelled)
        self.assertFalse(result.complete)
        self.assertFalse(result.limited)
        self.assertNotIn(1, result.statuses)

    def test_total_byte_limit_is_shared_between_members(self):
        self.make_zip([("one", b"a" * 80), ("two", b"b" * 80), ("three", b"c" * 80)])
        result = self.check(self.inspect(), max_bytes=100)
        self.assertTrue(result.limited)
        self.assertFalse(result.complete)
        self.assertFalse(result.cancelled)
        self.assertLessEqual(result.read_bytes, 100)
        self.assertNotIn(2, result.statuses)
        self.assertNotEqual(result.statuses.get(1), "ok")
        self.assertEqual(result.errors, {})

    def test_none_byte_limit_checks_every_member(self):
        self.make_zip([("one", b"a" * 80), ("two", b"b" * 80)])
        result = self.check(self.inspect(), max_bytes=None)
        self.assertEqual(result.statuses, {0: "ok", 1: "ok"})
        self.assertEqual(result.read_bytes, 160)
        self.assertTrue(result.complete)
        self.assertFalse(result.limited)

    def test_exact_byte_limit_allows_completion(self):
        self.make_zip([("one", b"abc"), ("two", b"defgh")])
        result = self.check(self.inspect(), max_bytes=8)
        self.assertEqual(result.statuses, {0: "ok", 1: "ok"})
        self.assertEqual(result.read_bytes, 8)
        self.assertTrue(result.complete)
        self.assertFalse(result.limited)

    def test_zero_byte_limit_leaves_nonempty_member_unchecked(self):
        self.make_zip([("data", b"payload")])
        result = self.check(self.inspect(), max_bytes=0)
        self.assertEqual(result.statuses, {})
        self.assertEqual(result.read_bytes, 0)
        self.assertEqual(result.errors, {})
        self.assertTrue(result.limited)
        self.assertFalse(result.complete)
        self.assertFalse(result.cancelled)

    def test_changed_signature_raises_before_verification(self):
        self.make_zip([("original", b"original")])
        archive = self.inspect()
        self.make_zip([("replacement", b"a clearly different size")])
        self.assertNotEqual(self.inspect().signature, archive.signature)
        with self.assertRaises(ArchiveChanged):
            self.check(archive)


if __name__ == "__main__":
    unittest.main()

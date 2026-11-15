"""Real compressed-stream regressions; fixtures stay in temporary directories."""
import pathlib
import struct
import tempfile
import threading
import unittest
import zipfile
import zlib

from zippeek.archive import inspect_archive, verify


METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED,
           zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA)
if hasattr(zipfile, 'ZIP_ZSTANDARD') and getattr(zipfile, 'zstd', None) is not None:
    METHODS += (zipfile.ZIP_ZSTANDARD,)
COMPRESSED = METHODS[1:]


class StreamTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = pathlib.Path(temporary.name, 'stream.zip')

    def make_archive(self, method, entries):
        with zipfile.ZipFile(self.path, 'w', compression=method) as archive:
            for name, payload in entries:
                archive.writestr(name, payload)

    def check(self, **kwargs):
        before = self.path.read_bytes()
        try:
            return verify(inspect_archive(self.path),
                          kwargs.pop('stop', threading.Event()), **kwargs)
        finally:
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def assert_corrupt(self, result):
        self.assertEqual(result.statuses, {0: 'error'})
        self.assertTrue(result.errors.get(0))
        self.assertFalse(result.complete)
        self.assertFalse(result.cancelled)

    def test_understated_size_and_prefix_crc_are_corrupt(self):
        payload = b'abcdef' * 100
        for method in COMPRESSED:
            for cap in (None, 1):
                with self.subTest(method=method, cap=cap):
                    self.make_archive(method, [('x', payload)])
                    raw = bytearray(self.path.read_bytes())
                    central = raw.index(b'PK\x01\x02')
                    # Both headers agree, but the real codec output is 600 bytes.
                    for offset in (22, central + 24):
                        struct.pack_into('<I', raw, offset, 1)
                    for offset in (14, central + 16):
                        struct.pack_into('<I', raw, offset, zlib.crc32(payload[:1]))
                    self.path.write_bytes(raw)
                    # Demonstrate why ZipExtFile-based length/CRC checks miss this.
                    with zipfile.ZipFile(self.path) as archive:
                        self.assertEqual(archive.read('x'), payload[:1])
                    self.assert_corrupt(self.check(max_bytes=cap))

    def test_deflate_missing_terminator_is_corrupt(self):
        payload = b'abcdef' * 100
        self.make_archive(zipfile.ZIP_DEFLATED, [('x', payload)])
        raw = bytearray(self.path.read_bytes())
        central = raw.index(b'PK\x01\x02')
        packed = struct.unpack_from('<I', raw, central + 20)[0]
        name_length, extra_length = struct.unpack_from('<HH', raw, 26)
        start = 30 + name_length + extra_length
        decoder = zlib.decompressobj(-15)
        self.assertEqual(decoder.decompress(raw[start:start + packed]), payload)
        self.assertTrue(decoder.eof)
        # Remove a real compressed byte, keeping all ZIP offsets consistent.
        del raw[start + packed - 1]
        central -= 1
        for offset in (18, central + 20):
            struct.pack_into('<I', raw, offset, packed - 1)
        eocd = raw.index(b'PK\x05\x06')
        struct.pack_into('<I', raw, eocd + 16, central)
        decoder = zlib.decompressobj(-15)
        self.assertEqual(decoder.decompress(raw[start:start + packed - 1]), payload)
        self.assertFalse(decoder.eof)
        self.path.write_bytes(raw)
        for cap in (None, len(payload)):
            with self.subTest(cap=cap):
                self.assert_corrupt(self.check(max_bytes=cap))

    def test_valid_methods_empty_and_nonempty_exact_or_unlimited_cap(self):
        entries = [('empty-first', b''), ('data', b'abcdef' * 100),
                   ('other', b'xyz'), ('empty-last', b'')]
        for method in METHODS:
            for cap in (None, 603):
                with self.subTest(method=method, cap=cap):
                    self.make_archive(method, entries)
                    result = self.check(max_bytes=cap)
                    self.assertEqual(result.statuses, dict.fromkeys(range(4), 'ok'))
                    self.assertEqual(result.read_bytes, 603)
                    self.assertEqual(result.errors, {})
                    self.assertTrue(result.complete)
                    self.assertFalse(result.limited)
                    self.assertFalse(result.cancelled)

    def test_low_cap_is_shared_across_valid_members(self):
        for method in METHODS:
            with self.subTest(method=method):
                self.make_archive(method, [('one', b'a' * 80), ('two', b'b' * 80)])
                result = self.check(max_bytes=100)
                self.assertEqual(result.statuses, {0: 'ok'})
                self.assertEqual(result.read_bytes, 80)
                self.assertEqual(result.errors, {})
                self.assertTrue(result.limited)
                self.assertFalse(result.complete)
                self.assertFalse(result.cancelled)

    def test_large_valid_stream_exact_cap_and_midstream_cancellation(self):
        payload = b'abcdef01' * (3 * 1024 * 1024 // 8)
        for method in METHODS:
            with self.subTest(method=method):
                self.make_archive(method, [('large', payload), ('after', b'')])
                result = self.check(max_bytes=len(payload))
                self.assertEqual(result.statuses, {0: 'ok', 1: 'ok'})
                self.assertEqual(result.read_bytes, len(payload))
                self.assertTrue(result.complete)
                self.assertFalse(result.limited)
                self.assertEqual(result.errors, {})

                stop = threading.Event()
                calls = []

                def progress(index, count):
                    calls.append((index, count))
                    if count >= 1024 * 1024:
                        stop.set()

                result = self.check(stop=stop, progress=progress, max_bytes=len(payload))
                self.assertTrue(calls)
                self.assertTrue(stop.is_set())
                self.assertTrue(result.cancelled)
                self.assertFalse(result.complete)
                self.assertFalse(result.limited)
                self.assertEqual(result.errors, {})
                self.assertNotIn(0, result.statuses)
                self.assertNotIn(1, result.statuses)
                self.assertGreaterEqual(result.read_bytes, 1024 * 1024)
                self.assertLess(result.read_bytes, len(payload))
                self.assertEqual([n for _, n in calls], sorted(n for _, n in calls))


if __name__ == '__main__':
    unittest.main()

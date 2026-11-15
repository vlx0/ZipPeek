from pathlib import Path
import struct
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch

from zippeek.archive import inspect_archive, verify


class EdgeTests(unittest.TestCase):
    def test_crc_failure_reserves_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'broken.zip')
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('bad', b'a'*80)
                archive.writestr('good', b'b'*80)
            data = bytearray(path.read_bytes())
            data[30+len('bad')] ^= 255
            path.write_bytes(data)
            result = verify(inspect_archive(path), threading.Event(), max_bytes=100)
            self.assertEqual(result.statuses, {0:'error'})
            self.assertTrue(result.limited)

    def test_missing_codec_is_not_corruption(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'deflated.zip')
            with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('file', b'payload')
            with patch.object(zipfile, 'zlib', None):
                result = verify(inspect_archive(path), threading.Event())
            self.assertEqual(result.statuses, {0:'unsupported'})
            self.assertEqual(result.errors, {})
            self.assertFalse(result.complete)

    def test_encrypted_and_unsupported_do_not_pass(self):
        for state in ('encrypted', 'unsupported'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as folder:
                path = Path(folder, 'special.zip')
                with zipfile.ZipFile(path, 'w') as archive:
                    archive.writestr('file.txt', b'payload')
                data = bytearray(path.read_bytes())
                central = data.index(b'PK\x01\x02')
                if state == 'encrypted':
                    struct.pack_into('<H', data, 6, 1)
                    struct.pack_into('<H', data, central+8, 1)
                else:
                    struct.pack_into('<H', data, 8, 99)
                    struct.pack_into('<H', data, central+10, 99)
                path.write_bytes(data)
                result = verify(inspect_archive(path), threading.Event())
                self.assertEqual(result.statuses, {0:state})
                self.assertFalse(result.complete)

    def test_cancel_empty_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'empty.zip')
            with zipfile.ZipFile(path, 'w'):
                pass
            archive = inspect_archive(path)
            stop = threading.Event()
            stop.set()
            self.assertTrue(verify(archive, stop).cancelled)
            with self.assertRaises(InterruptedError):
                inspect_archive(path, stop)

    def test_zero_size_bad_crc_at_zero_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'empty-member.zip')
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('empty', b'')
            data = bytearray(path.read_bytes())
            struct.pack_into('<I', data, 14, 123)
            struct.pack_into('<I', data, data.index(b'PK\x01\x02') + 16, 123)
            path.write_bytes(data)
            result = verify(inspect_archive(path), threading.Event(), max_bytes=0)
            self.assertEqual(result.statuses, {0:'error'})
            self.assertFalse(result.complete)

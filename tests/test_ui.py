from pathlib import Path
import tempfile
import time
import unittest
import zipfile

from zippeek.ui import App


class UiTests(unittest.TestCase):
    def setUp(self):
        self.app = App()
        self.app.withdraw()

    def tearDown(self):
        for callback in self.app.tk.call('after', 'info'):
            self.app.after_cancel(callback)
        self.app.destroy()

    def settle(self):
        deadline = time.monotonic() + 5
        while self.app.busy and time.monotonic() < deadline:
            self.app.update()
        self.app.update()
        self.assertFalse(self.app.busy, 'worker did not complete')

    def test_cancelled_listing_is_not_corrupt_archive(self):
        def operation():
            raise InterruptedError('cancelled')
        self.app.work('loaded', operation)
        self.settle()
        self.assertIn('остановлено', self.app.status.get())
        self.assertNotIn('Ошибка', self.app.status.get())
        self.assertIsNone(self.app.archive)

    def test_load_check_filter_and_invalidate(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'fixture.zip')
            with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as source:
                source.writestr('folder/one.txt', b'one')
                source.writestr('folder/two.txt', b'two')
            a = self.app
            a.path.set(str(path))
            a.load()
            self.settle()
            self.assertEqual(a.total.get(), '2')
            self.assertIsNone(a.check)
            self.assertEqual(len(a.shown), 2)
            a.start_check()
            self.settle()
            self.assertTrue(a.check.complete)
            self.assertEqual(a.tree.item('0', 'values')[-1], 'Целостность OK')
            a.query.set('one.txt')
            a.render()
            self.assertEqual(list(a.shown), ['0'])
            a.path.set(str(Path(folder, 'missing.zip')))
            self.assertIsNone(a.archive)
            self.assertEqual(a.shown, {})
            self.assertEqual(str(a.check_button['state']), 'disabled')

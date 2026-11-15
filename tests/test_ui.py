from pathlib import Path
import tempfile
import time
import unittest
import zipfile
import json
import tkinter as tk
from unittest.mock import patch

from zippeek.ui import App
from zippeek.archive import Archive, Member, Check


class CloseTests(unittest.TestCase):
    def test_corrupt_unicode_setting_does_not_prevent_closing(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder, 'settings.json')
            settings.write_text(json.dumps({'path':'\ud800'}), encoding='utf-8')
            with patch('zippeek.ui.SETTINGS', settings):
                app = App()
                app.withdraw()
                try:
                    app.close()
                    self.assertEqual(json.loads(settings.read_text(encoding='utf-8'))['path'], '\ud800')
                    self.assertEqual(app.tk.call('info', 'commands', '.'), '')
                    self.assertFalse(app.tk.call('after', 'info'))
                finally:
                    if app.tk.call('info', 'commands', '.'):
                        app.destroy()


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

    def test_report_handles_busy_clipboard(self):
        a = self.app
        a.archive = Archive('fixture.zip', (0,0,100,0), ())
        with patch.object(a, 'clipboard_clear', side_effect=tk.TclError('clipboard busy')):
            a.copy_report()
        self.assertIn('Не удалось скопировать', a.detail.get())
        self.assertIsNotNone(a.archive)

    def test_deselection_clears_old_record_details(self):
        a = self.app
        a.archive = Archive('fixture.zip', (0,0,100,0), (Member(0,'name.txt',3,3,False,False,0),))
        a.render()
        a.tree.selection_set('0')
        a.select()
        self.assertIn('name.txt', a.detail.get())
        a.tree.selection_remove('0')
        a.select()
        self.assertNotIn('name.txt', a.detail.get())

    def test_switching_filters_cancels_pending_table_batches(self):
        a = self.app
        a.archive = Archive('fixture.zip', (0,0,100,0), tuple(
            Member(index, f'{index}.txt',3,3,False,False,0) for index in range(600)))
        a.render()
        self.assertIsNotNone(a.insert_job)
        a.query.set('599.txt')
        a.render()
        a.update()
        self.assertEqual(list(a.shown), ['599'])
        self.assertEqual(a.tree.get_children(), ('599',))

    def test_attention_keeps_errors_password_and_unchecked(self):
        a = self.app
        a.archive = Archive('fixture.zip', (0,0,100,0), tuple(
            Member(i, name, 3, 3, False, i==2, 0)
            for i,name in enumerate(['ok.txt','bad.txt','locked.txt','pending.txt'])))
        a.check = Check(statuses={0:'ok',1:'error',2:'encrypted'}, errors={1:'CRC failed'})
        a.attention.set(True)
        a.render()
        self.assertEqual(list(a.shown), ['1','2','3'])
        a.query.set('BAD')
        a.render()
        self.assertEqual(list(a.shown), ['1'])

    def test_report_preserves_partial_status_and_escapes_names(self):
        a = self.app
        a.archive = Archive('fixture.zip', (0,0,100,0), (
            Member(0, 'good.txt', 3,3,False,False,0),
            Member(1, 'bad\nname.txt', 3,3,False,False,0)))
        a.check = Check(statuses={0:'ok'}, cancelled=True)
        a.attention.set(True)
        text = a.report_text()
        self.assertIn('Остановлено', text)
        self.assertIn('в отчёте: 1', text)
        self.assertIn('Не проверено', text)
        self.assertIn('bad\\nname.txt', text)
        self.assertNotIn('good.txt', text)
        with patch.object(a, 'clipboard_clear') as clear, patch.object(a, 'clipboard_append') as append:
            a.copy_report()
            clear.assert_called_once()
            append.assert_called_once_with(text)

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

"""Tests for atomic, per-dataset publishing in writer and exporter."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.utils import writer, atomic_write
from plugins.exporter import exporter


class WriterTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='photon-writer-')

    def tearDown(self):
        for name in os.listdir(self.tmp):
            os.unlink(os.path.join(self.tmp, name))
        os.rmdir(self.tmp)

    def test_each_dataset_gets_its_own_file_and_content(self):
        datasets = [
            {'a', 'b'},
            {'x@example.com'},
        ]
        writer(datasets, ['internal', 'intel'], self.tmp)
        self.assertEqual(
            sorted(os.listdir(self.tmp)), ['intel.txt', 'internal.txt'])
        with open(os.path.join(self.tmp, 'internal.txt')) as fh:
            lines = sorted(fh.read().split())
        self.assertEqual(lines, ['a', 'b'])
        with open(os.path.join(self.tmp, 'intel.txt')) as fh:
            self.assertEqual(fh.read().strip(), 'x@example.com')

    def test_publish_is_atomic_and_keeps_previous_content_on_failure(self):
        target = os.path.join(self.tmp, 'internal.txt')
        atomic_write(target, 'previous\n')

        def fail_replace(src, dst):
            raise OSError('publish interrupted')

        with mock.patch('core.utils.os.replace', side_effect=fail_replace):
            with self.assertRaises(OSError):
                atomic_write(target, 'half-written\n')
        # Destination untouched
        with open(target) as fh:
            self.assertEqual(fh.read(), 'previous\n')
        # Temp file cleaned up
        leftovers = [n for n in os.listdir(self.tmp)
                     if n.startswith('.internal.txt.')]
        self.assertEqual(leftovers, [])

    def test_datasets_do_not_overwrite_one_another(self):
        # Re-writing one collection must not touch the other collection's file
        writer([{'one'}], ['internal'], self.tmp)
        writer([{'two'}], ['failed'], self.tmp)
        with open(os.path.join(self.tmp, 'internal.txt')) as fh:
            self.assertEqual(fh.read().strip(), 'one')
        with open(os.path.join(self.tmp, 'failed.txt')) as fh:
            self.assertEqual(fh.read().strip(), 'two')
        # No stray temp files after successful runs
        self.assertFalse(
            any(n.startswith('.') for n in os.listdir(self.tmp)))


class ExporterTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='photon-export-')
        self.datasets = {
            'files': [],
            'intel': ['x@example.com'],
            'internal': ['http://example.com/a', 'http://example.com/b'],
            'failed': [],
        }

    def tearDown(self):
        for name in os.listdir(self.tmp):
            os.unlink(os.path.join(self.tmp, name))
        os.rmdir(self.tmp)

    def test_json_export_roundtrip_and_does_not_touch_txt(self):
        writer([set(self.datasets['internal'])], ['internal'], self.tmp)
        exporter(self.tmp, 'json', self.datasets)
        json_path = os.path.join(self.tmp, 'exported.json')
        with open(json_path) as fh:
            loaded = json.load(fh)
        # Sets have no guaranteed order; compare as sets / per-key
        self.assertEqual(set(loaded), set(self.datasets))
        self.assertEqual(set(loaded['internal']),
                         set(self.datasets['internal']))
        # The per-dataset txt file remains intact (order is set-based)
        with open(os.path.join(self.tmp, 'internal.txt')) as fh:
            self.assertEqual(
                set(fh.read().split()),
                {'http://example.com/a', 'http://example.com/b'})

    def test_csv_export_rows_and_distinct_from_json(self):
        exporter(self.tmp, 'csv', self.datasets)
        csv_path = os.path.join(self.tmp, 'exported.csv')
        with open(csv_path) as fh:
            content = fh.read()
        self.assertIn('internal,http://example.com/a,http://example.com/b',
                      content)
        self.assertIn('intel,x@example.com', content)
        # CSV and JSON use separate files; a later CSV run must not clobber
        # an existing JSON export
        exporter(self.tmp, 'json', self.datasets)
        self.assertTrue(os.path.exists(csv_path))
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, 'exported.json')))

    def test_export_failure_leaves_no_temp_and_old_file(self):
        target = os.path.join(self.tmp, 'exported.json')
        atomic_write(target, '{"old": true}')
        with mock.patch('core.utils.os.replace', side_effect=OSError('boom')):
            with self.assertRaises(OSError):
                exporter(self.tmp, 'json', self.datasets)
        with open(target) as fh:
            self.assertEqual(json.load(fh), {'old': True})
        self.assertFalse(
            any(n.endswith('.tmp') for n in os.listdir(self.tmp)))

    def test_csv_handles_non_string_values(self):
        exporter(self.tmp, 'csv', {'counts': [1, 2, 3], 'empty': None})
        with open(os.path.join(self.tmp, 'exported.csv')) as fh:
            rows = [line.rstrip('\n') for line in fh]
        self.assertIn('counts,1,2,3', rows)
        self.assertIn('empty', rows)


if __name__ == '__main__':
    unittest.main()

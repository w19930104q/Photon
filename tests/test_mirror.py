"""Regression tests for core.mirror (clone) behavior."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.mirror import mirror


class MirrorTests(unittest.TestCase):

    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix='photon-mirror-')
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)
        for root, _, files in os.walk(self.tmp):
            for name in files:
                os.unlink(os.path.join(root, name))
        for root, dirs, _ in os.walk(self.tmp, topdown=False):
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(self.tmp)

    def _read(self, *parts):
        path = os.path.join(self.tmp, *parts)
        with open(path, 'rb') as fh:
            return fh.read().decode('utf-8')

    def test_same_name_query_pages_do_not_overwrite(self):
        mirror('http://example.com/page?id=1', 'body one')
        mirror('http://example.com/page?id=2', 'body two')
        prefix = 'example.com_mirror'
        self.assertEqual(
            self._read(prefix, 'page.html?id=1'), 'body one')
        self.assertEqual(
            self._read(prefix, 'page.html?id=2'), 'body two')

    def test_root_becomes_index_html(self):
        mirror('http://example.com', 'root body')
        self.assertEqual(
            self._read('example.com_mirror', 'index.html'), 'root body')

    def test_dummy_and_empty_responses_are_not_mirrored(self):
        mirror('http://example.com/skipped', 'dummy')
        mirror('http://example.com/empty', '')
        self.assertFalse(os.path.exists('example.com_mirror'))

    def test_nested_path_creates_directories(self):
        mirror('http://example.com/a/b/page', 'nested')
        self.assertEqual(
            self._read('example.com_mirror', 'a', 'b', 'page.html'),
            'nested')


if __name__ == '__main__':
    unittest.main()

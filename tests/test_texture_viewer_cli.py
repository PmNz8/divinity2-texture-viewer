# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
"""Developer entry-point checks: never instantiate a Tk window."""

import subprocess
import sys
import unittest


class TextureViewerCLITests(unittest.TestCase):
    def test_check_needs_neither_window_nor_webview_stack(self):
        script = '''
import importlib.abc
import sys
import tkinter
class RejectWebStack(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'webview', 'terrain_viewer', 'clr'}:
            raise AssertionError('Unexpected GUI dependency: ' + fullname)
sys.meta_path.insert(0, RejectWebStack())
def reject_window(*args, **kwargs):
    raise AssertionError('Headless check attempted to create a window')
tkinter.Tk = reject_window
from texture_viewer.cli import main
raise SystemExit(main(['check']))
'''
        result = subprocess.run(
            [sys.executable, '-c', script], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"frontend": "tkinter"', result.stdout)
        self.assertIn('"gui_tested": false', result.stdout)


if __name__ == '__main__':
    unittest.main()

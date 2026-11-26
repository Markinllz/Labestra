import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labestra.review_ui import build
from labestra.submission import BASELINE_ARCHIVE, ROOT
import zipfile


class ReviewFormTests(unittest.TestCase):
    def test_form_is_unlabelled_and_saved_session_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            with zipfile.ZipFile(ROOT / BASELINE_ARCHIVE) as bundle:
                bundle.extractall(directory)
            session=next(directory.glob('*/summary.json')).parent
            output=directory/'review.html'
            with patch('socket.socket.connect',side_effect=AssertionError('No network')):
                self.assertEqual(build(session, ROOT/'eval/review/gold.visible.jsonl', output),12)
            text=output.read_text()
            self.assertIn('covered:{},accepted:',text)
            self.assertIn('const truthControls=controls.slice()',text)
            self.assertIn('if(!s.confirmed',text)
            self.assertNotIn('reviewer_id":"sabukhi',text)
            with self.assertRaises(FileExistsError):
                build(session,ROOT/'eval/review/gold.visible.jsonl',output)

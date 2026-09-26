import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from emetools.data_enrichment import DB_SCRIPT, DataEnrichment, _docker_job


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.strm = root / 'S01E01.DoVi.strm'
        self.strm.write_text('https://example.org/video.mp4\n', encoding='utf8')
        self.storage = {}
        owner = SimpleNamespace(_strm_root=str(root), get_data=lambda key: self.storage.get(key),
                                save_data=lambda key, value: self.storage.__setitem__(key, value))
        self.enrichment = DataEnrichment(owner)

    def test_preview_classification_and_cached_repair(self):
        key = 'Emby::123'
        thumb = str(self.strm)[:-5] + '-thumb.jpg'
        self.assertEqual(self.enrichment._preview_status(key, thumb), 'missing')
        from PIL import Image
        Image.new('RGB', (200, 200), (30, 205, 50)).save(thumb)
        self.assertEqual(self.enrichment._preview_status(key, thumb), 'candidate')
        stat = os.stat(thumb)
        self.storage['enrichment_preview_cache'] = {key: {'thumb': thumb, 'mtime': int(stat.st_mtime), 'size': stat.st_size}}
        self.assertEqual(self.enrichment._preview_status(key, thumb), 'fixed')

    def test_reject_strm_outside_root(self):
        self.assertEqual(self.enrichment._safe_strm(self.strm), str(self.strm))
        with self.assertRaises(ValueError):
            self.enrichment._safe_strm('/etc/passwd')

    def test_database_script_writes_only_selected_fields_and_keeps_backup(self):
        db = Path(self.temp.name) / 'library.db'
        with sqlite3.connect(db) as connection:
            connection.execute('CREATE TABLE MediaItems (Id INTEGER PRIMARY KEY, Size INTEGER, RunTimeTicks INTEGER)')
            connection.execute('INSERT INTO MediaItems VALUES (1, 0, 0)')
        script = DB_SCRIPT.replace('/emby-config/data/library.db', str(db))
        environment = {**os.environ, 'ENRICH_UPDATES': json.dumps({'1': {'Size': 1024, 'RunTimeTicks': 20000000},
                                                                    '2': {'Size': 999}})}
        result = subprocess.run([sys.executable, '-c', script], env=environment,
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['updated'], 1)
        with sqlite3.connect(db) as connection:
            self.assertEqual(connection.execute('SELECT Size, RunTimeTicks FROM MediaItems').fetchone(), (1024, 20000000))
        with sqlite3.connect(json.loads(result.stdout)['backup']) as connection:
            self.assertEqual(connection.execute('SELECT Size FROM MediaItems').fetchone(), (0,))

    def test_sidecar_wait_error_still_removes_container(self):
        with patch('emetools.data_enrichment._docker') as docker:
            docker.side_effect = [SimpleNamespace(json=lambda: {'Id': 'abc'}),
                                  SimpleNamespace(), RuntimeError('wait timeout'), SimpleNamespace()]
            with self.assertRaises(RuntimeError):
                _docker_job('test-image', {})
            self.assertEqual(docker.call_args.args, ('DELETE', '/containers/abc'))


if __name__ == '__main__':
    unittest.main()

import unittest
from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock, patch

from resourceflow import ResourceFlow, BrushTaskConfig
from resourceflow.models import BrushTaskPayload


class DownloadTypeTest(unittest.TestCase):
    def setUp(self):
        self.plugin = ResourceFlow.__new__(ResourceFlow)
        self.plugin.chain = SimpleNamespace(recognize_media=Mock())

    def classify(self, media=None, category=None, labels=None, cache=None):
        self.plugin.chain.recognize_media.return_value = media
        torrent = SimpleNamespace(title="Example.S01E01.2026", description="", category=category, labels=labels or [])
        return self.plugin._download_type(torrent, cache if cache is not None else {})

    def test_special_site_category_does_not_need_network(self):
        self.assertEqual(self.classify(category="体育")[0], "sports")
        self.assertEqual(self.classify(labels=["MV"])[0], "mv")
        self.plugin.chain.recognize_media.assert_not_called()

    def test_genres_override_basic_site_category(self):
        for genre, expected in [(16, "anime"), (99, "documentary"), (10764, "variety"), (10767, "variety")]:
            with self.subTest(genre=genre):
                self.assertEqual(self.classify(SimpleNamespace(type="电视剧", genre_ids=[genre]), "电视剧")[0], expected)

    def test_media_genre_objects_and_basic_type(self):
        media = SimpleNamespace(type="电影", genres=[SimpleNamespace(id=16, name="Animation")])
        self.assertEqual(self.classify(media)[0], "anime")
        self.assertEqual(self.classify(SimpleNamespace(type="电影", genre_ids=[]))[0], "movie")
        self.assertEqual(self.classify(SimpleNamespace(type="电视剧", genre_ids=[]))[0], "tv")

    def test_failure_and_site_fallback(self):
        self.assertIsNone(self.classify()[0])
        self.assertEqual(self.classify(category="电影")[0], "movie")
        self.plugin.chain.recognize_media.side_effect = RuntimeError("network")
        self.assertIsNone(self.classify()[0])

    def test_cache_and_config_compatibility(self):
        cache = {}
        self.classify(SimpleNamespace(type="电影"), cache=cache)
        self.classify(SimpleNamespace(type="电影"), cache=cache)
        self.plugin.chain.recognize_media.assert_called_once()
        self.assertEqual(BrushTaskConfig({}).download_types, [])
        payload = BrushTaskPayload(name="task", site_id=1, downloader="qb", download_types=["anime"])
        self.assertEqual(payload.download_types, ["anime"])
        with self.assertRaises(ValueError):
            BrushTaskPayload(name="task", site_id=1, downloader="qb", download_types=["invalid"])

    def test_filter_stops_unselected_type_before_downloader(self):
        task = BrushTaskConfig({"name": "test", "download_types": ["anime"], "except_subscribe": False})
        self.plugin._get_task_config = Mock(return_value=task)
        self.plugin._blacklist_other_site_names = Mock(return_value=set())
        self.plugin._ResourceFlow__evaluate_pre_conditions_for_brush = Mock(return_value=(True, ""))
        self.plugin._ResourceFlow__evaluate_size_condition_for_brush = Mock(return_value=(True, ""))
        self.plugin._ResourceFlow__evaluate_conditions_for_brush = Mock(return_value=(True, ""))
        self.plugin._ResourceFlow__calculate_seeding_torrents_size = Mock(return_value=0)
        self.plugin._ResourceFlow__download = Mock(return_value=None)
        torrents = [
            SimpleNamespace(title="Anime", description="", category="动漫", labels=[], pubdate="", size=1),
            SimpleNamespace(title="Film", description="", category="电影", labels=[], pubdate="", size=1),
            SimpleNamespace(title="Unknown", description="", category=None, labels=[], pubdate="", size=1),
        ]
        self.plugin.chain.recognize_media.return_value = None
        report = {"reason_counts": Counter(), "blacklist_excluded": 0, "added_count": 0, "added_titles": []}
        with patch("resourceflow.TorrentsChain") as chain:
            chain.return_value.browse.return_value = torrents
            self.plugin._ResourceFlow__brush_site_torrents(
                SimpleNamespace(name="site", domain="example.org"), {}, {}, set(), report, 0
            )
        self.plugin._ResourceFlow__download.assert_called_once_with(torrents[0])
        self.assertEqual(report["reason_counts"]["下载类型未选择：movie"], 1)
        self.assertEqual(report["reason_counts"]["下载类型无法识别"], 1)


if __name__ == "__main__":
    unittest.main()

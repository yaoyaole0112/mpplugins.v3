import re
import unittest
from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.application.classification.reference import ClassificationCategoryResolver
from app.schemas import MediaType

from resourceflow import ResourceFlow, BrushTaskConfig
from resourceflow.models import BrushTaskPayload, ResourceFlowSettingsPayload


class DownloadTypeTest(unittest.TestCase):
    def setUp(self):
        self.plugin = ResourceFlow.__new__(ResourceFlow)
        self.plugin._mp_transfer_exclude_patterns = Mock(return_value=())
        self.plugin.chain = SimpleNamespace(recognize_media=Mock())
        self.plugin._mp_recognize_media = self.plugin.chain.recognize_media

    def classify(self, media=None, category=None, labels=None, description="", cache=None):
        self.plugin.chain.recognize_media.return_value = media
        torrent = SimpleNamespace(title="Example.S01E01.2026", description=description,
                                  category=category, labels=labels or [])
        return self.plugin._download_type(torrent, cache if cache is not None else {})

    def test_special_site_category_is_a_fallback(self):
        self.assertEqual(self.classify(category="体育")[0], "sports")
        self.assertIsNone(self.classify(labels=["MV"])[0])
        self.plugin.chain.recognize_media.assert_called()

    def test_mp_library_category_wins_over_station_music_tag(self):
        media = SimpleNamespace(type="电视剧", library_category="动画/国漫", genre_ids=[])
        kind, source = self.classify(media=media, category="音乐")
        self.assertEqual(kind, "anime")
        self.assertEqual(source, "MoviePilot 整理分类：动画/国漫")

    def test_episodic_title_cannot_be_classified_music_by_site_alone(self):
        self.assertIsNone(self.classify(category="音乐")[0])
        torrent = SimpleNamespace(title="Example Artist 2026", category="音乐", labels=[], description="")
        self.assertEqual(self.plugin._download_type(torrent, {})[0], "music")

    def test_custom_category_name_falls_back_to_mp_type_not_station_category(self):
        media = SimpleNamespace(type="电影", library_category="精选收藏/独立制作")
        self.assertEqual(self.classify(media=media, category="动漫")[0], "movie")

    def test_short_drama_category_and_label_are_explicit_types(self):
        self.assertEqual(self.classify(SimpleNamespace(type="电视剧"), category="短剧")[0], "short_drama")
        self.assertEqual(self.classify(labels=["微短剧"])[0], "short_drama")
        self.assertEqual(self.classify(category="short drama")[0], "short_drama")

    def test_short_drama_string_labels_and_description_category(self):
        self.assertEqual(self.classify(labels="短剧")[0], "short_drama")
        self.assertEqual(self.classify(labels="电视剧/短剧")[0], "short_drama")
        self.assertEqual(self.classify(description="类型：剧情/短剧")[0], "short_drama")
        self.assertIsNone(self.classify(description="剧情简介：讲述短剧演员的故事")[0])

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
        payload = BrushTaskPayload(name="task", site_id=1, downloader="qb", download_types=["short_drama"])
        self.assertEqual(payload.download_types, ["short_drama"])
        with self.assertRaises(ValueError):
            BrushTaskPayload(name="task", site_id=1, downloader="qb", download_types=["invalid"])

    def test_effective_moviepilot_category_is_exact_and_supports_custom_paths(self):
        policy = SimpleNamespace(revision=1, categories=[
            SimpleNamespace(id="custom", name="精选", path=["影片", "独立制作"],
                            enabled=True, media_type=MediaType.MOVIE.value),
        ])
        resolver = ClassificationCategoryResolver(lambda: policy)
        selected = SimpleNamespace(category_id="custom", category_path=["旧目录"])
        media = SimpleNamespace(type=MediaType.MOVIE, library_category="外语电影",
                                classification=SimpleNamespace(effective=selected, recommended=None))
        self.plugin._mp_recognize_media.return_value = media
        torrent = SimpleNamespace(title="Custom Movie", category="外语电影", description="")
        task = BrushTaskConfig({"download_categories": ["movie:影片/独立制作"], "download_types": ["tv"]})
        cache = {}
        with patch("resourceflow.classification_category_resolver_snapshot", return_value=resolver):
            self.assertEqual(self.plugin._mp_download_category_options(), [
                {"value": "movie:影片/独立制作", "title": "电影 · 影片/独立制作"},
            ])
            self.assertTrue(self.plugin._download_category_filter(task, torrent, cache)[0])
            task.download_categories = ["tv:影片/独立制作", "movie:外语电影"]
            self.assertEqual(self.plugin._download_category_filter(task, torrent, cache),
                             (False, "MoviePilot 整理分类未选择：影片/独立制作"))
            policy.categories[0].enabled = False
            self.assertFalse(self.plugin._download_category_filter(task, torrent, {})[0])
        self.assertEqual(task.download_types, [])

    def test_category_filter_fails_closed_without_moviepilot_identity(self):
        self.plugin._mp_recognize_media.return_value = None
        task = BrushTaskConfig({"download_categories": ["tv:国产剧"]})
        torrent = SimpleNamespace(title="Example", category="国产剧", description="")
        self.assertEqual(self.plugin._download_category_filter(task, torrent, {}),
                         (False, "无法确认 MoviePilot 整理分类"))
        self.assertEqual(self.plugin._download_category_filter(
            BrushTaskConfig({"download_categories": [], "download_types": ["movie"]}), torrent, {}),
            (True, ""))

    def test_domestic_drama_requires_moviepilot_tv_category_and_reuses_recognition(self):
        torrent = SimpleNamespace(title="Example S01E01", category="国产剧", description="")
        task = BrushTaskConfig({"download_categories": ["tv:剧集/国产剧/古装"]})
        self.assertFalse(ResourceFlowSettingsPayload().domestic_drama_4k_only)
        for kind, path, expected in [
            (MediaType.TV, ["剧集", "国产剧", "古装"], True),
            (MediaType.TV, ["国产动画"], False),
            (MediaType.TV, ["国产剧场"], False),
            (MediaType.MOVIE, ["国产剧"], False),
            (MediaType.TV, [], False),
        ]:
            with self.subTest(kind=kind, path=path):
                self.plugin._mp_recognize_media.reset_mock()
                media = SimpleNamespace(type=kind)
                self.plugin._mp_recognize_media.return_value = media
                resolver = Mock()
                resolver.resolve_media.return_value = SimpleNamespace(path=path, state="resolved")
                with patch("resourceflow.classify_media", return_value=media), patch(
                    "resourceflow.classification_category_resolver_snapshot", return_value=resolver,
                ):
                    cache = {}
                    self.assertEqual(self.plugin._download_category_filter(task, torrent, cache)[0], expected)
                    self.assertEqual(self.plugin._is_domestic_drama(
                        self.plugin._mp_download_category(torrent, cache),
                    ), expected)
                self.plugin._mp_recognize_media.assert_called_once()
                resolver.resolve_media.assert_called_once_with(media)

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
            SimpleNamespace(title="Show 加更篇", description="", category="动漫", labels=[], pubdate="", size=1),
        ]
        patterns = (re.compile("加更篇", re.I),)
        self.plugin._mp_transfer_exclude_patterns.return_value = patterns
        self.plugin.chain.recognize_media.return_value = None
        report = {"reason_counts": Counter(), "blacklist_excluded": 0, "added_count": 0, "added_titles": []}
        with patch("resourceflow.TorrentsChain") as chain:
            chain.return_value.browse.return_value = torrents
            self.plugin._ResourceFlow__brush_site_torrents(
                SimpleNamespace(name="site", domain="example.org"), {}, {}, set(), report, 0
            )
        self.plugin._ResourceFlow__download.assert_called_once_with(torrents[0], transfer_patterns=patterns)
        self.assertEqual(self.plugin.chain.recognize_media.call_count, 3)
        self.assertEqual(report["reason_counts"]["命中 MoviePilot 文件整理屏蔽词"], 1)
        self.assertEqual(sum(count for reason, count in report["reason_counts"].items()
                             if reason.startswith("旧下载类型未通过：")), 2)


if __name__ == "__main__":
    unittest.main()

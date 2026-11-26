import json
import asyncio
import threading
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from app.sdk.media import MetaInfo
from resourceflow import BrushTaskConfig, ResourceFlow, RequestUtils
from resourceflow.models import BrushTaskPayload


class RecentEpisodeRuleTest(unittest.TestCase):
    def setUp(self):
        for name in ("_tmdb_year_season", "_tmdb_episode_air_date", "_tmdb_web_episode_air_dates",
                     "_recent_episode_douban_id", "_douban_episode_air_dates"):
            getattr(ResourceFlow, name).cache_clear()

    def select_files(self, paths, media, season_dates=(), days=3, plugin=None):
        plugin = plugin or ResourceFlow.__new__(ResourceFlow)
        if media and not hasattr(media, "type"):
            media = SimpleNamespace(**vars(media), type="tv")
        plugin._mp_recognize_media = Mock(return_value=media)
        plugin.save_data = Mock()
        with patch("resourceflow.TorrentHelper") as torrent_helper, patch("resourceflow.TmdbChain") as tmdb:
            torrent_helper.return_value.get_fileinfo_from_torrent_content.return_value = (None, paths)
            tmdb.return_value.tmdb_episodes.side_effect = lambda *, season, **_: [
                SimpleNamespace(episode_number=number, air_date=value)
                for number, value in (season_dates.get(season, ()) if isinstance(season_dates, dict) else season_dates)
            ]
            return plugin._recent_episode_file_selection(b"torrent", "test", days)

    def test_identity_gate_filters_unrecognized_media(self):
        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._mp_file_recognition = Mock(return_value=(None, None, None, [], "MoviePilot 未确认电影或电视剧身份"))
        task = SimpleNamespace(name="test")

        selection = plugin._mp_identity_file_selection(
            [{"path": "Unknown.S01E01.1080p.mkv"}], "Unknown S01E01", task,
        )

        self.assertIsNone(selection)

    def test_identity_gate_obeys_douban_only_action(self):
        plugin = ResourceFlow.__new__(ResourceFlow)
        media = SimpleNamespace(
            type="tv", media_source="douban", media_id="9876543", title="豆瓣独有剧",
        )
        plugin._mp_file_recognition = Mock(return_value=(None, media, 1, [4], ""))
        task = SimpleNamespace(name="test")
        files = [{"path": "Douban.Only.Show.S01E04.2026.1080p.WEB-DL.mkv"}]

        plugin._douban_only_action = "exclude"
        self.assertIsNone(plugin._mp_identity_file_selection(files, "豆瓣独有剧", task))

        plugin._douban_only_action = "download"
        selection = plugin._mp_identity_file_selection(files, "豆瓣独有剧", task)
        self.assertEqual(selection["accepted"], {files[0]["path"]})

    def test_douban_only_download_bypasses_all_episode_date_lookups(self):
        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._douban_only_action = "download"
        media = SimpleNamespace(
            type="tv", media_source="douban", media_id="9876543", title="豆瓣独有剧",
        )
        path = "Douban.Only.Show.S01E04.2026.1080p.WEB-DL.mkv"
        plugin._mp_recognize_media = Mock(return_value=media)
        with (
            patch("resourceflow.TorrentHelper") as torrent_helper,
            patch("resourceflow.TmdbChain") as tmdb,
            patch.object(ResourceFlow, "_douban_episode_air_dates") as douban_dates,
        ):
            torrent_helper.return_value.get_fileinfo_from_torrent_content.return_value = (None, [path])
            result = plugin._recent_episode_file_selection(b"torrent", "Douban Only Show S01E04", 3)

        self.assertEqual(result["accepted"], {path})
        tmdb.return_value.tmdb_episodes.assert_not_called()
        douban_dates.assert_not_called()

    def test_douban_only_exclude_skips_date_queries_and_filters_candidate(self):
        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._douban_only_action = "exclude"
        media = SimpleNamespace(
            type="tv", media_source="douban", media_id="9876543", title="豆瓣独有剧",
        )
        path = "Douban.Only.Show.S01E04.2026.1080p.WEB-DL.mkv"
        plugin._mp_recognize_media = Mock(return_value=media)
        with (
            patch("resourceflow.TorrentHelper") as torrent_helper,
            patch("resourceflow.TmdbChain") as tmdb,
            patch.object(ResourceFlow, "_douban_episode_air_dates") as douban_dates,
        ):
            torrent_helper.return_value.get_fileinfo_from_torrent_content.return_value = (None, [path])
            result = plugin._recent_episode_file_selection(b"torrent", "Douban Only Show S01E04", 3)

        self.assertIsNone(result)
        tmdb.return_value.tmdb_episodes.assert_not_called()
        douban_dates.assert_not_called()

    def test_includes_cutoff_today_and_future_dates(self):
        today = date(2026, 11, 9)
        self.assertTrue(ResourceFlow._air_date_is_recent("2026-11-06", today))
        self.assertTrue(ResourceFlow._air_date_is_recent("2026-11-09", today))
        self.assertTrue(ResourceFlow._air_date_is_recent("2026-11-15", today))

    def test_excludes_dates_older_than_cutoff_and_invalid_dates(self):
        today = date(2026, 11, 9)
        self.assertFalse(ResourceFlow._air_date_is_recent("2026-11-05", today))
        self.assertFalse(ResourceFlow._air_date_is_recent(None, today))
        self.assertFalse(ResourceFlow._air_date_is_recent("2026-02-30", today))

    def test_custom_recent_days_changes_inclusive_cutoff_and_defaults_to_three(self):
        today = date(2026, 11, 9)
        self.assertTrue(ResourceFlow._air_date_is_recent("2026-11-06", today))
        self.assertFalse(ResourceFlow._air_date_is_recent("2026-11-05", today, days=2))
        self.assertTrue(ResourceFlow._air_date_is_recent("2026-11-07", today, days=2))
        self.assertEqual(
            BrushTaskPayload(name="test", site_id=1, downloader="QB",
                             recent_episodes_only=True).recent_episodes_days,
            3,
        )

    def test_video_filter_ignores_non_video_files(self):
        self.assertTrue(ResourceFlow._video_file("Season 2/E02.mkv"))
        self.assertFalse(ResourceFlow._video_file("Season 2/E02.srt"))

    def test_ai_bridge_runs_host_tools_on_moviepilot_main_loop(self):
        from resourceflow.recent_air_search import run_on_moviepilot_loop

        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def serve_loop():
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(target=serve_loop, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(timeout=2))
        try:
            with patch("app.runtime.loop.main_loop_registry", SimpleNamespace(current=loop)):
                async def use_host_loop_resource():
                    semaphore = asyncio.Semaphore(0)

                    async def release():
                        await asyncio.sleep(0.01)
                        semaphore.release()

                    asyncio.create_task(release())
                    await semaphore.acquire()
                    return asyncio.get_running_loop()

                self.assertIs(run_on_moviepilot_loop(use_host_loop_resource()), loop)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)
            loop.close()

    def test_movie_year_uses_tmdb_release_date_then_recognized_or_filename_year(self):
        self.assertEqual(
            ResourceFlow._movie_year(SimpleNamespace(release_date="2026-12-01", year="2025"), "2024"),
            2026,
        )
        self.assertEqual(ResourceFlow._movie_year(SimpleNamespace(year="2026"), None), 2026)
        self.assertEqual(ResourceFlow._movie_year(SimpleNamespace(), "2026"), 2026)
        self.assertIsNone(ResourceFlow._movie_year(SimpleNamespace(), None))

    def test_recent_rule_accepts_current_year_movie_and_rejects_old_or_unknown_year(self):
        cases = [
            ("Current.Movie.2026.mkv", SimpleNamespace(type="电影", tmdb_id=1, year=str(datetime.now(ZoneInfo("Asia/Shanghai")).year)), True),
            ("Old.Movie.2025.mkv", SimpleNamespace(type="movie", tmdb_id=2, release_date="2000-12-31"), False),
            ("Unknown.Movie.mkv", SimpleNamespace(type="movie", tmdb_id=3), False),
        ]
        for path, media, expected in cases:
            with self.subTest(path=path):
                result = self.select_files([path], media)
                if expected:
                    self.assertEqual(result["accepted"], {path})
                else:
                    self.assertIsNone(result)

    def test_metainfo_extracts_single_and_multi_episode_ranges(self):
        single = MetaInfo("Example.Series.S02E03.mkv")
        multiple = MetaInfo("Example.Series.S02E03-E04.mkv")
        unknown = MetaInfo("Example.Series.2026.1080p.mkv")
        self.assertEqual((single.begin_season, single.episode_list), (2, [3]))
        self.assertEqual((multiple.begin_season, multiple.episode_list), (2, [3, 4]))
        self.assertEqual(unknown.episode_list, [])

    def test_recent_rule_looks_up_canonical_season_for_year_season(self):
        path = "你好，星期六.H!6.S2026E45.2026.1080p.WEB-DL.H265.AAC-ADWeb.mkv"
        media = SimpleNamespace(type="电视剧", tmdb_id=154770, title="你好，星期六", year="2022")
        aired = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        with patch("resourceflow.TmdbApi") as tmdb:
            tmdb.return_value.get_info.return_value = {"seasons": [
                {"season_number": 4, "name": "2025"},
                {"season_number": 5, "name": "2026"},
            ]}
            result = self.select_files([path], media, {5: [(45, aired)]}, days=3)
            self.assertEqual(result["accepted"], {path})
            tmdb.return_value.get_info.assert_called_once()
        ResourceFlow._tmdb_year_season.cache_clear()
        with patch("resourceflow.TmdbApi") as tmdb:
            tmdb.return_value.get_info.return_value = {"seasons": [
                {"season_number": 5, "name": "2026"}, {"season_number": 6, "name": "2026年"},
            ]}
            self.assertIsNone(self.select_files([path], media, {5: [(45, aired)]}, days=3))

    def test_missing_episode_fallback_only_uses_numbers_above_latest_without_requiring_continuity(self):
        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._recent_missing_date_tolerance_days = 7
        plugin._recent_episode_evidence = {}
        plugin.save_data = Mock()
        dates = {8: "2026-11-18"}
        sources = {8: "TMDB整季API"}
        inferred = plugin._infer_missing_episode_dates(
            "多喜一家人",
            "多喜一家人.Still.A.Family.S01.mkv",
            SimpleNamespace(tmdb_id=294498),
            1,
            [7, 11, 25],
            dates,
            sources,
            date(2026, 11, 20),
        )
        self.assertEqual(inferred, {11, 25})
        self.assertNotIn(7, dates)
        self.assertEqual(dates[11], "2026-11-18")
        self.assertEqual(dates[25], "2026-11-18")
        self.assertTrue(sources[11].startswith("日期推断"))
        self.assertTrue(plugin.save_data.called)

    def test_recent_weekly_cadence_projects_real_episode_dates_before_recent_window(self):
        path = "You.Quiz.On.The.Block.S04E370.2026.1080p.WEB-DL.mkv"
        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._recent_missing_date_tolerance_days = 7
        plugin._recent_episode_evidence = {}
        plugin._recent_episode_ai_dates = Mock(return_value={})
        plugin.save_data = Mock()
        with patch("resourceflow.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 26)
            result = self.select_files(
                [path],
                SimpleNamespace(tmdb_id=81978, title="刘在街头"),
                {4: [(367, "2026-11-04"), (368, "2026-11-11")]},
                days=3,
                plugin=plugin,
            )
        self.assertEqual(result["accepted"], {path})
        plugin._recent_episode_ai_dates.assert_not_called()

    def test_episode_gap_expands_tolerance_when_weekly_cadence_is_unknown(self):
        path = "You.Quiz.On.The.Block.S04E370.2026.1080p.WEB-DL.mkv"
        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._recent_missing_date_tolerance_days = 7
        plugin._recent_episode_evidence = {}
        plugin._recent_episode_ai_dates = Mock(return_value={})
        plugin.save_data = Mock()
        with patch("resourceflow.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 26)
            result = self.select_files(
                [path],
                SimpleNamespace(tmdb_id=81978, title="刘在街头"),
                {4: [(368, "2026-11-11")]},
                days=3,
                plugin=plugin,
            )
        self.assertEqual(result["accepted"], {path})
        self.assertIn("缺失日期容差兜底", plugin._recent_episode_evidence["81978:4:370"]["source"])

    def test_episode_gap_tolerance_remains_bounded(self):
        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._recent_missing_date_tolerance_days = 7
        plugin._recent_episode_evidence = {}
        plugin.save_data = Mock()
        dates = {368: "2026-11-01"}
        sources = {368: "TMDB整季API"}
        inferred = plugin._infer_missing_episode_dates(
            "刘在街头",
            "",
            SimpleNamespace(tmdb_id=81978),
            4,
            [370],
            dates,
            sources,
            date(2026, 11, 26),
        )
        self.assertEqual(inferred, set())

    def test_missing_episode_fallback_does_not_use_special_or_old_anchor(self):
        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._recent_missing_date_tolerance_days = 7
        plugin._recent_episode_evidence = {}
        plugin.save_data = Mock()
        for title in ("抢鲜版", "预告"):
            dates = {8: "2026-11-18"}
            sources = {8: "TMDB整季API"}
            self.assertEqual(
                plugin._infer_missing_episode_dates(
                    title,
                    f"{title}.S01E09.mkv",
                    SimpleNamespace(tmdb_id=1),
                    1,
                    [9],
                    dates,
                    sources,
                    date(2026, 11, 20),
                ),
                set(),
            )
        dates = {8: "2026-11-01"}
        self.assertEqual(
            plugin._infer_missing_episode_dates(
                "正常剧",
                "正常剧.S01E09.mkv",
                SimpleNamespace(tmdb_id=1),
                1,
                [9],
                dates,
                {},
                date(2026, 11, 20),
            ),
            set(),
        )

    def test_recent_episode_evidence_is_pruned_and_normalized(self):
        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._recent_episode_evidence = {
            "ok": {
                "tmdb_id": "123",
                "season": "1",
                "episode": "9",
                "air_date": "2026-11-19",
                "source": "官方排播",
                "confidence": "high",
                "updated_at": 4102444800,
            },
            "invalid": {"tmdb_id": 123, "updated_at": 4102444800},
            "old": {
                "tmdb_id": 123, "season": 1, "episode": 1,
                "air_date": "2026-01-01", "updated_at": 1,
            },
        }
        plugin.save_data = Mock()
        plugin._prune_recent_episode_evidence()
        self.assertEqual(plugin._recent_episode_evidence["ok"]["tmdb_id"], 123)
        self.assertNotIn("invalid", plugin._recent_episode_evidence)
        self.assertNotIn("old", plugin._recent_episode_evidence)
        plugin.save_data.assert_called_once_with(
            "recent_episode_evidence", plugin._recent_episode_evidence
        )

    def test_recent_ai_title_accepts_tmdb_aliases_and_season_suffix(self):
        media = SimpleNamespace(
            title="Still a Family",
            en_title="Still a Family",
            names=["多喜一家人"],
        )
        self.assertTrue(ResourceFlow._recent_episode_title_matches(
            "多喜一家人", "Still a Family", media
        ))
        self.assertTrue(ResourceFlow._recent_episode_title_matches(
            "多喜一家人 第1季", "Still a Family", media
        ))
        self.assertFalse(ResourceFlow._recent_episode_title_matches(
            "完全不同的剧", "Still a Family", media
        ))

    def test_air_date_search_tries_chinese_alias_and_episode_period_variants(self):
        from app.agent.tools.impl.search_web import SearchWebTool
        from resourceflow.recent_air_search import search_episode_evidence

        row = {"url": "https://example.com/schedule", "title": "鲜味中国播出安排",
               "snippet": "第4集于2026年11月20日播出"}
        search_payload = {"total_results": 1, "results": [row]}
        with patch.object(
            SearchWebTool, "run", new=AsyncMock(return_value=json.dumps(search_payload))
        ) as search:
            results = asyncio.run(search_episode_evidence({
                "title": "Xian Wei Zhong Guo", "aliases": ["鲜味中国"], "year": "2026",
                "season": 1, "episodes": [4],
            }))

        queries = [call.kwargs["query"] for call in search.await_args_list]
        self.assertTrue(any("鲜味中国" in query for query in queries))
        self.assertTrue(any("第4期" in query for query in queries))
        self.assertTrue(any("S01E04" in query for query in queries))
        self.assertEqual(len(results), 1)

    def test_ai_calls_mp_local_search_when_builtin_is_unavailable_and_requires_real_evidence(self):
        from app.agent.llm.helper import LLMHelper
        from app.agent.llm.tools import ServerToolUnavailableError
        from app.agent.tools.impl.search_web import SearchWebTool

        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._recent_missing_date_ai_enabled = True
        plugin.save_data = Mock()
        media = SimpleNamespace(tmdb_id=294498, title="多喜一家人")
        url = "https://v.qq.com/example/official-schedule.html"
        quote = "2026年11月20日播出第11集"
        row = {"url": url, "title": "多喜一家人 官方排播", "snippet": quote}
        answer = {"title": media.title, "tmdb_id": 294498, "season": 1, "episode": 11,
                  "date": "2026-11-20", "confidence": "high", "source": url, "quote": quote}
        response = SimpleNamespace(content=json.dumps([
            answer,
            {**answer, "episode": 12, "source": "https://v.qq.com/guessed.html"},
            {**answer, "episode": 13, "quote": "2026年11月20日播出第13集"},
            {**answer, "episode": 14, "season": 2},
            {**answer, "episode": 15, "tmdb_id": None},
        ]))
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=response))
        unavailable = ServerToolUnavailableError(provider="custom", model="test", tool_id="web_search")
        with (
            patch.object(LLMHelper, "get_llm", new=AsyncMock(side_effect=[unavailable, model])) as make,
            patch.object(LLMHelper, "close_llm", new_callable=AsyncMock),
            patch.object(SearchWebTool, "run", new=AsyncMock(return_value=json.dumps([row]))) as search,
        ):
            results = plugin._recent_episode_ai_dates(media.title, media, 1, [11, 12, 13, 14, 15])
        self.assertNotIn("web_search_mode", make.await_args_list[0].kwargs)
        self.assertEqual(make.await_args_list[1].kwargs["web_search_mode"], "local")
        self.assertEqual(search.await_count, 3)
        self.assertIn("search_results", model.ainvoke.await_args.args[0][-1].content)
        self.assertEqual(set(results), {11, 13})
        self.assertEqual(results[11]["confidence"], "high")
        self.assertEqual(results[13]["confidence"], "medium")

    def test_ai_accepts_douban_only_identity_without_fabricated_tmdb_id(self):
        from app.agent.llm.helper import LLMHelper
        from app.agent.tools.impl.search_web import SearchWebTool

        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._recent_missing_date_ai_enabled = True
        plugin.save_data = Mock()
        title = "鲜味中国"
        url = "https://v.qq.com/example/official-schedule.html"
        quote = "《鲜味中国》2026年11月20日播出第4集"
        row = {"url": url, "title": "鲜味中国官方排播", "snippet": quote}
        answer = {
            "title": title, "tmdb_id": None, "season": 1, "episode": 4,
            "date": "2026-11-20", "confidence": "high", "source": url, "quote": quote,
        }
        response = SimpleNamespace(content=json.dumps([answer], ensure_ascii=False))
        model = SimpleNamespace(
            ainvoke=AsyncMock(return_value=response),
            _moviepilot_server_tool_metadata={"mode": "local", "use_local_web_search": True},
        )
        with (
            patch.object(LLMHelper, "get_llm", new=AsyncMock(return_value=model)),
            patch.object(LLMHelper, "close_llm", new_callable=AsyncMock),
            patch.object(
                SearchWebTool, "run", new=AsyncMock(return_value=json.dumps([row], ensure_ascii=False))
            ),
        ):
            results = plugin._recent_episode_ai_dates(title, SimpleNamespace(title=title), 1, [4])

        self.assertEqual(results[4]["date"], "2026-11-20")
        self.assertEqual(results[4]["confidence"], "high")
        request = json.loads(model.ainvoke.await_args.args[0][-1].content)
        self.assertIsNone(request["tmdb_id"])

    def test_ai_uses_provider_search_when_local_search_has_no_evidence(self):
        from app.agent.llm.helper import LLMHelper
        from app.agent.tools.impl.search_web import SearchWebTool

        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._recent_missing_date_ai_enabled = True
        plugin.save_data = Mock()
        title = "鲜味中国"
        url = "https://v.qq.com/example/official-schedule.html"
        answer = {
            "title": title, "tmdb_id": None, "season": 1, "episode": 4,
            "date": "2026-11-20", "confidence": "high", "source": url,
            "quote": "《鲜味中国》2026年11月20日播出第4集",
        }
        native_response = SimpleNamespace(content=[{
            "type": "text",
            "text": json.dumps([answer], ensure_ascii=False),
            "annotations": [{"type": "url_citation", "url": url}],
        }])
        local_model = SimpleNamespace(
            _moviepilot_server_tool_metadata={"mode": "local", "use_local_web_search": True},
        )
        auto_model = SimpleNamespace(
            ainvoke=AsyncMock(return_value=native_response),
            bind_tools=Mock(return_value=SimpleNamespace(
                ainvoke=AsyncMock(return_value=native_response)
            )),
            _moviepilot_server_tool_metadata={
                "mode": "builtin", "server_tools": [{"type": "web_search"}],
                "use_local_web_search": False,
            },
        )
        with (
            patch.object(
                LLMHelper, "get_llm",
                new=AsyncMock(side_effect=[local_model, auto_model]),
            ) as make,
            patch.object(LLMHelper, "close_llm", new_callable=AsyncMock),
            patch.object(
                SearchWebTool, "run",
                new=AsyncMock(return_value="未找到相关搜索结果"),
            ),
        ):
            results = plugin._recent_episode_ai_dates(
                title, SimpleNamespace(title=title), 1, [4]
            )

        self.assertEqual(make.await_args_list[1].kwargs["web_search_mode"], "auto")
        self.assertEqual(results[4]["date"], "2026-11-20")
        self.assertEqual(results[4]["confidence"], "medium")

    def test_ai_respects_disabled_search_without_prompting_model(self):
        from app.agent.llm.helper import LLMHelper

        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._recent_missing_date_ai_enabled = True
        model = SimpleNamespace(ainvoke=AsyncMock(), _moviepilot_server_tool_metadata={
            "mode": "disabled", "use_local_web_search": False,
        })
        with (
            patch.object(LLMHelper, "get_llm", new=AsyncMock(return_value=model)),
            patch.object(LLMHelper, "close_llm", new_callable=AsyncMock),
            patch("resourceflow.search_episode_evidence", new_callable=AsyncMock) as search,
        ):
            self.assertEqual(plugin._recent_episode_ai_dates("Show", SimpleNamespace(), 1, [11]), {})
        model.ainvoke.assert_not_awaited()
        search.assert_not_awaited()

    def test_ai_diagnostics_include_runtime_initialization_failure(self):
        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._recent_missing_date_ai_enabled = True

        def fail_and_close(coroutine):
            coroutine.close()
            raise RuntimeError("LLM provider runtime is not initialized")

        diagnostics = {}
        with patch("resourceflow.run_on_moviepilot_loop", side_effect=fail_and_close):
            result = plugin._recent_episode_ai_dates(
                "Show", SimpleNamespace(title="Show", tmdb_id=1), 1, [11],
                diagnostics=diagnostics,
            )
        self.assertEqual(result, {})
        self.assertIn("LLM provider runtime is not initialized", diagnostics["message"])

    def test_plugin_direct_ai_uses_search_evidence_without_mp_llm_runtime(self):
        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._recent_missing_date_ai_enabled = True
        plugin._recent_ai_api_base_url = "https://ai.example/v1"
        plugin._recent_ai_model = "test-model"
        plugin._recent_ai_api_key = "test-secret"
        plugin.save_data = Mock()
        title = "刘在街头"
        url = "https://tvn.cjenm.com/ko/youquiz/episodes/"
        quote = "2026년 11월 25일 방송 제370회"
        row = {"url": url, "title": "유 퀴즈 온 더 블럭", "snippet": quote}
        answer = [{
            "title": title, "tmdb_id": 81978, "season": 4, "episode": 370,
            "date": "2026-11-25", "confidence": "high", "source": url, "quote": quote,
        }]
        media = SimpleNamespace(
            title=title, en_title="You Quiz on the Block", tmdb_id=81978, year="2018",
        )
        with (
            patch("resourceflow.search_episode_evidence", new=AsyncMock(return_value=[row])),
            patch("resourceflow.run_on_moviepilot_loop", side_effect=lambda coroutine: asyncio.run(coroutine)),
            patch(
                "resourceflow.recent_air_ai.request_recent_air_dates",
                return_value=json.dumps(answer, ensure_ascii=False),
            ) as request,
        ):
            results = plugin._recent_episode_ai_dates(title, media, 4, [370])

        request.assert_called_once()
        self.assertEqual(request.call_args.args[:3], (
            "https://ai.example/v1", "test-model", "test-secret",
        ))
        self.assertEqual(results[370]["date"], "2026-11-25")
        self.assertEqual(results[370]["confidence"], "high")

    def test_native_response_with_citation_but_no_matching_excerpt_cannot_pass(self):
        from app.agent.llm.helper import LLMHelper

        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._recent_missing_date_ai_enabled = True
        plugin.save_data = Mock()
        url = "https://v.qq.com/example/official-schedule.html"
        response = SimpleNamespace(content=[{
            "type": "text",
            "text": json.dumps([{
                "title": "Show", "tmdb_id": 123, "season": 1, "episode": 11, "date": "2026-11-20",
                "source": url, "confidence": "high", "quote": "Episode 11: 2026-11-20",
            }]),
            "annotations": [{"type": "url_citation", "url": url}],
        }])
        bound = SimpleNamespace(ainvoke=AsyncMock(return_value=response))
        tools = [{"type": "web_search"}]
        model = SimpleNamespace(bind_tools=Mock(return_value=bound), _moviepilot_server_tool_metadata={
            "server_tools": tools, "use_local_web_search": False,
        })
        with (
            patch.object(LLMHelper, "get_llm", new=AsyncMock(return_value=model)),
            patch.object(LLMHelper, "close_llm", new_callable=AsyncMock),
            patch("resourceflow.search_episode_evidence", new=AsyncMock(return_value=[])) as search,
        ):
            result = plugin._recent_episode_ai_dates("Show", SimpleNamespace(tmdb_id=123), 1, [11])
        model.bind_tools.assert_called_once_with(tools)
        search.assert_awaited_once()
        self.assertEqual(result[11]["confidence"], "medium")

    def test_official_tvn_korean_episode_quote_can_be_verified(self):
        from resourceflow.recent_air_search import supports_official_date

        url = "https://tvn.cjenm.com/ko/youquiz/episodes/"
        quote = "2026년 11월 25일 방송 제370회"
        row = {"url": url, "title": "유 퀴즈 온 더 블럭 370회", "snippet": quote}
        item = {
            "source": url,
            "quote": quote,
        }
        self.assertTrue(
            supports_official_date(
                item,
                [row],
                date(2026, 11, 25),
                370,
                4,
                lambda _: False,
            )
        )

    @patch.object(ResourceFlow, "_recent_episode_douban_id", return_value="")
    @patch.object(ResourceFlow, "_tmdb_web_episode_air_dates", return_value={})
    @patch.object(ResourceFlow, "_tmdb_episode_air_date", return_value=None)
    def test_pack_groups_season_before_inference_and_only_selects_matching_files(self, single, web, douban):
        paths = [
            "Still.A.Family.S01E12.mkv",
            "Still.A.Family.S01E08.mkv",
            "Still.A.Family.S01E11.mkv",
            "Still.A.Family.S01E11.预告.mkv",
            "Still.A.Family.S01E07.mkv",
            "Still.A.Family.S02E11.mkv",
        ]
        plugin = ResourceFlow.__new__(ResourceFlow)
        plugin._recent_episode_ai_dates = Mock(return_value={})
        with patch("resourceflow.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 20)
            result = self.select_files(paths, SimpleNamespace(tmdb_id=294498, title="多喜一家人"),
                                       {1: [(8, "2026-11-18")]}, days=1, plugin=plugin)
        # E08 has an explicit date outside the task cutoff. E07 is before the
        # anchor, and a preview cannot borrow E11's inference from a real file.
        self.assertEqual(result["accepted"], {paths[0], paths[2]})
        self.assertEqual([item.args[2:] for item in plugin._recent_episode_ai_dates.call_args_list],
                         [(1, [7]), (2, [11])])
        self.assertEqual(web.call_count, 2)
        self.assertEqual(douban.call_count, 2)

    @patch.object(RequestUtils, "get_res")
    def test_douban_fallback_requires_explicit_episode_air_dates(self, get_res):
        class Response:
            ok = True

            @staticmethod
            def json():
                return {"episodes": [
                    {"episode": 1, "title": "第一集"},
                    {"episode": 2, "title": "第二集", "air_date": "2026-11-12"},
                ]}

        get_res.return_value = Response()
        dates = ResourceFlow._douban_episode_air_dates("12345", 1)
        self.assertIsNone(dates.get(1))
        self.assertEqual(dates.get(2), "2026-11-12")

    @patch.object(ResourceFlow, "_recent_episode_douban_id")
    @patch.object(ResourceFlow, "_tmdb_web_episode_air_dates")
    @patch.object(ResourceFlow, "_tmdb_episode_air_date", return_value="2099-11-09")
    def test_season_list_missing_episode_uses_single_episode_api(self, single, web, douban):
        path = "有你的恋歌.Sing.for.You.S03E10.2026.2160p.MGTV.WEB-DL.H264.AAC-ADWeb.mkv"
        result = self.select_files([path], SimpleNamespace(tmdb_id=281511), [(8, "2026-11-02")])
        self.assertEqual(result["accepted"], {path})
        single.assert_called_once_with(281511, 3, 10)
        web.assert_not_called()
        douban.assert_not_called()

    @patch.object(ResourceFlow, "_recent_episode_douban_id")
    @patch.object(ResourceFlow, "_tmdb_web_episode_air_dates", return_value={35: "2099-11-09"})
    @patch.object(ResourceFlow, "_tmdb_episode_air_date", return_value=None)
    def test_null_api_date_uses_web_and_reuses_for_duplicate_file(self, single, web, douban):
        paths = [
            "Notes.of.DNA.Appraiser.S01E35.2160p.YOUKU.WEB-DL.AAC2.0.H.265-MWeb.mkv",
            "Notes.of.DNA.Appraiser.S01E35.1080p.YOUKU.WEB-DL.AAC2.0.H.264-MWeb.mkv",
        ]
        result = self.select_files(paths, SimpleNamespace(tmdb_id=259785), [(35, None)])
        self.assertEqual(result["accepted"], set(paths))
        single.assert_called_once_with(259785, 1, 35)
        web.assert_called_once_with(259785, 1)
        douban.assert_not_called()

    @patch.object(ResourceFlow, "_tmdb_web_episode_air_dates", return_value={1: "2099-11-09", 2: "2099-11-09"})
    @patch.object(ResourceFlow, "_tmdb_episode_air_date", return_value=None)
    def test_does_not_replace_known_old_dates_when_filling_missing_episodes(self, single, web):
        paths = ["Show.S01E01.mkv", "Show.S01E02.mkv", "Show.S01E01-E02.mkv"]
        result = self.select_files(paths, SimpleNamespace(tmdb_id=123), [(1, "2000-01-01"), (2, None)])
        self.assertEqual(result["accepted"], {paths[1]})

    @patch.object(ResourceFlow, "_tmdb_web_episode_air_dates", return_value={1: "2099-11-09", 2: "2099-11-09"})
    @patch.object(ResourceFlow, "_tmdb_episode_air_date")
    def test_multi_episode_file_uses_one_season_web_request(self, single, web):
        path = "Show.S01E01-E02.mkv"
        result = self.select_files([path], SimpleNamespace(tmdb_id=123))
        self.assertEqual(result["accepted"], {path})
        web.assert_called_once_with(123, 1)
        single.assert_not_called()

    @patch.object(ResourceFlow, "_recent_episode_douban_id", return_value="12345")
    @patch.object(ResourceFlow, "_douban_episode_air_dates", return_value={2: "2099-11-09"})
    @patch.object(ResourceFlow, "_tmdb_web_episode_air_dates", return_value={})
    @patch.object(ResourceFlow, "_tmdb_episode_air_date", return_value=None)
    def test_douban_fills_only_missing_dates_after_exact_name_lookup(self, single, web, douban, lookup):
        path = "Example.Series.2026.S01E01-E02.mkv"
        media = SimpleNamespace(tmdb_id=123, title="Example Series", year="2026")
        result = self.select_files([path], media, [(1, "2099-11-08")])
        self.assertEqual(result["accepted"], {path})
        lookup.assert_called_once_with("Example Series", "2026", 1)

    @patch.object(ResourceFlow, "_recent_episode_douban_id", return_value="")
    @patch.object(ResourceFlow, "_tmdb_web_episode_air_dates", return_value={})
    @patch.object(ResourceFlow, "_tmdb_episode_air_date", return_value=None)
    def test_all_sources_unavailable_still_excludes_file(self, single, web, douban):
        self.assertIsNone(self.select_files(["Show.S01E35.mkv"], SimpleNamespace(tmdb_id=123)))

    @patch.object(ResourceFlow, "_recent_episode_douban_id")
    @patch.object(ResourceFlow, "_tmdb_web_episode_air_dates")
    @patch.object(ResourceFlow, "_tmdb_episode_air_date")
    def test_episode_group_never_falls_back_to_default_numbering(self, single, web, douban):
        meta = SimpleNamespace(
            episode_list=[10], begin_season=3, end_season=None, episode_group="group"
        )
        with patch("resourceflow.MetaInfo", return_value=meta):
            self.assertIsNone(self.select_files(["Show.S03E10.mkv"], SimpleNamespace(tmdb_id=123)))
        single.assert_not_called()
        web.assert_not_called()
        douban.assert_not_called()

    @patch.object(ResourceFlow, "_tmdb_episode_air_date", return_value="2099-11-09")
    def test_special_season_zero_is_not_changed_to_season_one(self, single):
        path = "Show.S00E01.mkv"
        self.assertEqual(self.select_files([path], SimpleNamespace(tmdb_id=123))["accepted"], {path})
        single.assert_called_once_with(123, 0, 1)

    def test_web_dates_are_scoped_to_exact_tv_season_and_episode(self):
        def row(tmdb_id, season, episode, raw_date):
            return f"""<div class="episode">
                <a data-season-number="{season}" data-episode-number="{episode}"
                   href="/tv/{tmdb_id}/season/{season}/episode/{episode}?language=en-US">Episode</a>
                <span class="date">{raw_date}</span></div>"""

        html = '<span class="date">November 1, 2026</span>'
        html += row(259785, 1, 35, "November  9, 2026")
        html += row(259785, 1, 36, "November 10, 2026")
        html += row(259785, 1, 37, "")
        html += row(259785, 2, 38, "November 9, 2026")
        html += row(123, 1, 39, "November 9, 2026")
        html += row(259785, 1, 40, "Not available")
        self.assertEqual(ResourceFlow._parse_tmdb_web_episode_air_dates(html, 259785, 1), {
            35: "2026-11-09", 36: "2026-11-10",
        })

    @patch("resourceflow.TmdbApi")
    def test_single_episode_response_must_have_matching_season_and_episode(self, api):
        api.return_value.get_tv_episode_detail.return_value = {
            "season_number": 2, "episode_number": 10, "air_date": "2026-11-09"
        }
        self.assertIsNone(ResourceFlow._tmdb_episode_air_date(281511, 3, 10))

    @patch.object(RequestUtils, "get_res")
    def test_douban_lookup_requires_unambiguous_name_year_and_season(self, get_res):
        response = get_res.return_value
        response.ok = True
        response.json.return_value = [
            {"id": "1", "title": "有你的恋歌 第三季", "year": "2026"},
            {"id": "2", "title": "有你的恋歌 第二季", "year": "2026"},
            {"id": "3", "title": "其他恋歌 第三季", "year": "2026"},
            {"id": "4", "title": "有你的恋歌 第三季", "year": "2025"},
        ]
        self.assertEqual(ResourceFlow._recent_episode_douban_id("有你的恋歌", "2026", 3), "1")
        ResourceFlow._recent_episode_douban_id.cache_clear()
        response.json.return_value.append({"id": "5", "title": "有你的恋歌 第三季", "year": "2026"})
        self.assertEqual(ResourceFlow._recent_episode_douban_id("有你的恋歌", "2026", 3), "")

    def test_selection_excludes_nonmatching_files_before_starting(self):
        class Downloader:
            def __init__(self):
                self.calls = []

            def stop_torrents(self, torrent_hash):
                self.calls.append(("pause", torrent_hash))
                return True

            def get_files(self, torrent_hash):
                return [
                    {"id": 0, "name": "Show/Show.S01E01.mkv"},
                    {"id": 1, "name": "Show.S01E02.mkv"},
                    {"id": 2, "name": "Show.S01E03.srt"},
                ]

            def set_files(self, **kwargs):
                self.calls.append(("priority", kwargs["file_ids"], kwargs["priority"]))
                return True

            def start_torrents(self, torrent_hash):
                self.calls.append(("start", torrent_hash))
                return True

        downloader = Downloader()
        selected = ResourceFlow._select_recent_episode_files(
            downloader, "hash", {"accepted": {"Show.S01E01.mkv"}}, "qbittorrent", "test"
        )
        self.assertTrue(selected)
        self.assertEqual(downloader.calls, [
            ("pause", "hash"),
            ("priority", [1, 2], 0),
            ("priority", [0], 1),
            ("start", "hash"),
        ])

    def test_blacklist_requires_exact_title_and_other_selected_site(self):
        history = [
            SimpleNamespace(torrent_name="Show.S01E01.mkv", torrent_site="站点乙"),
            SimpleNamespace(torrent_name="Show.S01E01.mkv extra", torrent_site="站点乙"),
            SimpleNamespace(torrent_name="Show.S01E01.mkv", torrent_site="站点丙"),
        ]
        cache = {}
        with patch("resourceflow.DownloadHistoryOper") as history_oper:
            history_oper.return_value.query.return_value = (history, len(history))
            blocked, reason = ResourceFlow._is_blacklist_duplicate(
                "Show.S01E01.mkv", {"站点乙"}, cache
            )
        self.assertTrue(blocked)
        self.assertIn("站点乙", reason)
        self.assertEqual(history_oper.return_value.query.call_count, 1)

    def test_blacklist_normalizes_nfc_and_does_not_block_unselected_sites(self):
        history = [
            SimpleNamespace(torrent_name="Café.S01E01.mkv", torrent_site="当前站点"),
            SimpleNamespace(torrent_name="Café.S01E01.mkv", torrent_site="未选择站点"),
        ]
        with patch("resourceflow.DownloadHistoryOper") as history_oper:
            history_oper.return_value.query.return_value = (history, len(history))
            blocked, reason = ResourceFlow._is_blacklist_duplicate(
                "Cafe\u0301.S01E01.mkv", {"别的站点"}, {}
            )
        self.assertFalse(blocked)
        self.assertEqual(reason, "")

    def test_blacklist_query_failure_is_cached_and_excluded(self):
        cache = {}
        with patch("resourceflow.DownloadHistoryOper") as history_oper:
            history_oper.return_value.query.side_effect = RuntimeError("db unavailable")
            first = ResourceFlow._is_blacklist_duplicate("Show.mkv", {"站点乙"}, cache)
            second = ResourceFlow._is_blacklist_duplicate("Show.mkv", {"站点乙"}, cache)
        self.assertTrue(first[0])
        self.assertTrue(second[0])
        self.assertEqual(history_oper.return_value.query.call_count, 1)

    def test_blacklist_membership_follows_shared_task_sites(self):
        plugin = ResourceFlow.__new__(ResourceFlow)
        first = BrushTaskConfig({"site_id": 1, "spec_dedup_enabled": True})
        second = BrushTaskConfig({"site_id": 2, "spec_dedup_enabled": True})
        third = BrushTaskConfig({"site_id": 3, "spec_dedup_enabled": False, "blacklist_site_ids": [1, 3]})
        plugin._task_configs = {task.id: task for task in (first, second, third)}
        with patch("resourceflow.SiteOper") as sites:
            sites.return_value.get.return_value = SimpleNamespace(name="其他站")
            self.assertEqual(plugin._blacklist_other_site_names(first), set())
            plugin._global_torrent_blacklist_enabled = True
            self.assertEqual(plugin._blacklist_other_site_names(first), {"其他站"})
            self.assertEqual(plugin._blacklist_other_site_names(third), set())
            sites.return_value.get.assert_called_once_with(2)
            second.spec_dedup_enabled = False
            self.assertEqual(plugin._blacklist_other_site_names(first), set())
            self.assertNotIn("blacklist_site_ids", third.to_dict())


if __name__ == "__main__":
    unittest.main()

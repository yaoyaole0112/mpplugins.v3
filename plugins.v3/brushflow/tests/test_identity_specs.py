import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_channel_integration as integration
import test_inflight_dedup as inflight
from brushflow.channel_dedup import filename_title_aliases, long_title_prefix_matches, parse_candidate_file, same_release
from brushflow.models import ChannelDedupSettingsPayload


class SelectedSpecsTests(unittest.TestCase):
    def setUp(self):
        integration.ChannelIntegrationTests.setUp(self)
        self.plugin._channel_dedup_unknown_policy = "exclude"
        self.plugin._channel_dedup_attributes = ["resolution"]
        self.path = "Show.S01E01.1080p.mkv"
        self.record["specs"] = {
            "resolution": "1080p", "effects": "SDR", "codec": "H.264",
            "source": "WEB-DL", "fps": 24, "size": 1000,
        }

    def select(self, path=None, size=5000000000, accepted=None):
        return self.plugin._channel_dedup_file_selection(
            integration.make_torrent([(path or self.path, size)]), "Show S01", accepted
        )

    def test_resolution_only_ignores_all_missing_unselected_candidate_specs(self):
        result = self.select()
        self.assertEqual(result["excluded"], {self.path})
        self.assertEqual(result["missing_spec_count"], 0)
        self.plugin.chain.recognize_media.assert_not_called()

    def test_resolution_only_ignores_conflicting_unselected_values(self):
        path = "Show.S01E01.1080p.DV.H265.BluRay.60FPS.mkv"
        result = self.select(path)
        self.assertEqual(result["matched_video_count"], 1)
        self.assertEqual(result["missing_spec_count"], 0)

    def test_resolution_only_ignores_unknown_unselected_record_specs(self):
        self.record["specs"] = {"resolution": "1080p"}
        self.assertIsNotNone(self.select("Show.S01E01.1080p.HDR10.H265.mkv"))

    def test_resolution_only_without_a_record_or_with_different_resolution_downloads(self):
        self.record["specs"]["resolution"] = "720p"
        self.assertIsNone(self.select())
        self.plugin._channel_dedup_state["records"] = []
        self.assertIsNone(self.select())
        self.assertIsNone(self.select("Show.S01E01.1080p.H265.mkv"))

    def test_resolution_only_no_record_adds_despite_missing_unselected_fields(self):
        self.plugin._channel_dedup_state["records"] = []
        self.content = integration.make_torrent([(self.path, 1000)])
        downloader, selection = integration.ChannelIntegrationTests.prepare_download(self)
        self.assertEqual(self.plugin._BrushFlow__download(self.torrent), "a" * 40)
        downloader.add_torrent.assert_called_once()
        selection.assert_not_called()

    def test_resolution_only_record_match_stops_add_despite_other_fields_differing(self):
        self.content = integration.make_torrent([("Show.S01E01.1080p.DV.H265.BluRay.60FPS.mkv", 5000000000)])
        downloader, selection = integration.ChannelIntegrationTests.prepare_download(self)
        self.assertIsNone(self.plugin._BrushFlow__download(self.torrent))
        downloader.add_torrent.assert_not_called()
        selection.assert_not_called()

    def test_resolution_only_still_requires_same_identity_season_and_episode(self):
        for path in (
            "Different.S01E01.1080p.mkv", "Show.S02E01.1080p.mkv", "Show.S01E02.1080p.mkv",
        ):
            with self.subTest(path=path):
                self.assertIsNone(self.select(path))

    def test_resolution_only_can_resolve_cross_language_identity_without_other_specs(self):
        self.record["title_key"] = "中文剧名"
        self.record["tmdb_id"] = 123
        self.plugin.chain.recognize_media.return_value = SimpleNamespace(
            title="中文剧名", en_title="Show", tmdb_id=123, type="电视剧",
        )
        self.assertIsNotNone(self.select())

    def test_selected_missing_effect_excludes_even_when_no_channel_records_exist(self):
        self.plugin._channel_dedup_attributes = ["resolution", "effects"]
        self.plugin._channel_dedup_state["records"] = []
        path = "Show.S01E01.2160p.H265.mkv"
        with patch("brushflow.logger") as log:
            result = self.select(path)
        self.assertEqual(result["excluded"], {path})
        self.assertEqual(result["missing_spec_count"], 1)
        self.assertEqual(result["matched_video_count"], 0)
        self.assertTrue(any("不是已发布重复" in call.args[0] for call in log.info.call_args_list))
        self.plugin.chain.recognize_media.assert_not_called()

    def test_default_without_policy_attribute_is_strict_not_legacy_allow(self):
        del self.plugin._channel_dedup_unknown_policy
        self.assertEqual(self.select("Show.S01E01.H265.mkv")["missing_spec_count"], 1)

    def test_unknown_unselected_effect_allows_when_no_record_exists(self):
        self.plugin._channel_dedup_state["records"] = []
        self.assertIsNone(self.select("Show.S01E01.2160p.H265.mkv"))

    def test_compatibility_allow_policy_keeps_unknown_selected_specs_unmatched(self):
        self.plugin._channel_dedup_attributes = ["resolution", "effects"]
        self.plugin._channel_dedup_unknown_policy = "allow"
        self.assertIsNone(self.select("Show.S01E01.1080p.H265.mkv"))

    def test_record_missing_selected_spec_is_not_proof_of_a_duplicate(self):
        self.plugin._channel_dedup_attributes = ["resolution", "effects"]
        self.record["specs"]["effects"] = None
        self.assertIsNone(self.select("Show.S01E01.1080p.SDR.H265.mkv"))

    def test_no_attributes_disables_both_unknown_filter_and_dedup(self):
        self.plugin._channel_dedup_attributes = []
        self.assertIsNone(self.select("Show.S01E01.mkv"))

    def test_pack_excludes_missing_selected_specs_but_keeps_known_new_episode(self):
        self.plugin._channel_dedup_attributes = ["resolution", "effects"]
        unknown = "Show.S01E02.2160p.H265.mkv"
        known = "Show.S01E03.2160p.SDR.H265.mkv"
        result = self.plugin._channel_dedup_file_selection(
            integration.make_torrent([(unknown, 1000), (known, 2000)]), "Show S01"
        )
        self.assertEqual(result["excluded"], {unknown})
        self.assertEqual(result["remaining_paths"], {known})
        self.assertEqual(result["missing_spec_count"], 1)

    def test_strict_unknown_is_filtered_before_downloader_add_and_not_logged_as_published(self):
        self.plugin._channel_dedup_attributes = ["resolution", "effects"]
        self.content = integration.make_torrent([("Show.S01E01.2160p.H265.mkv", 1000)])
        downloader, _ = integration.ChannelIntegrationTests.prepare_download(self)
        with patch("brushflow.logger") as log:
            self.assertIsNone(self.plugin._BrushFlow__download(self.torrent))
        downloader.add_torrent.assert_not_called()
        self.assertTrue(any("同规格重复 0 个，缺少勾选规格 1 个" in call.args[0] for call in log.info.call_args_list))
        self.assertFalse(any("均已在监听频道发布" in call.args[0] for call in log.info.call_args_list))

    def test_only_recently_selected_files_are_subject_to_missing_specs_policy(self):
        unknown = "Show.S01E02.2160p.H265.mkv"
        known = "Show.S01E03.2160p.SDR.H265.mkv"
        self.plugin._channel_dedup_attributes = ["resolution", "effects"]
        content = integration.make_torrent([(unknown, 1000), (known, 2000)])
        self.assertIsNone(self.plugin._channel_dedup_file_selection(content, "Show S01", {known}))


class IdentityEvidenceTests(unittest.TestCase):
    def setUp(self):
        integration.ChannelIntegrationTests.setUp(self)

    def identity(self, path, media):
        self.plugin.chain.recognize_media.return_value = SimpleNamespace(type="电视剧", **media)
        return self.plugin._channel_candidate_tmdb_identity(path, path)

    def test_bracketed_chinese_title_repairs_english_alias_mismatch(self):
        path = "[十六夜膳房].The.Kitchen.of.16.Nights.2026.S01E05.1080p.WEB-DL.H264.mkv"
        result = self.identity(path, {
            "tmdb_id": 334351, "title": "十六夜膳房", "en_title": "The Sixteenth Night Kitchen",
            "year": "2026", "names": ["The 16th Night Kitchen"],
        })
        self.assertEqual(result, 334351)
        self.assertEqual(self.plugin._channel_candidate_tmdb_identity(path, path), 334351)
        self.plugin.chain.recognize_media.assert_called_once()

    def test_bracketed_chinese_title_repairs_romanization_difference(self):
        path = "[由你主动吻我吧].Omae.no.ho.kara.kisu.shite.kure.yo.2026.S01E04.1080p.mkv"
        self.assertEqual(self.identity(path, {
            "tmdb_id": 330370, "title": "由你主动吻我吧", "year": "2026",
            "names": ["Omae no Hou Kara Kiss Shite Kure yo"],
        }), 330370)

    def test_long_truncation_requires_full_word_boundary_and_matching_year(self):
        path = "Tsuihou.Sareta.Cheat.Fuyo.Majutsushi.wa.Kimama.na.Second.S01E06.2026.1080p.mkv"
        media = {
            "tmdb_id": 313346, "title": "被放逐的作弊赋予魔术师享受随心所欲的第二人生。",
            "year": "2026",
            "names": ["Tsuihou Sareta Cheat Fuyo Majutsushi wa Kimamana Second Life wo Ouka Suru"],
        }
        self.assertEqual(self.identity(path, media), 313346)
        self.plugin._channel_identity_cache.clear()
        self.assertIsNone(self.identity(path, {**media, "year": "2025"}))

    def test_short_prefix_and_truncated_word_are_not_validated_as_aliases(self):
        self.assertFalse(long_title_prefix_matches("The Light", ["The Light of Life"]))
        self.assertFalse(long_title_prefix_matches(
            "Tsuihou Sareta Cheat Fuyo Majutsushi wa Kimama na Secon",
            ["Tsuihou Sareta Cheat Fuyo Majutsushi wa Kimamana Second Life"],
        ))

    def test_long_prefix_without_year_needs_manual_binding(self):
        path = "Tsuihou.Sareta.Cheat.Fuyo.Majutsushi.wa.Kimama.na.Second.S01E06.1080p.mkv"
        self.assertIsNone(self.identity(path, {
            "tmdb_id": 313346, "title": "被放逐的作弊赋予魔术师",
            "year": "2026", "names": ["Tsuihou Sareta Cheat Fuyo Majutsushi wa Kimamana Second Life"],
        }))

    def test_conflicting_chinese_title_cannot_be_overridden_by_an_english_match(self):
        self.assertIsNone(self.identity("[华灯起].Light.the.Night.2026.S01E01.1080p.mkv", {
            "tmdb_id": 130330, "title": "华灯初上", "en_title": "Light the Night", "year": "2026",
        }))

    def test_subtitle_language_and_release_tags_are_not_treated_as_chinese_titles(self):
        self.assertEqual(filename_title_aliases("[简繁字幕].Show.S01E01.2026.1080p.mkv"), {"Show"})
        self.assertEqual(self.identity("[简繁字幕].Show.S01E01.2026.1080p.mkv", {
            "tmdb_id": 123, "title": "中文剧名", "en_title": "Show", "year": "2026",
        }), 123)

    def test_first_season_remake_year_conflict_is_rejected_but_later_season_year_is_allowed(self):
        media = {"tmdb_id": 123, "title": "Show", "year": "2025"}
        self.assertIsNone(self.identity("Show.2026.S01E01.1080p.mkv", media))
        self.assertEqual(self.identity("Show.2026.S02E01.1080p.mkv", media), 123)

    def test_manual_alias_binding_preempts_cached_miss_without_network(self):
        path = "Short.Roman.Title.2026.S01E01.1080p.mkv"
        self.assertIsNone(self.identity(path, {"tmdb_id": 123, "title": "Different", "year": "2026"}))
        self.plugin.chain.recognize_media.reset_mock()
        self.plugin._channel_dedup_identity_aliases = {"Short Roman Title": 313346}
        self.assertEqual(self.plugin._channel_candidate_tmdb_identity(path, path), 313346)
        self.plugin.chain.recognize_media.assert_not_called()

    def test_conflicting_bindings_do_not_guess_between_two_series(self):
        path = "[中文剧名].Show.2026.S01E01.1080p.mkv"
        self.plugin._channel_dedup_identity_aliases = {"中文剧名": 123, "Show": 456}
        self.assertIsNone(self.plugin._channel_candidate_tmdb_identity(path, path))
        self.assertIsNone(self.plugin._channel_dedup_file_selection(integration.make_torrent([(path, 1000)]), path))
        self.plugin.chain.recognize_media.assert_not_called()

    def test_explicit_tmdb_id_cannot_be_overridden_by_alias_binding(self):
        self.plugin._channel_dedup_attributes = ["resolution"]
        self.record["tmdb_id"] = 200
        self.plugin._channel_dedup_identity_aliases = {"Show": 200}
        path = "Show.(tmdbid=100).S01E01.1080p.mkv"
        self.assertIsNone(self.plugin._channel_dedup_file_selection(integration.make_torrent([(path, 1000)]), path))

    def test_chinese_filename_evidence_can_match_a_channel_post_without_tmdb_id(self):
        candidate = parse_candidate_file("[十六夜膳房].The.Kitchen.of.16.Nights.2026.S01E05.1080p.mkv", 1000)
        record = {**candidate, "title_key": "十六夜膳房", "title_keys": []}
        self.assertTrue(same_release(candidate, record, ["resolution"]))
        self.assertFalse(same_release(candidate, {**record, "year": 2025}, ["resolution"]))


class IdentitySettingsTests(unittest.TestCase):
    def test_new_settings_default_to_exclude_and_accept_optional_bindings(self):
        config = ChannelDedupSettingsPayload()
        self.assertEqual(config.unknown_policy, "exclude")
        self.assertEqual(config.identity_aliases, {})
        config = ChannelDedupSettingsPayload(identity_aliases="Short Roman Title = 313346\n十六夜膳房＝334351")
        self.assertEqual(config.identity_aliases, {"Short Roman Title": 313346, "十六夜膳房": 334351})

    def test_invalid_policy_and_conflicting_or_malformed_bindings_are_rejected(self):
        for value in (
            {"unknown_policy": "guess_sdr"}, {"identity_aliases": "Show: 123"},
            {"identity_aliases": "Show = 0"}, {"identity_aliases": {"Show": True}},
            {"identity_aliases": "Show = 123\nS.h.o.w = 456"},
            {"identity_aliases": {f"Show {n}": n + 1 for n in range(201)}},
        ):
            with self.subTest(value=str(value)[:80]), self.assertRaises(ValueError):
                ChannelDedupSettingsPayload(**value)

    def test_settings_round_trip_clears_identity_cache_but_preserves_channel_records(self):
        integration.ChannelIntegrationTests.setUp(self)
        self.plugin._save_config = lambda: None
        self.plugin._refresh_scheduler = lambda: None
        self.plugin._channel_identity_cache["old"] = (123, 456)
        before = copy.deepcopy(self.plugin._channel_dedup_state)
        with patch("brushflow.ThreadHelper"):
            response = self.plugin.update_channel_dedup_settings(ChannelDedupSettingsPayload(
                enabled=True, channels=["@channel"], attributes=["resolution"],
                unknown_policy="exclude", identity_aliases="Show = 123",
            ))
        self.assertTrue(response.success)
        self.assertEqual(self.plugin._channel_identity_cache, {})
        self.assertEqual(self.plugin._channel_dedup_state, before)
        payload = self.plugin._channel_dedup_payload()
        self.assertEqual(payload["unknown_policy"], "exclude")
        self.assertEqual(payload["identity_aliases"], {"Show": 123})
        saved = self.plugin._current_config()
        self.assertEqual(saved["channel_dedup_unknown_policy"], "exclude")
        self.assertEqual(saved["channel_dedup_identity_aliases"], {"Show": 123})


class SeasonTitleEvidenceTests(unittest.TestCase):
    def setUp(self):
        integration.ChannelIntegrationTests.setUp(self)
        self.plugin._channel_dedup_attributes = ["resolution"]
        self.plugin._channel_dedup_channels = ["@ms20260601"]
        self.record.update({
            "channel": "@ms20260601", "title": "茜茜皇后", "title_key": "茜茜皇后",
            "tmdb_id": 131488, "year": 2022, "season": 3, "episodes": [4],
            "specs": {"resolution": "1080p", "effects": None, "codec": "H.264",
                      "source": "WEB-DL", "fps": 24, "size": 2242148366},
        })
        self.plugin.chain.recognize_media.return_value = SimpleNamespace(
            type="电视剧", title="茜茜皇后", en_title="The Empress",
            original_title="Die Kaiserin", names=["茜茜皇后", "The Empress"],
            year="2022", tmdb_id=131488,
        )

    def path(self, prefix="[茜茜皇后 第三季].", season="S03"):
        return f"{prefix}The.Empress.2026.{season}E04.1080p.NF.WEB-DL.H264.DDP5.1.Atmos-UBWEB.mkv"

    def select(self, path):
        return self.plugin._channel_dedup_file_selection(
            integration.make_torrent([(path, 2242142161)]), "The Empress 2026 S03 Complete 1080p"
        )

    def test_resolution_only_replay_ignores_audio_size_effects_and_later_season_year(self):
        for prefix in ("", "[茜茜皇后 第三季].", "[茜茜皇后 第3季]."):
            with self.subTest(prefix=prefix):
                result = self.select(self.path(prefix))
                self.assertIsNotNone(result)
                self.assertEqual(result["matched_video_count"], 1)

    def test_season_title_match_blocks_before_downloader_add_without_inflight_help(self):
        self.content = integration.make_torrent([(self.path(), 2242142161)])
        downloader, _ = integration.ChannelIntegrationTests.prepare_download(self)
        self.assertIsNone(self.plugin._BrushFlow__download(self.torrent))
        downloader.add_torrent.assert_not_called()

    def test_different_season_suffix_is_not_silently_discarded(self):
        self.assertIsNone(self.select(self.path("[茜茜皇后 第四季].")))

    def test_suffix_with_extra_title_text_is_not_discarded(self):
        self.assertIsNone(self.select(self.path("[茜茜皇后 第三季 续篇].")))

    def test_wrong_chinese_base_cannot_be_overridden_by_english_match(self):
        self.assertIsNone(self.select(self.path("[华灯起 第三季].")))

    def test_different_season_or_episode_is_not_duplicate(self):
        self.assertIsNone(self.select(self.path(season="S04")))
        self.assertIsNone(self.select(self.path().replace("E04", "E05")))

    def test_different_resolution_is_not_duplicate(self):
        self.assertIsNone(self.select(self.path().replace("1080p", "2160p")))

    def test_season_suffix_alias_binding_still_works_without_recognition(self):
        self.plugin._channel_dedup_identity_aliases = {"茜茜皇后 第三季": 131488}
        self.assertIsNotNone(self.select(self.path()))
        self.plugin.chain.recognize_media.assert_not_called()

    def test_explicit_conflicting_id_still_wins_over_season_title(self):
        self.assertIsNone(self.select(self.path().replace("S03E04", "(tmdbid=999).S03E04")))


class QueueSelectedSpecsTests(unittest.TestCase):
    def setUp(self):
        inflight.InflightDedupTests.setUp(self)
        self.plugin._channel_dedup_unknown_policy = "exclude"

    def enqueue(self, *args, **kwargs):
        return inflight.InflightDedupTests.enqueue(self, *args, **kwargs)

    def scope(self, *args, **kwargs):
        return inflight.InflightDedupTests.scope(self, *args, **kwargs)

    def save(self, *args):
        return inflight.InflightDedupTests.save(self, *args)

    def test_resolution_only_queue_match_ignores_unknown_effects_and_other_specs(self):
        self.plugin._channel_dedup_attributes = ["resolution"]
        self.assertFalse(self.enqueue(path="Show.S01E01.1080p.mkv")["blocked"])
        self.assertTrue(self.enqueue("b" * 40, "Show.S01E01.1080p.HDR10.H265.BluRay.60FPS.mkv")["blocked"])

    def test_strict_unknown_is_not_reserved_or_added_even_with_empty_queue(self):
        result = self.enqueue(path="Show.S01E01.2160p.H265.mkv")
        self.assertTrue(result["blocked"])
        self.assertEqual(self.plugin._inflight_entries, {})

    def test_verified_manual_aliases_match_across_different_queued_title_spellings(self):
        self.plugin._channel_dedup_attributes = ["resolution"]
        self.plugin._channel_dedup_identity_aliases = {"Short Roman": 123, "Different Title": 123}
        self.assertFalse(self.enqueue(path="Short.Roman.S01E01.1080p.mkv")["blocked"])
        self.assertTrue(self.enqueue("b" * 40, "Different.Title.S01E01.1080p.mkv")["blocked"])
        records = next(iter(self.plugin._inflight_entries.values()))["records"]
        self.assertEqual(records[0]["tmdb_id"], 123)
        self.plugin.chain.recognize_media.assert_not_called()

    def test_editing_a_binding_updates_derived_queued_identity_before_matching(self):
        self.plugin._channel_dedup_attributes = ["resolution"]
        self.plugin._channel_dedup_identity_aliases = {"Short Roman": 123}
        self.assertFalse(self.enqueue(path="Short.Roman.S01E01.1080p.mkv")["blocked"])
        self.plugin._channel_dedup_identity_aliases = {"Short Roman": 456, "Different Title": 456}
        self.assertTrue(self.enqueue("b" * 40, "Different.Title.S01E01.1080p.mkv")["blocked"])
        records = next(iter(self.plugin._inflight_entries.values()))["records"]
        self.assertEqual(records[0]["tmdb_id"], 456)

    def test_editing_aliases_never_overwrites_an_explicit_file_tmdb_id(self):
        self.plugin._channel_dedup_attributes = ["resolution"]
        self.assertFalse(self.enqueue(path="Short.Roman.(tmdbid=123).S01E01.1080p.mkv")["blocked"])
        self.plugin._channel_dedup_identity_aliases = {"Short Roman": 456, "Different Title": 456}
        self.assertFalse(self.enqueue("b" * 40, "Different.Title.S01E01.1080p.mkv")["blocked"])
        records = self.plugin._inflight_entries[self.plugin._inflight_key("QB", "a" * 40)]["records"]
        self.assertEqual(records[0]["tmdb_id"], 123)


if __name__ == "__main__":
    unittest.main()

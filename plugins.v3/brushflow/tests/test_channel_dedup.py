import unittest

from brushflow.channel_dedup import (
    parse_candidate_file,
    parse_channel_message,
    parse_channel_messages,
    parse_episode,
    parse_specs,
    parse_torrent_files,
    repair_record_frame_rates,
    same_release,
)


def bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, str):
        encoded = value.encode()
        return str(len(encoded)).encode() + b":" + encoded
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        items = sorted(value.items())
        return b"d" + b"".join(bencode(key) + bencode(item) for key, item in items) + b"e"
    raise TypeError(value)


class ChannelDedupParsingTests(unittest.TestCase):
    def setUp(self):
        self.message = """✅新片送达–请您检阅
🎬 影视：PSYREN 决战游戏 ([https://t.me/Re20260601](https://t.me/Re20260601))
📺 季集：S01E05 ([https://t.me/Re20260601](https://t.me/Re20260601))
📦 质量：1080p SDR H264 AAC
🎞 帧率：24FPS
💾 大小：1.2GB
📅 年份：2026
⭐ 评分：7.2 / 10 ｜ ID：297903
ed2k://|file|PSYREN -决战游戏-(2026)(tmdbid=297903).S01E05.WEB-DL.1080p.SDR.H264.8bit.AAC.24帧-RMGroup.mkv|1251017826|908330eb04e6dfb75455bb85a117ee11|/
"""

    def test_parses_sample_message_without_retaining_raw_text(self):
        record = parse_channel_message("@Re20260601", 42, 1794339396, self.message)
        self.assertEqual(record["title"], "PSYREN 决战游戏")
        self.assertEqual(record["title_key"], "psyren决战游戏")
        self.assertEqual(record["tmdb_id"], 297903)
        self.assertEqual((record["season"], record["episodes"]), (1, [5]))
        self.assertEqual(record["specs"], {
            "resolution": "1080p", "effects": "SDR", "codec": "H.264", "source": "WEB-DL",
            "fps": 24.0, "size": 1251017826,
        })
        self.assertNotIn("text", record)

    def test_same_selected_attributes_and_episode_match(self):
        record = parse_channel_message("@channel", 1, 1794339396, self.message)
        candidate = parse_candidate_file(
            "PSYREN -决战游戏-(2026)(tmdbid=297903).S01E05.WEB-DL.1080p.SDR.H264.8bit.AAC.24帧-RMGroup.mkv",
            1251017826,
        )
        self.assertTrue(same_release(candidate, record, ["resolution", "effects", "codec", "source", "fps", "size"]))
        candidate["specs"]["codec"] = "H.265"
        self.assertFalse(same_release(candidate, record, ["resolution", "codec"]))

    def test_unknown_selected_attribute_fails_open(self):
        record = parse_channel_message("@channel", 1, 1794339396, self.message)
        candidate = parse_candidate_file("PSYREN 决战游戏.S01E05.1080p.WEB-DL.H264.mkv", 1000)
        self.assertFalse(same_release(candidate, record, ["resolution", "fps"]))
        self.assertTrue(same_release(candidate, record, ["resolution", "source", "codec"]))

    def test_tmdb_identity_episode_and_size_tolerance_are_checked(self):
        record = parse_channel_message("@channel", 1, 1794339396, self.message)
        candidate = parse_candidate_file("Different.Title.(tmdbid=297903).S01E05.1080p.WEB-DL.H264.24FPS.mkv", 1300000000)
        self.assertTrue(same_release(candidate, record, ["resolution", "codec"], 5))
        self.assertTrue(same_release(candidate, record, ["size"], 5))
        self.assertFalse(same_release(candidate, record, ["size"], 2))
        candidate["tmdb_id"] = 999
        self.assertFalse(same_release(candidate, record, ["resolution"]))

    def test_episode_single_and_range_formats(self):
        self.assertEqual(parse_episode("Show.S03E10-E12.mkv"), (3, [10, 11, 12]))
        self.assertEqual(parse_episode("S01E05E06"), (1, [5, 6]))
        self.assertEqual(parse_episode("第2季第9集"), (2, [9]))
        self.assertEqual(parse_episode("Show.1080p.mkv"), (None, []))

    def test_reads_per_file_lengths_from_multifile_torrent(self):
        torrent = bencode({"info": {
            "files": [
                {"length": 100, "path": [b"Show.S01E01.1080p.WEB-DL.H264.mkv"]},
                {"length": 200, "path": [b"Show.S01E02.1080p.WEB-DL.H264.mkv"]},
            ],
            "name": b"Show",
            "piece length": 16384,
            "pieces": b"a" * 20,
        }})
        self.assertEqual(parse_torrent_files(torrent), [
            {"path": "Show.S01E01.1080p.WEB-DL.H264.mkv", "size": 100},
            {"path": "Show.S01E02.1080p.WEB-DL.H264.mkv", "size": 200},
        ])

    def test_rejects_invalid_torrent_without_throwing(self):
        self.assertEqual(parse_torrent_files(b"not a torrent"), [])

    def test_filename_attributes_override_torrent_title(self):
        candidate = parse_candidate_file(
            "Show.S01E01.1080p.H264.SDR.24FPS.WEB-DL.mkv", 1024,
            "Show.S01.2160p.H265.HDR10.60FPS.BluRay",
        )
        self.assertEqual(candidate["specs"], {
            "resolution": "1080p", "effects": "SDR", "codec": "H.264", "source": "WEB-DL",
            "fps": 24.0, "size": 1024,
        })

    def test_conventional_avc_web_candidate_without_effect_label_matches_sdr(self):
        record = parse_channel_message(
            "@ms20260601", 9997, 1,
            "影视：换母疑云\n季集：S01E05\n质量：1080p SDR H264\n"
            "ed2k://|file|换母疑云(2026)(tmdbid=275139).S01E05.WEB-DL.1080p."
            f"SDR.H264.8bit.DDP.5.1.24帧-RMGroup.mkv|1109637998|{'a' * 32}|/",
        )
        candidate = parse_candidate_file(
            "Phony.(tmdbid=275139).S01E05.2026.1080p.DSNP.WEB-DL.H.264.DDP5.1-HHWEB.mkv",
            1109643658, "Phony S01 2026 1080p DSNP WEB-DL H.264 DDP5.1-HHWEB",
        )
        self.assertEqual(candidate["specs"]["effects"], "SDR")
        self.assertEqual(candidate["inferred_attributes"], ["effects"])
        self.assertTrue(same_release(candidate, record, ["resolution", "effects"]))

    def test_screenshot_releases_match_despite_audio_spelling_and_size_difference(self):
        first = "换母疑云(2026)(tmdbid=275139).S01E05.WEB-DL.1080p.SDR.H264.8bit.DDP.5.1.24帧-RMGroup.mkv"
        second = first.replace("DDP.", "DD+.")
        record = parse_channel_message(
            "@ms20260601", 9997, 1794299719,
            f"影视：换母疑云\n季集：S01E05\ned2k://|file|{first}|1109637998|{'a' * 32}|/",
        )
        candidate = parse_candidate_file(second, 1109643658)
        self.assertTrue(same_release(candidate, record, ["resolution", "effects"]))
        self.assertEqual(candidate["inferred_attributes"], [])

    def test_unselected_size_codec_source_and_fps_do_not_affect_matching(self):
        record = parse_channel_message("@channel", 1, 1, self.message)
        candidate = parse_candidate_file(
            "Different.(tmdbid=297903).S01E05.1080p.SDR.H265.BluRay.60FPS.mkv", 2000000000,
        )
        self.assertTrue(same_release(candidate, record, ["resolution", "effects"]))
        for attribute in ("size", "codec", "source", "fps"):
            with self.subTest(attribute=attribute):
                self.assertFalse(same_release(candidate, record, ["resolution", "effects", attribute]))

    def test_description_specs_fill_gaps_without_becoming_identity_or_overriding_filename(self):
        candidate = parse_candidate_file(
            "Show.S01E01.1080p.H265.WEB-DL.mkv", 1024, "Show S01",
            spec_hint="Other Show (tmdbid=999) 2025 2160p SDR H264 BluRay 25FPS",
        )
        self.assertEqual(candidate["specs"], {
            "resolution": "1080p", "effects": "SDR", "codec": "H.265", "source": "WEB-DL",
            "fps": 25, "size": 1024,
        })
        self.assertEqual(candidate["inferred_attributes"], [])
        self.assertEqual(candidate["title_key"], "show")
        self.assertIsNone(candidate["tmdb_id"])
        self.assertIsNone(candidate["year"])

    def test_explicit_effect_labels_take_priority_over_sdr_inference(self):
        for effect in ("HDR", "HDR10", "HDR10+", "DV", "HLG", "SDR"):
            with self.subTest(effect=effect):
                candidate = parse_candidate_file(
                    f"Show.S01E01.1080p.H264.{effect}.WEB-DL.mkv", 1,
                    spec_hint="SDR",
                )
                self.assertEqual(candidate["specs"]["effects"], effect)
                self.assertEqual(candidate["inferred_attributes"], [])

    def test_sdr_inference_requires_conventional_avc_web_specs(self):
        for quality in (
            "2160p.H264.WEB-DL", "1440p.H264.WEB-DL", "1080p.H265.WEB-DL",
            "1080p.AV1.WEB-DL", "1080p.H264.BluRay", "1080p.WEB-DL",
            "H264.WEB-DL", "1080p.H264",
        ):
            with self.subTest(quality=quality):
                candidate = parse_candidate_file(f"Show.S01E01.{quality}.mkv", 1)
                self.assertIsNone(candidate["specs"]["effects"])
                self.assertEqual(candidate["inferred_attributes"], [])
        for quality in ("1080p.H264.WEB-DL", "720p.x264.WEBRip", "480p.AVC.WEB"):
            with self.subTest(quality=quality):
                self.assertEqual(
                    parse_candidate_file(f"Show.S01E01.{quality}.mkv", 1)["specs"]["effects"], "SDR",
                )

    def test_hdr_or_high_bit_depth_hints_block_sdr_inference(self):
        for marker in (
            "10bit", "10-bit", "10 bit", "Hi10P", "High10", "High 10",
            "12bit", "12-bit", "HDRVivid", "HDR.Vivid", "HLG10", "DoVi8",
            "BT.2020", "Rec2020", "PQ", "EDR", "MAXPLUS", "SMPTE.ST.2084",
        ):
            for location in ("filename", "title", "description"):
                with self.subTest(marker=marker, location=location):
                    candidate = parse_candidate_file(
                        f"Show.S01E01.1080p.H264.WEB-DL.{marker if location == 'filename' else ''}.mkv",
                        1, marker if location == "title" else "",
                        spec_hint=marker if location == "description" else "",
                    )
                    self.assertNotEqual(candidate["specs"]["effects"], "SDR")
                    self.assertEqual(candidate["inferred_attributes"], [])

    def test_plain_tmdb_id_is_only_used_on_tv_template(self):
        message = "影视：Show\n季集：S01E01\n质量：1080p H264\n类型：剧集\n评分：7.2 ｜ ID：297903"
        self.assertEqual(parse_channel_message("@channel", 1, 1, message)["tmdb_id"], 297903)
        self.assertIsNone(parse_channel_message("@channel", 1, 1, message.replace("类型：剧集", ""))["tmdb_id"])

    def test_multiple_ed2k_files_in_one_post_keep_independent_episodes_and_sizes(self):
        message = "\n".join(
            f"ed2k://|file|Show.S01E{number:02d}.1080p.H264.mkv|{1000 * number}|{'a' * 32}|/"
            for number in (1, 2)
        )
        records = parse_channel_messages("@channel", 1, 1, message)
        self.assertEqual([record["episodes"] for record in records], [[1], [2]])
        self.assertEqual([record["specs"]["size"] for record in records], [1000, 2000])

    def test_no_selected_attribute_never_excludes(self):
        record = parse_channel_message("@channel", 1, 1, self.message)
        self.assertFalse(same_release(record, record, []))

    def test_same_name_different_known_year_is_not_a_duplicate(self):
        record = parse_channel_message("@channel", 1, 1, self.message)
        candidate = parse_candidate_file("PSYREN 决战游戏.2025.S01E05.1080p.H264.mkv", 1)
        self.assertFalse(same_release(candidate, record, ["resolution"]))

    def test_audio_channel_layout_does_not_become_part_of_frame_rate(self):
        for audio in ("AAC2.0", "AAC.2.0", "DDP5.1", "DDP.5.1", "DD+5.1",
                      "AC3.2.0", "E-AC-3.5.1", "EAC3.Dolby.5.1", "DTS-HD.MA.5.1",
                      "TrueHD.Atmos.7.1", "TrueHD.Dolby.Atmos.7.1"):
            for rate in (24, 25, 30, 50, 60):
                with self.subTest(audio=audio, rate=rate):
                    self.assertEqual(parse_specs(f"Show.S01E11.H264.{audio}.{rate}FPS.mkv")["fps"], rate)

    def test_decimal_frame_rates_are_not_truncated_or_multiplied(self):
        for rate in (0.25, 1.25, 23.976, 29.97, 59.94):
            for prefix in ("", "AAC.2.0.", "DDP5.1."):
                with self.subTest(rate=rate, prefix=prefix):
                    self.assertEqual(parse_specs(f"{prefix}{rate}FPS")["fps"], rate)

    def test_channel_post_and_candidate_use_correct_fps_after_audio_layout(self):
        filename = "今天是太阳雨(2026)(tmdbid=307853).S01E11.WEB-DL.1080p.H264.8bit.AAC.2.0.25帧-RMGroup.mkv"
        message = f"影视：今天是太阳雨\n季集：S01E11\n帧率：25FPS\ned2k://|file|{filename}|2226332870|{'a' * 32}|/"
        record = parse_channel_message("@ms20260601", 42, 1, message)
        candidate = parse_candidate_file(filename, 2226332870)
        self.assertEqual(record["specs"]["fps"], 25)
        self.assertEqual(candidate["specs"]["fps"], 25)
        self.assertTrue(same_release(candidate, record, ["resolution", "codec", "source", "fps", "size"]))

    def test_persisted_frame_rate_repair_is_idempotent_and_preserves_other_fields(self):
        record = parse_channel_message("@channel", 1, 1, self.message)
        record["file_key"] = "Show.S01E05.H264.AAC.2.0.25帧.mkv"
        record["specs"]["fps"] = 0.25
        original = {**record, "specs": {**record["specs"]}}
        self.assertEqual(repair_record_frame_rates([record]), 1)
        self.assertEqual(record["specs"]["fps"], 25)
        self.assertEqual(record, {**original, "specs": {**original["specs"], "fps": 25}})
        self.assertEqual(repair_record_frame_rates([record]), 0)

    def test_persisted_repair_keeps_template_fps_when_filename_has_no_fps(self):
        records = [
            {"file_key": "Show.S01E01.H264.AAC2.0.mkv", "specs": {"fps": 25}},
            {"file_key": "", "specs": {"fps": 25}},
            {"file_key": "Show.S01E01.0.25FPS.mkv", "specs": {"fps": 0.25}},
        ]
        self.assertEqual(repair_record_frame_rates(records), 0)
        self.assertEqual([record["specs"]["fps"] for record in records], [25, 25, 0.25])


if __name__ == "__main__":
    unittest.main()

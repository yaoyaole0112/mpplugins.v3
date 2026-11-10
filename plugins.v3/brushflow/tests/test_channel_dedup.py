import unittest

from brushflow.channel_dedup import (
    parse_candidate_file,
    parse_channel_message,
    parse_channel_messages,
    parse_episode,
    parse_torrent_files,
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


if __name__ == "__main__":
    unittest.main()

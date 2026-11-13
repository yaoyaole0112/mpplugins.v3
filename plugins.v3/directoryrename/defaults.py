"""Default target rules; existing global templates remain untouched."""

import json


SHARE_TEMPLATE = (
    "{{title}}{% if year %} ({{year}}){% endif %}/"
    "{{title}}{% if year %}({{year}}){% endif %}"
    "{% if tmdbid %}(tmdbid={{tmdbid}}){% endif %}"
    "{% if season_episode %}.{{season_episode}}{% endif %}"
    "{% if resourceType %}.{{resourceType}}{% endif %}"
    "{% if videoFormat %}.{{videoFormat}}{% endif %}"
    "{% if effect %}.{{effect}}{% endif %}"
    "{% if videoCodec %}.{{videoCodec}}{% endif %}"
    "{% if videoBit %}.{{videoBit}}{% endif %}"
    "{% if audioCodec %}.{{audioCodec}}{% endif %}"
    "{% if fps %}.{{fps}}帧{% endif %}-RMGroup{{fileExt}}"
)
LIBRARY_TV_TEMPLATE = (
    "{{title}}{% if year %} ({{year}}){% endif %}/Season {{season}}/"
    "{{title}} - {{season_episode}}"
    "{% if episode %} - 第 {{episode}} 集{% endif %}"
    "{% if videoFormat %} - {{videoFormat}}{% endif %}"
    "{% if effect %}.{{effect}}{% endif %}"
    "{% if videoCodec %}.{{videoCodec}}{% endif %}"
    "{% if audioCodec %}.{{audioCodec}}{% endif %}{{fileExt}}"
)
LIBRARY_MOVIE_TEMPLATE = (
    "{{title}}{% if year %} ({{year}}){% endif %}/"
    "{{title}}{% if videoFormat %} - {{videoFormat}}{% endif %}"
    "{% if effect %}.{{effect}}{% endif %}"
    "{% if videoCodec %}.{{videoCodec}}{% endif %}"
    "{% if audioCodec %}.{{audioCodec}}{% endif %}{{fileExt}}"
)


def default_config() -> dict:
    return {
        "enabled": False,
        "rules_json": json.dumps(
            [
                {
                    "name": "分享池",
                    "storage": "115网盘Plus",
                    "path": "/影视资源/分享池/已整理/",
                    "tv": SHARE_TEMPLATE,
                    # Movies have no season/episode segment.
                    "movie": SHARE_TEMPLATE.replace(
                        "{% if season_episode %}.{{season_episode}}{% endif %}", ""
                    ),
                },
                {
                    "name": "电视剧媒体库",
                    "storage": "115网盘Plus",
                    "path": "/影视资源/已整理/电视剧/",
                    "tv": LIBRARY_TV_TEMPLATE,
                    "movie": LIBRARY_MOVIE_TEMPLATE,
                },
            ],
            ensure_ascii=False,
            indent=2,
        ),
    }

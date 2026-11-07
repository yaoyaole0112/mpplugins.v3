import importlib.util
import sys
import types


def _load_module():
    app = types.ModuleType("app")
    plugins = types.ModuleType("app.plugins")
    plugins._PluginBase = type("_PluginBase", (), {})
    logging = types.ModuleType("app.sdk.logging")
    logging.logger = types.SimpleNamespace()
    network = types.ModuleType("app.sdk.network")
    network.RequestUtils = object
    services = types.ModuleType("app.sdk.services")
    services.DownloaderHelper = object
    cron = types.ModuleType("apscheduler.triggers.cron")
    cron.CronTrigger = type("CronTrigger", (), {})
    modules = {
        "app": app,
        "app.plugins": plugins,
        "app.sdk": types.ModuleType("app.sdk"),
        "app.sdk.logging": logging,
        "app.sdk.network": network,
        "app.sdk.services": services,
        "apscheduler": types.ModuleType("apscheduler"),
        "apscheduler.triggers": types.ModuleType("apscheduler.triggers"),
        "apscheduler.triggers.cron": cron,
    }
    sys.modules.update(modules)
    path = "config/local_repo/plugins.v3/forumrssqb/__init__.py"
    spec = importlib.util.spec_from_file_location("forumrssqb_test_target", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_parse_rss_items_and_attachment_links():
    module = _load_module()
    xml = """<rss><channel><item><title>Demo</title>
    <link>https://example.test/forum.php?mod=viewthread&amp;tid=1</link>
    <description><![CDATA[body]]></description></item></channel></rss>"""
    item = module.parse_rss_items(xml, "https://example.test/rss.xml")[0]
    assert item["title"] == "Demo"
    body = '<a href="/forum.php?mod=attachment&amp;aid=abc">Demo.torrent</a>'
    assert module.extract_attachment_urls(body, item["link"]) == [
        "https://example.test/forum.php?mod=attachment&aid=abc"
    ]


def test_rejects_html_and_accepts_torrent_like_bytes():
    module = _load_module()
    assert not module.is_torrent_payload(b"<html>login</html>")
    assert module.is_torrent_payload(b"d4:infod4:name4:testee")

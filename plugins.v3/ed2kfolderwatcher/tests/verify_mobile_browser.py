import json
import os
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket


def verify():
    port = int(os.environ.get("ED2K_TEST_PORT", "18765"))
    with tempfile.TemporaryDirectory(prefix="ed2k-mobile-chrome-") as profile:
        process = subprocess.Popen([
            os.environ.get("CHROME_BIN", "/opt/google/chrome/chrome"),
            "--headless", "--no-sandbox", "--disable-gpu", "--disable-background-networking",
            "--remote-debugging-port=0", "--remote-allow-origins=*", f"--user-data-dir={profile}",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        connection = None
        try:
            deadline = time.monotonic() + 20
            active_port = Path(profile) / "DevToolsActivePort"
            while not active_port.exists():
                if time.monotonic() > deadline or process.poll() is not None:
                    raise RuntimeError("Chrome did not start")
                time.sleep(.1)
            debug_port = int(active_port.read_text().splitlines()[0])
            with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/list") as response:
                target = next(entry for entry in json.load(response) if entry["type"] == "page")
            connection = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=30, suppress_origin=True)
            sequence = 0

            def command(method, params=None):
                nonlocal sequence
                sequence += 1
                connection.send(json.dumps({"id": sequence, "method": method, "params": params or {}}))
                while True:
                    result = json.loads(connection.recv())
                    if result.get("id") == sequence:
                        if "error" in result:
                            raise RuntimeError(result["error"])
                        return result.get("result", {})

            def evaluate(expression):
                result = command("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
                if "exceptionDetails" in result:
                    raise RuntimeError(result["result"].get("description", "Browser assertion failed"))
                return result["result"].get("value")

            def tap(selector, text):
                button = evaluate(f"""(() => {{
                    const button = [...document.querySelectorAll({json.dumps(selector)})].find(element => element.textContent.trim() === {json.dumps(text)});
                    if (!button) throw Error('Button missing');
                    const bounds = button.getBoundingClientRect();
                    const position = {{x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2}};
                    if (!button.contains(document.elementFromPoint(position.x, position.y))) throw Error('Button is obscured');
                    return position;
                }})()""")
                command("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [button]})
                command("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
                time.sleep(.3)

            def swipe():
                before = evaluate("window.scrollY")
                command("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": 20, "y": 300}]})
                for position in range(280, 79, -20):
                    command("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": 20, "y": position}]})
                    time.sleep(.025)
                command("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
                time.sleep(.5)
                if evaluate("window.scrollY") <= before + 50:
                    raise RuntimeError("Document does not respond to a touch swipe")

            command("Emulation.setTouchEmulationEnabled", {"enabled": True})
            for width, height in ((320, 640), (390, 844), (430, 932), (820, 1180), (932, 430)):
                command("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 2, "mobile": True})
                for component in ("Page", "Config"):
                    command("Runtime.evaluate", {"expression": "if(document.body) document.body.dataset.result=''"})
                    command("Page.navigate", {"url": f"http://127.0.0.1:{port}/tests/federation-smoke.html?dialog=1&manual=1&component={component}"})
                    deadline = time.monotonic() + 20
                    while evaluate("document.body?.dataset.result") != "ready":
                        if time.monotonic() > deadline:
                            raise RuntimeError("Touch fixture did not become ready")
                        time.sleep(.1)
                    if evaluate("innerWidth") != width:
                        raise RuntimeError("Mobile viewport was not applied")
                    swipe()
                    if component == "Page":
                        evaluate("window.scrollTo({top:0,behavior:'instant'})")
                        tap(".records-actions button", "设置")
                        if not evaluate("Boolean(document.querySelector('.ed2k-panel')) && document.querySelector('.ed2k-panel').getBoundingClientRect().top >= -2"):
                            raise RuntimeError("Touch settings entry failed")
                        swipe()
                    visible = evaluate("""(() => {
                        const button = [...document.querySelectorAll('.ed2k-panel button')].at(-1);
                        button.scrollIntoView({block:'center',behavior:'instant'});
                        const bounds = button.getBoundingClientRect();
                        return button.contains(document.elementFromPoint(bounds.left + bounds.width / 2, bounds.top + bounds.height / 2));
                    })()""")
                    if not visible:
                        raise RuntimeError("Bottom settings are obscured")
                    evaluate("window.scrollTo({top:0,behavior:'instant'})")
                    tap(".header-actions button", "关闭")
                    if evaluate("ed2kSmoke.closeCount()") != 1:
                        raise RuntimeError("Touch close entry failed")
                    evaluate("ed2kSmoke.app.unmount()")
                    if not evaluate("getComputedStyle(document.documentElement).position === 'fixed' && getComputedStyle(document.body).position === 'fixed'"):
                        raise RuntimeError("Host scroll locks were not restored")
                    print(f"PASS {width}x{height} {component}: touch scroll, settings, bottom hit test, close and lock restoration", flush=True)
        finally:
            if connection:
                try:
                    command("Browser.close")
                except (OSError, websocket.WebSocketException):
                    pass
                connection.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
            time.sleep(.5)


if __name__ == "__main__":
    verify()

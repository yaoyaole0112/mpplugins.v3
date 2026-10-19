import json
import os
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket


def verify():
    port = int(os.environ.get("ED2K_TEST_PORT", "18771"))
    with tempfile.TemporaryDirectory(prefix="ed2k-clipboard-chrome-") as profile:
        process = subprocess.Popen([
            os.environ.get("CHROME_BIN", "/opt/google/chrome/chrome"),
            "--headless", "--no-sandbox", "--disable-gpu", "--disable-background-networking",
            "--window-size=1280,1000",
            "--no-proxy-server", "--host-resolver-rules=MAP ed2k-http.test 127.0.0.1",
            "--remote-debugging-port=0", "--remote-allow-origins=*", f"--user-data-dir={profile}",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        connection = None
        try:
            deadline = time.monotonic() + 20
            active_port = Path(profile) / "DevToolsActivePort"
            while not active_port.exists():
                if time.monotonic() > deadline or process.poll() is not None:
                    raise RuntimeError("Chrome did not start")
                time.sleep(0.1)
            debug_port = int(active_port.read_text().splitlines()[0])
            with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/list") as response:
                targets = json.load(response)
            target = next(target for target in targets if target["type"] == "page")
            connection = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=15, suppress_origin=True)
            sequence = 0

            def command(method, params=None):
                nonlocal sequence
                sequence += 1
                identifier = sequence
                connection.send(json.dumps({"id": identifier, "method": method, "params": params or {}}))
                while True:
                    response = json.loads(connection.recv())
                    if response.get("id") == identifier:
                        if "error" in response:
                            raise RuntimeError(response["error"])
                        return response.get("result", {})

            def evaluate(expression):
                result = command("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
                if "exceptionDetails" in result:
                    raise RuntimeError(result["exceptionDetails"])
                return result["result"].get("value")

            def wait_for(expression):
                deadline = time.monotonic() + 15
                while not evaluate(expression):
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"Browser condition timed out: {expression}; page={evaluate('document.body.innerText')}")
                    time.sleep(0.1)

            command("Page.enable")
            for host, query, secure, success in (
                ("ed2k-http.test", "", False, True),
                ("127.0.0.1", "?modern=1", True, True),
                ("ed2k-http.test", "?deny=1", False, False),
            ):
                command("Page.navigate", {"url": f"http://{host}:{port}/tests/clipboard-dialog.html{query}"})
                wait_for("window.fixtureReady && !!document.querySelector('.copy-link')")
                wait_for("document.querySelector('.copy-feedback')?.getBoundingClientRect().height === 40")
                evaluate("document.fonts.ready.then(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))")
                assert evaluate("window.isSecureContext") is secure
                if secure:
                    command("Browser.grantPermissions", {"origin": f"http://{host}:{port}", "permissions": ["clipboardReadWrite", "clipboardSanitizedWrite"]})
                else:
                    assert evaluate("typeof navigator.clipboard") == "undefined"
                evaluate("document.querySelector('.copy-link').scrollIntoView({block: 'center'})")
                layout_expression = "JSON.stringify([...document.querySelectorAll('.record, .record-heading, .record h3, .record-time, .copy-link, .source-status, .copy-feedback')].map(element => { const bounds = element.getBoundingClientRect(); return [bounds.top, bounds.left, bounds.width, bounds.height] }))"
                initial_layout = evaluate(layout_expression)
                coordinates = evaluate("(() => { const bounds = document.querySelector('.copy-link').getBoundingClientRect(); return {x: bounds.left + 20, y: bounds.top + bounds.height / 2} })()")
                command("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, **coordinates})
                command("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, **coordinates})
                if success:
                    wait_for("document.querySelector('.copy-feedback')?.textContent.includes('已复制')")
                    assert evaluate(layout_expression) == initial_layout, (initial_layout, evaluate(layout_expression))
                    evaluate("document.querySelector('#paste-check').focus()")
                    command("Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": "v", "code": "KeyV", "windowsVirtualKeyCode": 86, "modifiers": 2})
                    command("Input.dispatchKeyEvent", {"type": "keyUp", "key": "v", "code": "KeyV", "windowsVirtualKeyCode": 86, "modifiers": 2})
                    wait_for("document.querySelector('#paste-check').value === window.expectedLink")
                    print(f"PASS real clipboard copy and paste in modal: secure={secure}")
                else:
                    wait_for("document.querySelector('.copy-feedback')?.textContent.includes('复制受限')")
                    assert evaluate(layout_expression) == initial_layout, (initial_layout, evaluate(layout_expression))
                    assert evaluate("window.getSelection().toString() === window.expectedLink")
                    assert not evaluate("document.querySelector('.notice')?.textContent.includes('已复制') || false")
                    print("PASS blocked clipboard selects link without claiming success")
                assert evaluate("document.querySelectorAll('.records-page > textarea').length") == 0
                assert evaluate("document.querySelector('.records-page > .notice') === null")
        finally:
            if connection is not None:
                connection.close()
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    verify()

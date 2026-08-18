"""Full-page dashboard screenshot, via Chrome DevTools Protocol.

Usage:  python3 scripts/screenshot.py http://127.0.0.1:8000/ docs/dashboard.png

Regenerates the image README embeds. Two things make this awkward enough to
need a script rather than a one-liner:

Chrome's plain --screenshot fires before the dashboard's first fetch resolves,
so it captures an empty shell; CDP lets us wait for real content. And MapLibre
needs WebGL, which headless Chrome lacks unless SwiftShader is forced on.
"""
import base64, json, subprocess, sys, time
import urllib.request
import websocket

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
URL = sys.argv[1]
OUT = sys.argv[2]
W, H = 1600, 1000

proc = subprocess.Popen([CHROME, "--headless=new", "--remote-debugging-port=9222",
                         "--hide-scrollbars", f"--window-size={W},{H}",
                         "--no-first-run", "--remote-allow-origins=*", "--enable-unsafe-swiftshader", "--use-gl=angle", "--use-angle=swiftshader", "--enable-webgl", "about:blank"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try:
            tabs = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json"))
            page = next(t for t in tabs if t["type"] == "page")
            break
        except Exception:
            time.sleep(0.5)
    else:
        raise SystemExit("chrome did not expose a debugging target")

    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=90)
    n = 0
    def send(method, params=None):
        global n
        n += 1
        ws.send(json.dumps({"id": n, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == n:
                return msg.get("result", {})

    send("Page.enable")
    send("Runtime.enable")
    send("Page.navigate", {"url": URL})
    time.sleep(22)   # Let the socket push, charts animate, and map tiles land.

    # Grow the viewport to the full document so nothing is cropped.
    height = send("Runtime.evaluate", {"expression": "document.body.scrollHeight", "returnByValue": True})["result"]["value"]
    send("Emulation.setDeviceMetricsOverride",
         {"width": W, "height": int(height), "deviceScaleFactor": 2, "mobile": False})
    time.sleep(3)
    send("Runtime.evaluate", {"expression": "window.dispatchEvent(new Event('resize'))"})
    time.sleep(3)

    data = send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})["data"]
    open(OUT, "wb").write(base64.b64decode(data))
    print(f"wrote {OUT} at {W}x{int(height)}")
finally:
    proc.terminate()

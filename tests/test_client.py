"""Tests against a local stub HTTP server: no network, no real key. Run with pytest or python -m unittest."""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import warnings
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lassocut import LassoCut, LassoCutError, LassoCutWarning, __version__  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nfake-png"


def parse_multipart(content_type, body):
    msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    fields, files = {}, {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if filename is not None:
            files[name] = (filename, payload)
        else:
            fields[name] = payload.decode()
    return fields, files


class Stub:
    """Answers each request with the next queued response and records what it received."""

    def __init__(self):
        self.queue, self.requests = [], []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                stub.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                      "body": body})
                status, headers, payload = stub.queue.pop(0) if stub.queue else (500, {}, b"")
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1.0"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def ok(self, payload=PNG, ctype="image/png", **extra):
        headers = {"Content-Type": ctype, "X-Credits-Charged": "0.25", "X-Width": "400", "X-Height": "300",
                   "X-Type": "product", "X-Foreground-Top": "10", "X-Foreground-Left": "20",
                   "X-Foreground-Width": "100", "X-Foreground-Height": "200"}
        headers.update(extra)
        self.queue.append((200, headers, payload))

    def fail(self, status, errors=None, **headers):
        body = json.dumps({"errors": errors}).encode() if errors is not None else b""
        self.queue.append((status, {"Content-Type": "application/json", **headers}, body))

    def form(self, i=-1):
        r = self.requests[i]
        return parse_multipart(r["headers"]["Content-Type"], r["body"])


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.stub = Stub()
        self.sleeps = []
        self.client = LassoCut("test-key", api_url=self.stub.url, sleep=self.sleeps.append, timeout=10)

    def tearDown(self):
        self.stub.close()

    # -- request shape ----------------------------------------------------------------------------------------

    def test_bytes_upload_and_headers(self):
        self.stub.ok()
        result = self.client.remove_background(b"img-bytes", size="preview")
        req = self.stub.requests[0]
        self.assertEqual((req["method"], req["path"]), ("POST", "/v1.0/removebg"))
        self.assertEqual(req["headers"]["X-Api-Key"], "test-key")
        self.assertEqual(req["headers"]["X-Lassocut-Client"], "python-sdk")
        self.assertEqual(req["headers"]["User-Agent"], f"lassocut-python/{__version__}")
        self.assertTrue(req["headers"]["Idempotency-Key"])
        fields, files = self.stub.form()
        self.assertEqual(fields, {"size": "preview"})
        self.assertEqual(files["image_file"][1], b"img-bytes")
        self.assertEqual(result.data, PNG)
        self.assertEqual(bytes(result), PNG)
        self.assertEqual(result.credits_charged, 0.25)
        self.assertEqual((result.width, result.height), (400, 300))
        self.assertEqual(result.type, "product")
        self.assertEqual(result.foreground, (10, 20, 100, 200))
        self.assertEqual(result.extension, ".png")
        self.assertIsNone(result.compliant)

    def test_path_and_file_object(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "shoe.jpg")
            p.write_bytes(b"jpeg-bytes")
            self.stub.ok()
            self.client.remove_background(str(p))
            self.assertEqual(self.stub.form()[1]["image_file"], ("shoe.jpg", b"jpeg-bytes"))
            self.stub.ok()
            with open(p, "rb") as fh:
                self.client.remove_background(fh)
            self.assertEqual(self.stub.form()[1]["image_file"], ("shoe.jpg", b"jpeg-bytes"))
            self.stub.ok()
            self.client.remove_background(io.BytesIO(b"x"))
            self.assertEqual(self.stub.form()[1]["image_file"], ("image", b"x"))

    def test_image_url_and_default_size(self):
        self.stub.ok()
        self.client.remove_background(image_url="https://example.com/a.jpg")
        fields, files = self.stub.form()
        self.assertEqual(fields, {"size": "auto", "image_url": "https://example.com/a.jpg"})
        self.assertEqual(files, {})

    def test_exactly_one_source(self):
        with self.assertRaises(ValueError):
            self.client.remove_background()
        with self.assertRaises(ValueError):
            self.client.remove_background(b"x", image_url="https://example.com/a.jpg")
        with self.assertRaises(FileNotFoundError):
            self.client.remove_background("does-not-exist.jpg")
        self.assertEqual(self.stub.requests, [])

    def test_plain_options(self):
        self.stub.ok()
        self.client.remove_background(b"x", format="JPG", bg_color="#81d4fa", crop=True, crop_margin="10%",
                                      scale="80%", position="center", channels="rgba", type="product",
                                      bg_image=b"bg", extra={"semitransparency": False, "roi": "0% 0% 100% 100%"})
        fields, files = self.stub.form()
        self.assertEqual(fields, {"size": "auto", "type": "product", "format": "jpg", "channels": "rgba",
                                  "bg_color": "81d4fa", "crop": "true", "crop_margin": "10%", "scale": "80%",
                                  "position": "center", "semitransparency": "false", "roi": "0% 0% 100% 100%"})
        self.assertEqual(files["bg_image_file"][1], b"bg")

    # -- presets and shadow -----------------------------------------------------------------------------------

    def test_preset_sends_only_preset(self):
        self.stub.ok(payload=b"jpg", ctype="image/jpeg", **{"X-Preset": "amazon",
                                                           "X-Compliance": "fail; reasons=fill,size",
                                                           "X-Preset-Ignored": "crop", "X-Upscaled": "true"})
        result = self.client.remove_background(b"x", preset="Amazon", crop=False)
        fields, _ = self.stub.form()
        self.assertEqual(fields, {"size": "auto", "preset": "amazon"})
        self.assertEqual(result.preset, "amazon")
        self.assertFalse(result.compliant)
        self.assertEqual(result.compliance_reasons, ["fill", "size"])
        self.assertEqual(result.preset_ignored, ["crop"])
        self.assertTrue(result.upscaled)
        self.assertEqual(result.extension, ".jpg")

    def test_preset_compliant(self):
        self.stub.ok(ctype="image/jpeg", **{"X-Preset": "ebay", "X-Compliance": "ok"})
        result = self.client.remove_background(b"x", preset="ebay")
        self.assertTrue(result.compliant)
        self.assertEqual(result.compliance_reasons, [])

    def test_preset_rejects_options_it_decides(self):
        blocked = {"format": "png", "channels": "rgba", "bg_color": "fff", "bg_image": b"b",
                   "bg_image_url": "https://example.com/b.jpg", "crop": True, "crop_margin": "5px",
                   "scale": "80%", "position": "center"}
        for name, value in blocked.items():
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, name):
                self.client.remove_background(b"x", preset="shopify", **{name: value})
        with self.assertRaisesRegex(ValueError, "bg-color"):
            self.client.remove_background(b"x", preset="shopify", extra={"bg-color": "fff"})
        with self.assertRaisesRegex(ValueError, "unsupported preset"):
            self.client.remove_background(b"x", preset="walmart")
        self.assertEqual(self.stub.requests, [])

    def test_preset_none_means_no_preset(self):
        self.stub.ok()
        self.client.remove_background(b"x", preset="none", format="png")
        self.assertEqual(self.stub.form()[0], {"size": "auto", "format": "png"})

    def test_cast_shadow(self):
        self.stub.ok()
        self.client.remove_background(b"x", preset="etsy", shadow="cast", light_angle=-30, shadow_length="Long",
                                      shadow_strength=55)
        self.assertEqual(self.stub.form()[0], {"size": "auto", "preset": "etsy", "shadow_type": "cast",
                                               "shadow_angle": "-30", "shadow_length": "long",
                                               "shadow_opacity": "55"})

    def test_shadow_validation(self):
        bad = [dict(shadow="drop"), dict(light_angle=10), dict(shadow="none", shadow_length="long"),
               dict(shadow="cast", light_angle=91), dict(shadow="cast", shadow_strength=101),
               dict(shadow="cast", shadow_length="huge"), dict(shadow="cast", light_angle="abc")]
        for opts in bad:
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                self.client.remove_background(b"x", **opts)
        self.stub.ok()
        self.client.remove_background(b"x", shadow="none")
        self.assertEqual(self.stub.form()[0], {"size": "auto"})

    def test_zalando_ignores_shadow_with_warning(self):
        self.stub.ok()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.client.remove_background(b"x", preset="zalando", shadow="cast", light_angle=20)
        self.assertTrue(any(issubclass(w.category, LassoCutWarning) for w in caught))
        self.assertEqual(self.stub.form()[0], {"size": "auto", "preset": "zalando"})

    # -- retries and errors -----------------------------------------------------------------------------------

    def test_retries_502_503_504_with_same_idempotency_key(self):
        self.stub.fail(502)
        self.stub.fail(504, [{"title": "Processing took longer than your app waits."}])
        self.stub.ok()
        result = self.client.remove_background(b"x")
        self.assertEqual(result.data, PNG)
        self.assertEqual(len(self.stub.requests), 3)
        self.assertEqual(self.sleeps, [1.0, 2.0])
        keys = {r["headers"]["Idempotency-Key"] for r in self.stub.requests}
        self.assertEqual(len(keys), 1)

    def test_gives_up_after_three_attempts(self):
        for _ in range(3):
            self.stub.fail(503)
        with self.assertRaises(LassoCutError) as ctx:
            self.client.remove_background(b"x")
        self.assertEqual(ctx.exception.status, 503)
        self.assertIn("busy", str(ctx.exception))
        self.assertEqual(len(self.stub.requests), 3)

    def test_no_retry_on_client_errors(self):
        self.stub.fail(400, [{"title": "No foreground found", "code": "unknown_foreground", "detail": "empty"}])
        with self.assertRaises(LassoCutError) as ctx:
            self.client.remove_background(b"x")
        e = ctx.exception
        self.assertEqual((e.status, e.code, e.title, e.detail), (400, "unknown_foreground", "No foreground found",
                                                                 "empty"))
        self.assertEqual(str(e), "No foreground found: empty")
        self.assertEqual(len(self.stub.requests), 1)

    def test_friendly_messages(self):
        self.stub.fail(402, [{"title": "Insufficient credits"}])
        with self.assertRaisesRegex(LassoCutError, "Not enough credits"):
            self.client.remove_background(b"x")
        self.stub.fail(403, [{"title": "Forbidden"}])
        with self.assertRaisesRegex(LassoCutError, "rejected"):
            self.client.credits()
        self.stub.fail(429, [{"title": "Rate limit exceeded"}], **{"Retry-After": "12"})
        with self.assertRaises(LassoCutError) as ctx:
            self.client.remove_background(b"x")
        self.assertEqual(ctx.exception.retry_after, 12)
        self.assertIn("12 seconds", str(ctx.exception))

    def test_unreachable(self):
        self.stub.close()
        client = LassoCut("k", api_url=self.stub.url, timeout=2)
        with self.assertRaisesRegex(LassoCutError, "Could not reach"):
            client.remove_background(b"x")
        self.stub = Stub()   # tearDown closes it

    # -- account, files, config -------------------------------------------------------------------------------

    def test_credits(self):
        body = {"data": {"attributes": {"credits": {"total": 200.0, "subscription": 0.0, "payg": 200.0,
                                                    "enterprise": 0.0},
                                        "api": {"free_calls": 10, "sizes": "all"}}}}
        self.stub.queue.append((200, {"Content-Type": "application/json"}, json.dumps(body).encode()))
        balance = self.client.credits()
        req = self.stub.requests[0]
        self.assertEqual((req["method"], req["path"]), ("GET", "/v1.0/account"))
        self.assertEqual(req["headers"]["X-Api-Key"], "test-key")
        self.assertEqual((balance.credits, balance.free_previews), (200.0, 10))

    def test_remove_background_file(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d, "photo.jpg")
            src.write_bytes(b"jpeg")
            self.stub.ok(payload=b"jpg-out", ctype="image/jpeg", **{"X-Preset": "amazon", "X-Compliance": "ok"})
            result = self.client.remove_background_file(src, preset="amazon")
            self.assertEqual(Path(d, "photo-lassocut.jpg").read_bytes(), b"jpg-out")
            self.assertTrue(result.compliant)
            self.stub.ok()
            out = Path(d, "sub", "cut.png")
            self.client.remove_background_file(str(src), out, size="preview")
            self.assertEqual(out.read_bytes(), PNG)
            self.assertEqual(self.stub.form()[0], {"size": "preview"})

    def test_key_from_environment(self):
        with mock.patch.dict(os.environ, {"LASSOCUT_API_KEY": "env-key", "LASSOCUT_API_URL": self.stub.url + "/"}):
            client = LassoCut()
        self.assertEqual(client.api_url, self.stub.url)
        self.stub.ok()
        client.remove_background(b"x")
        self.assertEqual(self.stub.requests[0]["headers"]["X-Api-Key"], "env-key")
        self.assertNotIn("env-key", repr(client))
        env = {k: v for k, v in os.environ.items() if k not in ("LASSOCUT_API_KEY", "REMOVE_BG_API_KEY")}
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaisesRegex(LassoCutError, "No API key"):
            LassoCut()

    def test_default_api_url(self):
        env = {k: v for k, v in os.environ.items() if k != "LASSOCUT_API_URL"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(LassoCut("k").api_url, "https://api.lassocut.com/v1.0")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""
Serve the Forest Watch phone app on the local network.

The app itself runs in the phone's browser and listens to the ntfy topics
that forest/incidents.py publishes to. This script only hands the app's
files to the phone, and prints a QR code so a phone can open it quickly.

Usage:
  source .venv/bin/activate
  python serve.py                                   # http://<this computer>:8000
  python serve.py --prefix forestwatch-team42       # QR code presets the topic

On the iPhone: scan the QR code (same Wi-Fi / hotspot as this computer),
then Share -> "Add to Home Screen" so it opens full screen like an app.
"""
import argparse
import functools
import http.server
import socket
import sys
from pathlib import Path
from urllib.parse import urlencode

APP_DIR = Path(__file__).resolve().parent / "app"


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".webmanifest": "application/manifest+json",
    }

    def end_headers(self):
        # no caching, so edits show up on the phone after a refresh
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()}  {fmt % args}\n")


class Server(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # Phones often hang up mid-download (e.g. Safari cancelling the
        # manifest or icon request). Harmless – don't print a traceback.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def lan_ip():
    """The address other devices on the network can reach (no data is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--prefix", help="ntfy topic prefix to preset in the app")
    p.add_argument("--server", help="ntfy server to preset (default https://ntfy.sh)")
    a = p.parse_args()

    query = {k: v for k, v in (("prefix", a.prefix), ("server", a.server)) if v}
    url = f"http://{lan_ip()}:{a.port}/" + (f"?{urlencode(query)}" if query else "")

    handler = functools.partial(Handler, directory=str(APP_DIR))
    httpd = Server(("0.0.0.0", a.port), handler)

    print(f"\nForest Watch phone app:  {url}\n")
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except ImportError:
        print("(pip install -r requirements.txt to show a QR code here)")
    print("\nPhone must be on the same Wi-Fi/hotspot. Ctrl+C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

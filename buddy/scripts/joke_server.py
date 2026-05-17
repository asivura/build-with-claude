"""Tiny local HTTP joke server for the Cardputer-Adv demo.

### Why this exists

The Cardputer's MicroPython TLS stack runs out of heap during the
HTTPS handshake -- only ~65 KB free after the LCD / BLE / keyboard
drivers initialize, well below what the TLS context wants. Every
modern free joke API forces HTTPS now (we checked icanhazdadjoke,
api.icndb.com, geek-jokes.sameerkumar.website -- all 301 to TLS).

So we serve jokes locally over plain HTTP from this machine. The
device fetches ``http://<this-laptop-LAN-IP>:8080/joke`` and the
same verbose narration that lit up for wttr.in works here too,
just pointing at a server on the same WiFi.

### Run

    python3 scripts/joke_server.py

Then point ``joke_fetcher.py`` at this machine's LAN IP. The kid
can edit the ``JOKES`` list below and changes show up on the next
``/joke`` request -- the module-level list is re-read on every
``random.choice`` call, so no restart is needed for content edits.

### Endpoints

    GET /        tiny help page
    GET /joke    one random joke as plain text
    GET /jokes   the full list, one per line
"""

import random
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 8080

JOKES = [
    "Why don't scientists trust atoms? They make up everything.",
    "I told my wife she was drawing her eyebrows too high. She looked surprised.",
    "Why did the bicycle fall over? It was two-tired.",
    "What do you call a fish with no eyes? Fsh.",
    "I'm reading a book about anti-gravity. It's impossible to put down.",
    "Why don't eggs tell jokes? They'd crack each other up.",
    "Why did the scarecrow win an award? He was outstanding in his field.",
    "What do you call cheese that isn't yours? Nacho cheese.",
    "How does a penguin build its house? Igloos it together.",
    "Why don't oysters share their pearls? They're shellfish.",
    "What's brown and sticky? A stick.",
    "I used to play piano by ear. Now I use my hands.",
    "What did one wall say to the other? I'll meet you at the corner.",
    "Why did the math book look sad? It had too many problems.",
    "How do you organize a space party? You planet.",
    "What do you call a sleeping bull? A bulldozer.",
    "Why did the cookie go to the doctor? It felt crumbly.",
    "What did the ocean say to the shore? Nothing, it just waved.",
    "Why don't skeletons fight each other? They don't have the guts.",
    "What kind of music do mummies listen to? Wrap.",
    "Why did the cat sit on the computer? To keep an eye on the mouse.",
    "What's an astronaut's favorite key on a keyboard? The space bar.",
    "Why did the orange stop? It ran out of juice.",
    "What do you call a fake noodle? An impasta.",
    "Why did the can crusher quit? It was soda pressing.",
    "How do you make a tissue dance? Put a little boogie in it.",
    "What did the grape say when it got stepped on? Nothing, it just let out a little wine.",
    "Why are ghosts bad at lying? You can see right through them.",
    "What did one hat say to the other? Stay here, I'm going on ahead.",
    "Why don't programmers like nature? It has too many bugs.",
]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/joke":
            self._send(200, "text/plain; charset=utf-8", random.choice(JOKES))
            return
        if self.path == "/jokes":
            self._send(200, "text/plain; charset=utf-8", "\n".join(JOKES))
            return
        if self.path == "/":
            help_text = (
                "Cardputer joke server\n\n"
                "  GET /joke   -- random joke\n"
                "  GET /jokes  -- all jokes\n"
            )
            self._send(200, "text/plain; charset=utf-8", help_text)
            return
        self._send(404, "text/plain; charset=utf-8", "not found\n")

    def _send(self, code, ctype, body):
        body_b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body_b)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body_b)

    def log_message(self, fmt, *args):
        # Keep request logs on one line; default formatting is noisy.
        sys.stderr.write(
            "{} - {}\n".format(self.address_string(), fmt % args)
        )


def main():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    sys.stderr.write("Joke server listening on 0.0.0.0:{}\n".format(PORT))
    sys.stderr.write("Try: curl http://localhost:{}/joke\n".format(PORT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nshutting down\n")
        server.server_close()


if __name__ == "__main__":
    main()

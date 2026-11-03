"""A small fake of GitHub: its OAuth app sign-in on the web host and the REST API's issue routes on the API host.

The tests call it in process as an `httpx.MockTransport` handler. Run as a script, it serves the same handler over
https on this machine's private address with a self-signed certificate for `github.test` and `api.github.test`, so a
local Ops AI stack can sign in to it and call it as it would GitHub, and prints one JSON line with the address, the
certificate and the OAuth app's client id and secret:

    python tests/fake_github.py --dir .run/fake-github

Like GitHub, the token answer is form-encoded unless the request asks for JSON with `Accept: application/json`, and
the issue list is a bare JSON array paged by the `Link` header.
"""

from __future__ import annotations

import argparse
import html
import json
import secrets
import socket
import ssl
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx

WEB_HOST = "github.test"
API_HOST = "api.github.test"
CLIENT_ID = "Iv1.fakegithubclient"
CLIENT_SECRET = "fake-github-secret"
LOGIN = "octo-ops"


def _issue(  # noqa: PLR0917 (one line per seeded issue reads best positionally)
    owner: str, repo: str, number: int, title: str, body: str, labels: list[str], state: str = "open"
) -> dict[str, Any]:
    return {
        "id": 1000 + number,
        "number": number,
        "title": title,
        "state": state,
        "body": body,
        "user": {"login": "dana-okafor", "id": 7},
        "labels": [{"name": n, "color": "ededed"} for n in labels],
        "comments": 0,
        "updated_at": f"2026-10-{10 + number:02d}T09:00:00Z",
        "html_url": f"https://{WEB_HOST}/{owner}/{repo}/issues/{number}",
    }


def seed() -> dict[tuple[str, str], list[dict[str, Any]]]:
    return {
        ("acme-logistics", "dispatch-api"): [
            _issue(
                "acme-logistics",
                "dispatch-api",
                3,
                "Route planner times out over 200 stops",
                "Seen on the Leeds depot run.",
                ["bug"],
            ),
            _issue(
                "acme-logistics",
                "dispatch-api",
                2,
                "Add driver break rules for the EU",
                "Tachograph rules.",
                ["feature"],
            ),
            _issue("acme-logistics", "dispatch-api", 1, "Old manifest export", "Replaced by v2.", [], "closed"),
        ],
        ("northwind-clinic", "intake-forms"): [
            _issue(
                "northwind-clinic",
                "intake-forms",
                12,
                "Consent checkbox missing on mobile",
                "Safari only.",
                ["bug", "a11y"],
            ),
        ],
    }


class FakeGitHub:
    def __init__(self) -> None:
        self.issues = seed()
        self.comments: list[dict[str, Any]] = []
        self.codes: dict[str, str] = {}
        self.tokens: dict[str, str] = {"tok": LOGIN}
        self.seen: list[httpx.Request] = []
        self.token_requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        host, path = request.url.host, request.url.path
        if host == WEB_HOST or path.startswith("/login/oauth/"):
            return self._web(request, path)
        return self._api(request, path)

    # ── sign-in ──

    def _web(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == "/login/oauth/authorize" and request.method == "GET":
            q = dict(request.url.params)
            if q.get("client_id") != CLIENT_ID:
                return httpx.Response(404, text="Unknown OAuth app")
            hidden = "".join(
                f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">' for k, v in q.items()
            )
            page = (
                "<!doctype html><title>Authorize Ops AI</title><h1>Fake GitHub</h1>"
                f"<p>Ops AI wants to read and comment on issues as <b>{LOGIN}</b>"
                f" (scopes: {html.escape(q.get('scope', ''))}).</p>"
                f'<form method="post" action="/login/oauth/authorize">{hidden}'
                '<button type="submit" name="decision" value="allow">Authorize</button> '
                '<button type="submit" name="decision" value="deny">Cancel</button></form>'
            )
            return httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"})
        if path == "/login/oauth/authorize" and request.method == "POST":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            back = form["redirect_uri"]
            if form.get("decision") != "allow":
                params = {"error": "access_denied", "state": form.get("state", "")}
            else:
                code = secrets.token_hex(8)
                self.codes[code] = LOGIN
                params = {"code": code, "state": form.get("state", "")}
            sep = "&" if "?" in back else "?"
            return httpx.Response(302, headers={"location": f"{back}{sep}{urlencode(params)}"})
        if path == "/login/oauth/access_token" and request.method == "POST":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            basic = request.headers.get("authorization", "")
            self.token_requests.append(
                {"form": sorted(form), "accept": request.headers.get("accept"), "basic": basic.startswith("Basic ")}
            )
            if form.get("client_id", CLIENT_ID) != CLIENT_ID or (
                form.get("client_secret") and form["client_secret"] != CLIENT_SECRET
            ):
                answer = {"error": "incorrect_client_credentials"}
            elif (login := self.codes.pop(form.get("code", ""), None)) is None:
                answer = {"error": "bad_verification_code"}
            else:
                token = "gho_" + secrets.token_hex(16)
                self.tokens[token] = login
                answer = {"access_token": token, "token_type": "bearer", "scope": "repo,read:user"}
            if "application/json" in request.headers.get("accept", ""):
                return httpx.Response(200, json=answer)
            return httpx.Response(
                200, text=urlencode(answer), headers={"content-type": "application/x-www-form-urlencoded"}
            )
        return httpx.Response(404, text="Not Found")

    # ── REST API ──

    def _api(self, request: httpx.Request, path: str) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        token = auth.split(" ", 1)[1] if auth.lower().startswith(("bearer ", "token ")) else ""
        login = self.tokens.get(token)
        if login is None:
            return httpx.Response(401, json={"message": "Bad credentials", "status": "401"})
        if path == "/user":
            return httpx.Response(200, json={"login": login, "id": 42})
        parts = path.strip("/").split("/")
        if len(parts) < 4 or parts[0] != "repos" or parts[3] != "issues":
            return httpx.Response(404, json={"message": "Not Found"})
        issues = self.issues.get((parts[1], parts[2]))
        if issues is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if len(parts) == 4 and request.method == "GET":
            state = request.url.params.get("state", "open")
            per_page = int(request.url.params.get("per_page", "30"))
            rows = [i for i in issues if state == "all" or i["state"] == state][:per_page]
            return httpx.Response(200, json=rows)
        number = int(parts[4]) if len(parts) >= 5 and parts[4].isdigit() else -1
        issue = next((i for i in issues if i["number"] == number), None)
        if issue is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if len(parts) == 5 and request.method == "GET":
            return httpx.Response(200, json=issue)
        if len(parts) == 6 and parts[5] == "comments" and request.method == "POST":
            try:
                body = json.loads(request.content)
            except ValueError:
                return httpx.Response(400, json={"message": "Problems parsing JSON"})
            if not isinstance(body, dict) or not isinstance(body.get("body"), str) or not body["body"]:
                return httpx.Response(422, json={"message": "Validation Failed"})
            comment = {
                "id": 9000 + len(self.comments),
                "body": body["body"],
                "user": {"login": login},
                "html_url": f"{issue['html_url']}#issuecomment-{9000 + len(self.comments)}",
            }
            self.comments.append({"issue": number, **comment})
            issue["comments"] += 1
            return httpx.Response(201, json=comment)
        return httpx.Response(404, json={"message": "Not Found"})


# ── serving over https for a local stack ──


def private_address() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return str(s.getsockname()[0])
    finally:
        s.close()


def certificate(folder: Path) -> tuple[Path, Path]:
    cert, key = folder / "github.crt", folder / "github.key"
    if not cert.exists():
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "7",
                "-subj",
                "/CN=fake github",
                "-addext",
                f"subjectAltName=DNS:{WEB_HOST},DNS:{API_HOST}",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
            ],
            check=True,
            capture_output=True,
        )
    return cert, key


def serve(folder: Path, port: int) -> None:
    fake = FakeGitHub()

    class Handler(BaseHTTPRequestHandler):
        def _any(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            host = (self.headers.get("host") or API_HOST).split(":")[0]
            request = httpx.Request(
                self.command, f"https://{host}{self.path}", headers=dict(self.headers), content=body
            )
            answer = fake(request)
            data = answer.content
            self.send_response(answer.status_code)
            for k, v in answer.headers.items():
                if k.lower() not in ("content-length", "transfer-encoding", "connection"):
                    self.send_header(k, v)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PATCH = do_DELETE = _any  # noqa: N815 (http.server dispatches by these names)

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("fake-github " + (fmt % args) + "\n")

    folder.mkdir(parents=True, exist_ok=True)
    cert, key = certificate(folder)
    address = private_address()
    server = ThreadingHTTPServer((address, port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    target = f"{address}:{server.server_address[1]}"
    info = {
        "address": target,
        "ca": str(cert.resolve()),
        "resolve": {WEB_HOST: target, API_HOST: target},
        "settings": {"web_url": f"https://{WEB_HOST}", "api_url": f"https://{API_HOST}"},
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "hosts": [WEB_HOST, API_HOST],
        "range": f"{address}/32",
    }
    print(json.dumps(info), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, default=Path(".run/fake-github"))
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    serve(args.dir, args.port)

"""Security regressions (SECURITY_AUDIT_REPORT.md): MCP environment isolation,
private session storage, sandbox-bypass approval, SSRF / DNS rebinding,
browser request guarding. All offline: local servers and fake resolvers."""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from coded.config import Config
from coded.permissions import Decision, PermissionManager
from coded.tools.base import ToolContext

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


# -- SEC-01: MCP servers get a minimal environment ---------------------------------

PARENT = {"PATH": "/usr/bin", "HOME": "/home/u", "LANG": "C.UTF-8", "LC_ALL": "C",
          "HTTPS_PROXY": "http://proxy:8080", "OPENAI_API_KEY": "sk-secret",
          "AWS_SECRET_ACCESS_KEY": "aws-secret", "GITHUB_TOKEN": "ghp_x",
          "DATABASE_PASSWORD": "pw", "SYSTEMROOT": "C:\\Windows",
          "BASH_FUNC_x%%": "() { evil; }", "TERM": "() { evil; }"}


def test_mcp_server_does_not_see_unrelated_secrets():
    from coded.mcp_client import server_params

    _cmd, _args, env = server_params({"command": "srv"}, PARENT)
    for secret in ("OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN",
                   "DATABASE_PASSWORD"):
        assert secret not in env
    # what programs need to start stays
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/u"
    assert env["LANG"] == "C.UTF-8" and env["LC_ALL"] == "C"
    assert env["HTTPS_PROXY"] == "http://proxy:8080" and env["SYSTEMROOT"] == "C:\\Windows"
    assert "TERM" not in env  # an exported shell function is never passed on


def test_mcp_explicit_env_and_opt_in_inheritance():
    from coded.mcp_client import server_params

    cfg = {"command": "srv", "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}", "MODE": "ci",
                                     "PATH": "/opt/srv/bin"}}
    env = server_params(cfg, PARENT)[2]
    assert env["GITHUB_TOKEN"] == "ghp_x" and env["MODE"] == "ci"
    assert env["PATH"] == "/opt/srv/bin"  # the server's own value wins
    assert "OPENAI_API_KEY" not in env

    named = server_params({"command": "srv", "inherit_env": ["DATABASE_PASSWORD"]}, PARENT)[2]
    assert named["DATABASE_PASSWORD"] == "pw" and "OPENAI_API_KEY" not in named
    everything = server_params({"command": "srv", "inherit_env": True}, PARENT)[2]
    assert everything["OPENAI_API_KEY"] == "sk-secret"


@pytest.mark.skipif(sys.platform == "win32", reason="spawns a POSIX python child")
def test_mcp_child_process_really_gets_the_minimal_env(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from coded.mcp_client import MCPManager

    server = tmp_path / "srv.py"
    server.write_text(
        "import os\n"
        "try:\n"
        "    from mcp.server.mcpserver import MCPServer as App\n"
        "except ImportError:\n"
        "    from mcp.server.fastmcp import FastMCP as App\n"
        "app = App('envcheck')\n"
        "@app.tool()\n"
        "def env_names() -> str:\n"
        "    return ','.join(sorted(os.environ))\n"
        "app.run()\n")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("CODED_TEST_MARKER", "x")
    mgr = MCPManager({"envcheck": {"command": sys.executable, "args": [str(server)],
                                   "env": {"EXPLICIT_ONE": "1"}}})
    try:
        mgr.start()
        if not mgr.sessions:
            pytest.skip("MCP server did not start: " + "; ".join(mgr.errors))
        names = mgr.call("envcheck", "env_names", {})
    finally:
        mgr.stop()
    names = str(names)
    assert "EXPLICIT_ONE" in names and "PATH" in names
    assert "OPENAI_API_KEY" not in names and "CODED_TEST_MARKER" not in names


# -- SEC-02: private session storage ---------------------------------------------

def _session():
    from coded.session import Session

    s = Session(system_prompt="sys")
    s.messages.append({"role": "user", "content": "my password is hunter2"})
    return s


@posix_only
def test_sessions_are_private_files_in_a_private_folder(tmp_path, monkeypatch):
    from coded.sessions_store import SessionStore

    folder = tmp_path / "sessions"
    monkeypatch.setenv("CODED_SESSIONS_DIR", str(folder))
    store = SessionStore()
    store.save(_session(), session_id="20260101-000000-001", cwd="/p", model="m")
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    path = folder / "20260101-000000-001.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not [p for p in folder.iterdir() if p.name.endswith(".tmp")]  # atomic, no debris
    assert store.load("20260101-000000-001").messages[-1]["content"].endswith("hunter2")


@posix_only
def test_the_temp_file_is_private_before_anything_is_written(tmp_path, monkeypatch):
    from coded import securefs

    modes = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *a, **kw):
        fd = real_open(path, flags, mode, *a, **kw)
        modes.append((str(path), stat.S_IMODE(os.fstat(fd).st_mode), flags))
        return fd

    monkeypatch.setattr(securefs.os, "open", spy)
    securefs.write_private(tmp_path / "x.json", "{}")
    assert modes and modes[0][1] == 0o600 and modes[0][0].endswith(".tmp")
    assert modes[0][2] & os.O_EXCL


@posix_only
def test_an_old_readable_session_folder_is_tightened(tmp_path, monkeypatch):
    from coded import securefs
    from coded.sessions_store import SessionStore

    folder = tmp_path / "old"
    folder.mkdir(mode=0o755)
    os.chmod(folder, 0o755)
    old = folder / "20250101-000000-001.json"
    old.write_text(json.dumps({"id": "x", "messages": []}))
    os.chmod(old, 0o644)
    monkeypatch.setenv("CODED_SESSIONS_DIR", str(folder))
    monkeypatch.setattr(securefs, "_tightened", set())
    SessionStore().save(_session(), session_id="20260101-000000-002", cwd="/p", model="m")
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE(old.stat().st_mode) == 0o600
    assert SessionStore().load("20250101-000000-001") is not None  # still readable by us


def test_session_ids_cannot_leave_the_folder(tmp_path, monkeypatch):
    from coded.securefs import UnsafePath
    from coded.sessions_store import SessionStore

    monkeypatch.setenv("CODED_SESSIONS_DIR", str(tmp_path / "s"))
    (tmp_path / "victim.json").write_text(json.dumps({"messages": []}))
    store = SessionStore()
    for bad in ("../victim", "..", "a/b", "/etc/passwd", "", ".hidden", "x\\..\\y"):
        assert store.load(bad) is None
        with pytest.raises(UnsafePath):
            store.save(_session(), session_id=bad, cwd="/p", model="m")
    assert json.loads((tmp_path / "victim.json").read_text()) == {"messages": []}


@pytest.mark.skipif(not hasattr(os, "symlink") or sys.platform == "win32",
                    reason="needs symlinks")
def test_a_symlinked_session_folder_or_file_is_refused(tmp_path, monkeypatch):
    from coded.securefs import UnsafePath
    from coded.sessions_store import SessionStore

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "sessions"
    link.symlink_to(elsewhere)
    monkeypatch.setenv("CODED_SESSIONS_DIR", str(link))
    with pytest.raises(UnsafePath):
        SessionStore().save(_session(), session_id="a1", cwd="/p", model="m")
    assert not list(elsewhere.iterdir())

    real = tmp_path / "real"
    real.mkdir()
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"messages": [{"role": "user", "content": "s"}]}))
    (real / "a2.json").symlink_to(secret)
    monkeypatch.setenv("CODED_SESSIONS_DIR", str(real))
    assert SessionStore().load("a2") is None
    # saving replaces the link itself, never writes through it
    SessionStore().save(_session(), session_id="a2", cwd="/p", model="m")
    assert json.loads(secret.read_text())["messages"][0]["content"] == "s"
    assert not (real / "a2.json").is_symlink()


def test_existing_sessions_still_load(tmp_path, monkeypatch):
    from coded.sessions_store import SessionStore

    monkeypatch.setenv("CODED_SESSIONS_DIR", str(tmp_path))
    (tmp_path / "20240101-000000-000.json").write_text(json.dumps({
        "id": "20240101-000000-000", "cwd": "/p", "model": "m", "updated": 1,
        "system_prompt": "s", "messages": [{"role": "user", "content": "hi"}]}))
    loaded = SessionStore().load("20240101-000000-000")
    assert loaded is not None and loaded.messages[-1]["content"] == "hi"


# -- SEC-03: sandbox bypass is its own approval ------------------------------------

def _bash_ctx(tmp_path, perms, sandbox):
    cfg = Config()
    cfg.sandbox = sandbox
    return ToolContext(cwd=str(tmp_path), permissions=perms, config=cfg)


def _agent(tmp_path, perms, sandbox):
    from coded.agent import create_agent
    from coded.config import ModelConfig

    cfg = Config()
    cfg.sandbox = sandbox
    model = ModelConfig(name="fake", model="fake", provider="openai-compatible",
                        base_url="http://127.0.0.1:9", api_key="k")
    cfg.add_model(model, make_default=True)
    return create_agent(model=model, config=cfg, cwd=str(tmp_path), permissions=perms,
                        stream=False, verbose=False)


def _bash_call(command, **extra):
    from coded.llm import ToolCall

    return ToolCall(id="c1", name="bash", arguments=json.dumps({"command": command, **extra}))


def test_ordinary_bash_approval_does_not_cover_running_unsandboxed(tmp_path):
    asked = []
    perms = PermissionManager(rules={"allow": ["bash"]})
    perms.allow_always("bash")
    perms.set_prompt(lambda title, detail: asked.append((title, detail)) or "no")
    agent = _agent(tmp_path, perms, {"enabled": True, "auto_allow": True})
    out, is_error, _ = agent._run_tool(
        _bash_call("echo hi > ../../escaped", dangerously_disable_sandbox=True), display=False)
    assert is_error and not (tmp_path.parent.parent / "escaped").exists()
    title, detail = asked[0]
    assert "UNSANDBOXED" in title and "echo hi > ../../escaped" in detail
    assert "filesystem: no restriction" in detail and "network: full access" in detail
    ev = [e for e in agent.events.snapshot() if e.get("kind") == "escalation"]
    assert ev and ev[-1]["allowed"] is False and ev[-1]["reason"] == "user"


def test_explicit_bypass_approval_runs_it_and_says_so(tmp_path):
    perms = PermissionManager()
    perms.set_prompt(lambda title, detail: "yes" if "UNSANDBOXED" in title else "no")
    agent = _agent(tmp_path, perms, {"enabled": True})
    out, is_error, _ = agent._run_tool(
        _bash_call("echo outside", dangerously_disable_sandbox=True), display=False)
    assert not is_error and "outside" in out and "UNSANDBOXED" in out
    assert [e for e in agent.events.snapshot() if e.get("kind") == "escalation"][-1]["allowed"]


def test_hooks_and_unattended_runs_cannot_bypass(tmp_path):
    # nobody to ask (mission / --print with an allow rule for bash): refused
    perms = PermissionManager(rules={"allow": ["bash", "*"][:1]})
    agent = _agent(tmp_path, perms, {"enabled": True})
    out, is_error, _ = agent._run_tool(
        _bash_call("echo x", dangerously_disable_sandbox=True), display=False)
    assert is_error and "nobody can approve" in out
    # a background task: refused even with a prompt available
    perms2 = PermissionManager()
    perms2.set_prompt(lambda t, d: "yes")
    perms2.set_background(lambda: True)
    agent2 = _agent(tmp_path, perms2, {"enabled": True})
    out, is_error, _ = agent2._run_tool(
        _bash_call("echo x", dangerously_disable_sandbox=True), display=False)
    assert is_error
    # a deny rule for bash still wins over an explicit bypass "yes"
    perms3 = PermissionManager(rules={"deny": ["bash(rm *)"]})
    perms3.set_prompt(lambda t, d: "yes")
    agent3 = _agent(tmp_path, perms3, {"enabled": True})
    out, is_error, _ = agent3._run_tool(
        _bash_call("rm -rf x", dangerously_disable_sandbox=True), display=False)
    assert is_error and "deny rule" in out


def test_a_missing_sandbox_backend_follows_the_policy(tmp_path, monkeypatch):
    from coded import sandbox

    monkeypatch.setattr(sandbox, "available_backend", lambda: None)
    from coded.tools.shell import BashTool

    strict = _bash_ctx(tmp_path, PermissionManager(auto_approve=True),
                       {"enabled": True, "fail_if_unavailable": True})
    res = BashTool().run({"command": "echo hi"}, strict)
    assert res.is_error and "refusing to run unsandboxed" in res.content
    # not strict: never silently unsandboxed — it is an escalation, and labelled
    lax = _bash_ctx(tmp_path, PermissionManager(), {"enabled": True})
    assert BashTool().escalation({"command": "echo hi"}, lax)["key"] == "bash_unsandboxed"
    assert "UNSANDBOXED" in BashTool().run({"command": "echo hi"}, lax).content
    # sandbox off entirely: nothing extra to approve
    off = _bash_ctx(tmp_path, PermissionManager(), {"enabled": False})
    assert BashTool().escalation({"command": "x", "dangerously_disable_sandbox": True},
                                 off) is None


# -- SEC-04: SSRF and DNS rebinding ---------------------------------------------

@pytest.mark.parametrize("url", [
    "http://localhost/", "http://127.0.0.1:8000/", "http://127.1/", "http://0.0.0.0/",
    "http://10.1.2.3/", "http://172.16.0.1/", "http://192.168.0.1/", "http://100.64.0.1/",
    "http://169.254.169.254/latest/meta-data", "http://metadata.google.internal/",
    "http://[::1]/", "http://[fd00::1]/", "http://[fe80::1]/", "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:169.254.169.254]/", "http://[64:ff9b::a9fe:a9fe]/",
    "http://[2002:7f00:1::]/", "http://[ff02::1]/", "http://224.0.0.1/",
    "http://printer.local/", "file:///etc/passwd",
])
def test_internal_destinations_are_recognised(url):
    from coded.netguard import internal_reason

    assert internal_reason(url), url


@pytest.mark.parametrize("url", ["https://93.184.216.34/", "http://[2606:4700:4700::1111]/",
                                 "https://8.8.8.8/x"])
def test_public_destinations_pass(url):
    from coded.netguard import internal_reason

    assert internal_reason(url) is None


class _Hits(BaseHTTPRequestHandler):
    hits: list = []
    redirect_to = ""

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).hits.append((self.path, self.headers.get("Host")))
        if self.path == "/go" and type(self).redirect_to:
            self.send_response(302)
            self.send_header("Location", type(self).redirect_to)
            self.end_headers()
            return
        body = b"<html><body>inside</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def local_site(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
                 "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    handler = type("H", (_Hits,), {"hits": [], "redirect_to": ""})
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield handler, srv.server_address[1]
    srv.shutdown()


def test_dns_rebinding_is_stopped_at_connection_time(tmp_path, local_site, monkeypatch):
    """The name looks public when web_fetch checks it, then rebinds to
    127.0.0.1: the request is judged on the answer it connects to."""
    from coded import netguard
    from coded.tools.web import WebFetchTool

    handler, port = local_site
    answers = iter([["93.184.216.34"]] + [["127.0.0.1"]] * 10)
    monkeypatch.setattr(netguard, "resolve", lambda host: next(answers))
    ctx = ToolContext(cwd=str(tmp_path), permissions=PermissionManager(), config=Config())
    res = WebFetchTool().run({"url": f"http://rebind.example:{port}/"}, ctx)
    assert res.is_error and "not allowed" in res.content
    assert handler.hits == []


def test_the_checked_address_is_the_one_connected_to(local_site):
    """The pinned transport sends the request to the judged address with the
    original name in the Host header (and SNI for https)."""
    from coded.websearch import fetch_url

    handler, port = local_site
    seen = []
    text = fetch_url(f"http://site.example:{port}/page",
                     allow=lambda u, r: seen.append((u, r)) or True,
                     resolver=lambda host: ["127.0.0.1"])
    assert "inside" in text
    assert handler.hits == [("/page", f"site.example:{port}")]
    assert seen[0][1] and "site.example" in seen[0][1]  # judged internal, then approved


def test_a_redirect_to_a_private_address_is_blocked(local_site):
    from coded.netguard import DestinationRefused
    from coded.websearch import fetch_url

    handler, port = local_site
    handler.redirect_to = f"http://127.0.0.1:{port}/secret"
    resolver = {"public.example": ["127.0.0.1"], "127.0.0.1": ["127.0.0.1"]}

    def allow(url, reason):  # the first host is approved, nothing internal after it
        return "public.example" in url

    with pytest.raises(DestinationRefused):
        fetch_url(f"http://public.example:{port}/go", allow=allow,
                  resolver=lambda h: resolver[h])
    assert [h[0] for h in handler.hits] == ["/go"]


def test_tls_keeps_the_hostname_for_sni_and_certificates(monkeypatch):
    import httpx

    from coded.netguard import pinned_transport

    sent = []
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request",
                        lambda self, req: sent.append(req) or httpx.Response(200, text="ok"))
    t = pinned_transport(lambda u, r: True, resolver=lambda h: ["93.184.216.34"])
    with httpx.Client(transport=t) as client:
        client.get("https://docs.example.org/x")
    req = sent[0]
    assert req.url.host == "93.184.216.34"
    assert req.extensions["sni_hostname"] == "docs.example.org"
    assert req.headers["Host"] == "docs.example.org"


def test_a_public_page_still_loads_through_the_tool(tmp_path, local_site, monkeypatch):
    """Legitimate access keeps working: a host that resolves publicly is not
    asked about (resolution faked to a public address; connection is local)."""
    import httpx

    from coded import netguard
    from coded.tools.web import WebFetchTool

    monkeypatch.setattr(netguard, "resolve", lambda host: ["93.184.216.34"])
    sent = []

    def fake(self, req):
        sent.append(req)
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text="<p>public docs</p>", request=req)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake)
    ctx = ToolContext(cwd=str(tmp_path), permissions=PermissionManager(), config=Config())
    res = WebFetchTool().run({"url": "https://docs.example.org/"}, ctx)
    assert not res.is_error and "public docs" in res.content
    assert sent[0].url.host == "93.184.216.34"


def test_browser_popups_and_subrequests_cannot_reach_internal_addresses(tmp_path,
                                                                        local_site):
    import glob

    pytest.importorskip("playwright")
    from coded.tools.browser import BrowserTool

    handler, port = local_site
    page = tmp_path / "app.html"
    page.write_text(
        "<html><body>hello"
        f"<script>window.open('http://127.0.0.1:{port}/popup');"
        f"fetch('http://127.0.0.1:{port}/xhr').catch(()=>0);</script>"
        f"<iframe src='http://127.0.0.1:{port}/frame'></iframe></body></html>")
    found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
    exe = os.environ.get("CODED_TEST_CHROME") or (found[-1] if found else "")
    ctx = ToolContext(cwd=str(tmp_path), permissions=PermissionManager(), config=Config())
    try:
        res = BrowserTool(exe).run({"url": page.as_uri(), "screenshot": False,
                                    "steps": [{"action": "wait", "value": "500"}]}, ctx)
    except Exception as exc:  # pragma: no cover
        pytest.skip(str(exc))
    if res.is_error and "Chromium is not installed" in res.content:
        pytest.skip(res.content)
    assert not res.is_error, res.content
    assert handler.hits == []  # nothing left the browser for 127.0.0.1
    assert f"127.0.0.1:{port}/xhr" in res.content and f"127.0.0.1:{port}/frame" in res.content

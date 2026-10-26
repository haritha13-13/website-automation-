import json

SYSTEM_RULES = """You analyze web page form structure for a content-publishing automation.
You receive a JSON list of controls OBSERVED by a browser on one page. You are text-only.

Hard rules:
1. Use ONLY selectors that appear in an element's "selector" field, or a short Playwright selector
   that clearly targets exactly one observed element (e.g. button:has-text('Publish') for a button whose
   observed text is 'Publish'). Never invent ids, names or elements that are not in the data.
2. If a control is not present, set "selector" to null and give a short "reason". Do not guess.
3. "confidence" is your own 0-1 estimate. Lower it when labels are vague or several candidates fit.
4. Prefer stable selectors: #id, then tag[name=..], then text-based button selectors.
5. If several elements could match, pick none and explain in "reason", and set requires_human_review true.
6. page_type is "article_editor" only if the page has a title field AND a body editor. If the page is a
   login form, dashboard or list, say so and put a concrete next inspection step in next_step_suggestion.
7. List every field name with a null selector in missing_fields.
Return only JSON matching the required schema."""


def build_user_prompt(observation: dict) -> str:
    return "Observed page data (sanitized):\n" + json.dumps(observation, ensure_ascii=False, indent=1)


NAV_RULES = """You help an automation reach the page where a NEW ARTICLE / POST / PROJECT is written in a CMS or portfolio site.
You receive the sanitized observation of the current page. Its "nav_candidates" are the links and buttons that can be
clicked right now. Choose up to 3 of them, best first, that most likely lead to content creation. Think about what a
signed-in author would click: "Create", "New", "Add", "Write", "Upload", "Post", "Project", "Article", or a section such
as "Content", "Posts", "Projects" that leads to them. A "Create" button that opens a menu is a good choice.
Rules:
1. Each "text" MUST be copied exactly from nav_candidates, with its "kind". Never invent one.
2. Ignore marketing and account links (hiring, jobs, pricing, help, terms, careers, downloads, settings, logout).
3. Never choose anything that deletes, removes, logs out, or changes account settings.
4. If nothing plausibly leads toward content creation, return an empty list.
Return only JSON matching the schema."""


def build_nav_prompt(observation: dict) -> str:
    return "Current page (sanitized):\n" + json.dumps(observation, ensure_ascii=False, indent=1)

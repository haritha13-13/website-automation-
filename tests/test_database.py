from services.database import Database


def _db():
    return Database(":memory:")


def test_claim_blocks_duplicates():
    db = _db()
    run = db.start_run("f")
    a = db.add_item(run, 2, "site", "u", "T", "h1", "publish", "validated")
    b = db.add_item(run, 3, "site", "u", "T", "h1", "publish", "validated")
    assert db.claim(a) is True
    assert db.claim(b) is False  # identical article already in progress
    assert db.get_item(b)["status"] == "validated"


def test_non_blocking_states_allow_retry():
    db = _db()
    run = db.start_run("f")
    a = db.add_item(run, 2, "site", "u", "T", "h1", "draft", "rejected_by_site")
    b = db.add_item(run, 3, "site", "u", "T", "h1", "draft", "validated")
    assert db.find_blocking("site", "h1", exclude_item=b) is None and db.claim(b)


def test_other_site_or_hash_not_blocked_and_events():
    db = _db()
    run = db.start_run("f")
    db.add_item(run, 2, "siteA", "u", "T", "h1", "draft", "published")
    assert db.find_blocking("siteB", "h1") is None and db.find_blocking("siteA", "h2") is None
    assert db.find_blocking("siteA", "h1")["status"] == "published"
    db.log(run, "stage", "hello")
    assert db.events_for_run(run)[0]["message"] == "hello"
    db.finish_run(run, "completed")

import hashlib
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from bosshunter.executor import resume_images
from bosshunter.executor.resume_images import (
    attach_resume_images,
    pick_file_input,
    rank_file_input,
    resume_image_settings,
)

DISCOVER = resume_images.JS_DISCOVER_FILE_INPUTS
CONFIRM = resume_images.JS_CLICK_UPLOAD_CONFIRM


def _config(paths, enabled=True):
    return {"profile": {"resume_images": paths, "send_resume_images": enabled}}


def test_settings_disabled_by_default():
    settings = resume_image_settings(_config([], enabled=False))
    assert settings["enabled"] is False
    assert settings["paths"] == []
    assert settings["ready"] is False


def test_settings_reports_missing_and_unsupported_files(tmp_path):
    missing = str(tmp_path / "nope.png")
    wrong = tmp_path / "resume.pdf"
    wrong.write_text("not an image")
    settings = resume_image_settings(_config([missing, str(wrong)]))

    assert settings["enabled"] is True
    assert settings["ready"] is False
    reasons = {entry["path"]: entry["reason"] for entry in settings["entries"]}
    assert "文件不存在" in reasons[missing]
    assert "不支持的格式" in reasons[str(wrong)]


def test_settings_accepts_png_and_records_size(tmp_path):
    image = tmp_path / "resume.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    settings = resume_image_settings(_config([str(image)]))

    assert settings["ready"] is True
    assert settings["paths"] == [str(image)]
    assert settings["entries"][0]["size"] == len(image.read_bytes())


def test_rank_prefers_image_accept_over_visible_generic_input():
    image_picker = {"accept": "image/png,image/jpeg", "multiple": True, "rendered": False}
    visible_generic = {"accept": "*/*", "multiple": False, "rendered": True}
    assert rank_file_input(image_picker) > rank_file_input(visible_generic)
    assert pick_file_input([visible_generic, image_picker]) is image_picker


def test_pick_returns_none_without_inputs():
    assert pick_file_input([]) is None


class _FakePage:
    """Drives attach_resume_images with scripted responses per exact script.

    Dispatch has to be exact-string based: several of the page scripts share
    substrings (``blob:`` appears in both the pending and message-image probes),
    so a ``in``-based router silently mislabels them.
    """

    def __init__(self, inputs, pending=0, message_images=0, confirm=None):
        self.inputs = inputs
        self.pending = pending
        self.message_images = message_images
        self.urls = [f"https://img.example/old-{index}" for index in range(message_images)]
        self.identity = ["data-chat-id=chat-123"]
        self.confirm = confirm or {"ok": False, "error": "no_confirm_button"}
        self.calls = []

    def evaluate(self, target_id, expression, timeout=30):
        import json

        self.calls.append(expression)
        if expression == DISCOVER:
            return json.dumps({"ok": True, "url": "https://www.zhipin.com/web/geek/chat",
                               "count": len(self.inputs), "inputs": self.inputs})
        if expression == resume_images.JS_CAPTURE_CONVERSATION:
            return json.dumps({"ok": True, "identity": self.identity, "images": [
                {"url": url, "fingerprint": hashlib.sha256(b"0" * 16).hexdigest()} for url in self.urls
            ]})
        if expression.startswith("(async () => {\n    const wanted"):
            return json.dumps(self.confirm)
        # marker / set-files acknowledge
        return True


def test_attach_skipped_when_not_enabled(tmp_path):
    image = tmp_path / "resume.png"
    image.write_bytes(b"0" * 16)
    page = _FakePage([])

    with patch("bosshunter.executor.resume_images.evaluate", page.evaluate), patch(
        "bosshunter.executor.resume_images.set_files", return_value=True
    ):
        report = attach_resume_images("t", _config([str(image)], enabled=False))

    assert report["skipped"] is True
    assert report["success"] is False


def test_attach_reports_unusable_paths_without_touching_browser(tmp_path):
    page = _FakePage([])
    with patch("bosshunter.executor.resume_images.evaluate", page.evaluate):
        report = attach_resume_images("t", _config(["/definitely/missing.png"]), job={"id": "1"})

    assert report["success"] is False
    assert "missing.png" in report["reason"]
    assert page.calls == []


def test_attach_without_file_input_is_reported_not_raised(tmp_path):
    image = tmp_path / "resume.png"
    image.write_bytes(b"0" * 16)
    page = _FakePage([])

    with patch("bosshunter.executor.resume_images.evaluate", page.evaluate), patch(
        "bosshunter.executor.resume_images.set_files", return_value=True
    ):
        report = attach_resume_images("t", _config([str(image)]), job={"id": "1", "company": "示例"})

    assert report["reason"] == "no_file_input"
    assert report["success"] is False


def test_attach_success_increments_message_images(tmp_path, monkeypatch):
    images = []
    for name in ("a.png", "b.png"):
        path = tmp_path / name
        path.write_bytes(b"0" * 16)
        images.append(str(path))
    # 2 inputs: a visible generic one and a hidden image picker.
    inputs = [
        {"index": 0, "accept": "*/*", "multiple": False, "rendered": True, "ancestors": "form"},
        {"index": 1, "accept": "image/*", "multiple": True, "rendered": False, "ancestors": "div.chat-input"},
    ]
    page = _FakePage(inputs, pending=1, message_images=0)

    def stage_files(target_id, selector, files):
        # Uploading the batch is what makes the new bubbles appear.
        page.message_images += 2
        page.urls.extend(["https://img.example/resume-a", "https://img.example/resume-b"])
        return True

    with patch("bosshunter.executor.resume_images.evaluate", page.evaluate), patch(
        "bosshunter.executor.resume_images.set_files", side_effect=stage_files
    ) as set_files:
        report = attach_resume_images("t", _config(images), job={"id": "1", "company": "示例"})

    assert report["success"] is True
    assert report["images_before"] == 0
    assert report["images_after"] == 2
    assert len(report["files"]) == 2
    # The image picker was marked and both files pushed in a single call.
    selector = "input[data-bh-resume-input]"
    assert set_files.call_count == 1
    set_files.assert_called_once_with("t", selector, images)


def test_attach_does_not_treat_unrelated_chat_images_as_resume_receipt(tmp_path):
    images = []
    for name in ("a.png", "b.png"):
        path = tmp_path / name
        path.write_bytes(b"0" * 16)
        images.append(str(path))
    inputs = [{"index": 0, "accept": "image/*", "multiple": True, "rendered": False, "ancestors": "div.chat"}]
    # These may be HR screenshots or other user images; count alone proves nothing.
    page = _FakePage(inputs, pending=0, message_images=2)

    with patch("bosshunter.executor.resume_images.evaluate", page.evaluate), patch(
        "bosshunter.executor.resume_images.set_files", return_value=True
    ) as set_files:
        report = attach_resume_images(
            "t", _config(images), job={"id": "2", "company": "多益网络"}, max_wait_seconds=0
        )

    assert report.get("skipped") is not True
    assert set_files.call_count == 1, "未匹配到本功能上传回执时，不得把普通聊天图片当成简历"
    assert DISCOVER in page.calls



def test_attach_reports_when_marking_the_input_fails(tmp_path):
    image = tmp_path / "only.png"
    image.write_bytes(b"0" * 16)
    inputs = [{"index": 0, "accept": "image/*", "multiple": True, "rendered": False, "ancestors": "div.chat"}]
    page = _FakePage(inputs, pending=1, message_images=0)

    def refuse_marker(target_id, expression, timeout=30):
        if "removeAttribute" in expression:
            return False
        return page.evaluate(target_id, expression, timeout)
    with patch("bosshunter.executor.resume_images.evaluate", side_effect=refuse_marker), patch(
        "bosshunter.executor.resume_images.set_files", return_value=True
    ) as set_files:
        report = attach_resume_images("t", _config([str(image)]))

    assert report["reason"] == "mark_file_input_failed"
    assert set_files.call_count == 0


def test_attach_reports_when_image_never_appears(tmp_path):
    image = tmp_path / "only.png"
    image.write_bytes(b"0" * 16)
    inputs = [{"index": 0, "accept": "image/*", "multiple": True, "rendered": False, "ancestors": "div.chat"}]
    page = _FakePage(inputs, pending=0, message_images=0)

    with patch("bosshunter.executor.resume_images.evaluate", page.evaluate), patch(
        "bosshunter.executor.resume_images.set_files", return_value=True
    ), patch("bosshunter.executor.resume_images.time.sleep"):
        report = attach_resume_images("t", _config([str(image)]), max_wait_seconds=1.0)

    assert report["success"] is False
    assert report["reason"] == "image_not_confirmed_in_conversation"


def test_attach_stops_early_when_stop_event_is_set(tmp_path):
    image = tmp_path / "stop.png"
    image.write_bytes(b"0" * 16)
    inputs = [{"index": 0, "accept": "image/*", "multiple": True, "rendered": False, "ancestors": "div.chat"}]

    class StopEvent:
        def is_set(self):
            return True

    page = _FakePage(inputs)
    with patch("bosshunter.executor.resume_images.evaluate", page.evaluate), patch(
        "bosshunter.executor.resume_images.set_files", return_value=True
    ):
        report = attach_resume_images("t", _config([str(image)]), stop_event=StopEvent())

    assert report["success"] is False
    assert report["reason"] == "task_stopped"


def test_settings_tolerates_non_list_and_missing_section():
    assert resume_image_settings({})["paths"] == []
    assert resume_image_settings({"profile": {"resume_images": "nope"}})["paths"] == []
    assert resume_image_settings({"profile": {"resume_images": ["", "  "]}})["paths"] == []


def test_attach_fails_closed_when_conversation_has_no_stable_identity(tmp_path):
    image = tmp_path / "resume.png"
    image.write_bytes(b"0" * 16)
    page = _FakePage([])
    page.identity = []

    with patch("bosshunter.executor.resume_images.evaluate", page.evaluate), patch(
        "bosshunter.executor.resume_images.set_files", return_value=True
    ) as set_files:
        report = attach_resume_images("t", _config([str(image)]))

    assert report["reason"] == "conversation_identity_unavailable"
    assert set_files.call_count == 0


def test_history_receipt_matches_chat_files_and_visible_uploaded_urls():
    import json
    import sqlite3

    from bosshunter.executor.sender import _resume_images_already_sent

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, action TEXT, detail TEXT)")
    receipt = {
        "schema": "resume_images.v2",
        "conversation_key": "chat-hash",
        "file_fingerprints": ["file-a", "file-b"],
        "image_urls": ["https://img.example/a", "https://img.example/b"],
    }
    db.execute("INSERT INTO history (action, detail) VALUES (?, ?)", ("resume_images", json.dumps(receipt)))

    assert _resume_images_already_sent(
        db, "chat-hash", ["file-a", "file-b"], receipt["image_urls"]
    )
    assert not _resume_images_already_sent(db, "other-chat", ["file-a", "file-b"], receipt["image_urls"])
    assert not _resume_images_already_sent(db, "chat-hash", ["changed-file"], receipt["image_urls"])
    assert not _resume_images_already_sent(db, "chat-hash", ["file-a", "file-b"], ["https://img.example/a"])
    assert _resume_images_already_sent(
        sqlite3.connect(":memory:"), "chat-hash", ["file-a"], ["https://img.example/a"]
    ) is None


def test_upload_attempt_fence_covers_shared_hr_partial_and_unknown_results():
    from bosshunter.executor.sender import _claim_resume_image_attempt

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, job_id TEXT, action TEXT, detail TEXT)")
    assert _claim_resume_image_attempt(db, "job-1", "chat-a", ["page-1", "page-2"])
    assert not _claim_resume_image_attempt(db, "job-2", "chat-a", ["page-1", "page-2"])
    assert not _claim_resume_image_attempt(db, "job-2", "chat-a", ["changed-file"])
    assert _claim_resume_image_attempt(db, "job-3", "chat-b", ["page-1"])
    db.execute("INSERT INTO history (job_id, action, detail) VALUES ('legacy', 'resume_images', 'old-file.png')")
    db.commit()
    assert not _claim_resume_image_attempt(db, "legacy", "chat-c", ["page-1"])


@pytest.fixture(scope="module")
def offline_browser():
    from patchright.sync_api import sync_playwright

    with sync_playwright() as runtime:
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        executable = str(chrome) if chrome.exists() else runtime.chromium.executable_path
        if not Path(executable).exists():
            pytest.skip("Offline DOM regression requires an installed Chromium browser")
        browser = runtime.chromium.launch(executable_path=executable, headless=True)
        yield browser
        browser.close()


@pytest.fixture
def offline_page(offline_browser):
    context = offline_browser.new_context(service_workers="block")
    # No site request reaches the network or any existing login/profile.
    context.route("**/*", lambda route: route.fulfill(
        status=200, content_type="text/html", body="<html><body></body></html>"
    ))
    page = context.new_page()
    page.goto("https://www.zhipin.com/web/geek/chat")
    yield page
    context.close()


def _preview(page, disabled=False, fingerprint_bytes=b"resume-png"):
    return page.evaluate("""async ({disabled, data}) => {
        document.body.innerHTML = `<div class="chat-conversation" data-chat-id="hr-1"></div>
            <input type="file" data-bh-resume-input="1">
            <button id="text-send" onclick="document.body.dataset.textClicks++">发送</button>
            <div role="dialog"><button onclick="document.body.dataset.otherClicks++">确认</button></div>
            <div class="upload-preview"><img><button id="image-send" onclick="document.body.dataset.imageClicks++"><span>发送</span></button></div>`;
        document.body.dataset.textClicks = 0; document.body.dataset.otherClicks = 0; document.body.dataset.imageClicks = 0;
        const file = new File([new Uint8Array(data)], 'resume.png', {type:'image/png'});
        const files = new DataTransfer(); files.items.add(file);
        document.querySelector('input').files = files.files;
        const img = document.querySelector('img'); img.src = URL.createObjectURL(file);
        // Decode state is modeled separately; content still passes through real Blob/File/fetch/crypto.
        Object.defineProperty(img, 'complete', {value: true, configurable: true});
        Object.defineProperty(img, 'naturalWidth', {value: 10, configurable: true});
        document.querySelector('#image-send').disabled = disabled;
    }""", {"disabled": disabled, "data": list(fingerprint_bytes)})


@pytest.mark.parametrize("disabled,expected", [(False, 1), (True, 0)])
def test_confirmation_clicks_only_verified_attachment_preview(offline_page, disabled, expected):
    _preview(offline_page, disabled)
    digest = hashlib.sha256(b"resume-png").hexdigest()
    result = offline_page.evaluate(resume_images._upload_confirm_script([digest]))
    assert offline_page.evaluate("[Number(document.body.dataset.textClicks), Number(document.body.dataset.otherClicks), Number(document.body.dataset.imageClicks)]") == [0, 0, expected], result


@pytest.mark.parametrize("mutation", [
    "document.querySelector('.upload-preview').className='unrelated-dialog'",
    "document.querySelector('img').remove()",
    "document.querySelector('img').style.display='none'; Object.defineProperty(document.querySelector('img'), 'naturalWidth', {value:0})",
    "document.querySelector('.upload-preview').setAttribute('aria-busy','true')",
    "document.body.insertAdjacentHTML('beforeend', '<div style=\"position:fixed;inset:0;z-index:999\"></div>')",
])
def test_confirmation_refuses_unknown_incomplete_busy_or_obscured_preview(offline_page, mutation):
    _preview(offline_page)
    offline_page.evaluate(mutation)
    offline_page.evaluate(resume_images._upload_confirm_script([hashlib.sha256(b"resume-png").hexdigest()]))
    assert offline_page.evaluate("Number(document.body.dataset.textClicks) + Number(document.body.dataset.otherClicks) + Number(document.body.dataset.imageClicks)") == 0


def test_confirmation_rejects_wrong_file_content(offline_page):
    _preview(offline_page)
    offline_page.evaluate(resume_images._upload_confirm_script([hashlib.sha256(b"different").hexdigest()]))
    assert offline_page.evaluate("Number(document.body.dataset.imageClicks)") == 0


def test_snapshot_excludes_hr_images_and_identifies_outbound_content(offline_page):
    data = b"verified-resume"
    offline_page.route("https://img.bosszhipin.com/**", lambda route: route.fulfill(
        status=200, content_type="image/png", body=data, headers={"Access-Control-Allow-Origin": "*"}
    ))
    offline_page.set_content("""<div class="chat-conversation" data-chat-id="hr-1">
        <div class="chat-message item-friend"><img class="message-image" src="https://img.bosszhipin.com/hr.png"></div>
        <div class="chat-message message-self"><img class="message-image" src="https://img.bosszhipin.com/resume.png?token=test"></div>
        <div class="chat-message"><img class="message-image" src="https://img.bosszhipin.com/unknown.png"></div>
        </div>""")
    snapshot = json.loads(offline_page.evaluate(resume_images.JS_CAPTURE_CONVERSATION))
    assert snapshot["images"] == [{"url": "https://img.bosszhipin.com/resume.png", "fingerprint": hashlib.sha256(data).hexdigest()}]
    assert "token=" not in json.dumps(snapshot)


def test_generic_chat_container_and_same_hr_name_do_not_count_as_identity(offline_page):
    offline_page.set_content('<div id="chat-conversation" class="chat-conversation"></div><div class="friend-content selected"><span class="name-text">HR</span></div>')
    assert json.loads(offline_page.evaluate(resume_images.JS_CAPTURE_CONVERSATION))["ok"] is False


@pytest.mark.parametrize("confirmed", [True, False])
def test_attacher_deduplicates_shared_conversation_after_success_or_unknown_result(tmp_path, confirmed):
    from bosshunter.executor import sender

    path = tmp_path / "resume.png"
    path.write_bytes(b"resume")
    fingerprints = resume_images.resume_image_fingerprints([str(path)])
    snapshot = {"conversation_key": "shared-chat", "urls": ["https://img.example/resume"]}
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, job_id TEXT, action TEXT, detail TEXT)")

    def upload(*args, **kwargs):
        assert db.execute("SELECT COUNT(*) FROM history WHERE action='resume_images_attempt'").fetchone()[0] == 1
        return {"success": confirmed, "conversation_key": "shared-chat",
                "file_fingerprints": fingerprints, "image_urls": snapshot["urls"]}

    with patch.object(sender, "capture_conversation_images", return_value=snapshot), patch.object(
        sender, "attach_resume_images", side_effect=upload
    ) as uploader:
        attach = sender._make_resume_image_attacher(_config([str(path)]), db)
        first = attach("t", {"id": "job-a"}, "", None)
        second = attach("t", {"id": "job-b"}, "", None)

    assert first["success"] == confirmed
    assert uploader.call_count == 1
    assert second["reason"] == ("already_sent" if confirmed else "resume_image_attempt_requires_manual_review")


def test_concurrent_jobs_can_only_claim_one_upload_for_same_hr(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from bosshunter.executor.sender import _claim_resume_image_attempt

    ledger = tmp_path / "ledger.db"
    with sqlite3.connect(ledger) as db:
        db.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, job_id TEXT, action TEXT, detail TEXT)")
    barrier = Barrier(2)

    def claim(job_id):
        with sqlite3.connect(ledger) as db:
            barrier.wait(timeout=5)
            return _claim_resume_image_attempt(db, job_id, "shared-chat", ["same-resume"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, ["job-a", "job-b"]))
    assert sorted(outcomes) == [False, True]


@pytest.mark.parametrize("mutation", [
    "document.querySelector('.chat-conversation').setAttribute('data-chat-id', 'other-hr')",
    "document.querySelector('.upload-preview').setAttribute('aria-busy', 'true')",
    "document.querySelector('#image-send').disabled = true",
])
def test_confirmation_rechecks_state_after_asynchronous_attachment_hashing(offline_page, mutation):
    _preview(offline_page)
    offline_page.evaluate("""mutation => {
        const original = window.fetch;
        window.fetch = async (...args) => {
            const response = await original(...args);
            new Function(mutation)();
            return response;
        };
    }""", mutation)
    offline_page.evaluate(resume_images._upload_confirm_script([hashlib.sha256(b"resume-png").hexdigest()]))
    assert offline_page.evaluate("Number(document.body.dataset.imageClicks)") == 0


def test_matching_outbound_content_handles_legacy_receipts_without_reupload(tmp_path):
    from bosshunter.executor import sender

    path = tmp_path / "resume.png"
    path.write_bytes(b"resume")
    snapshot = {"conversation_key": "chat-a", "urls": ["https://img.example/resume"],
                "fingerprints": resume_images.resume_image_fingerprints([str(path)])}
    with patch.object(sender, "capture_conversation_images", return_value=snapshot), patch.object(
        sender, "attach_resume_images"
    ) as uploader:
        attach = sender._make_resume_image_attacher(_config([str(path)]), None)
        result = attach("t", {"id": "legacy-job"}, "", None)
    assert result["reason"] == "verified_resume_images_present"
    assert result["success"] is True
    uploader.assert_not_called()

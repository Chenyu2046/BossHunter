"""Best-effort resume-image attachment after a greeting is delivered.

This module is deliberately isolated from the greeting delivery semantics.
BossHunter only marks a job ``sent`` once the greeting text is *verified* in the
conversation — that guarantee is what prevents duplicate greetings. Resume
images are an additive extra, so every failure path here is reported and
swallowed: an image that will not upload must never convert an already
delivered greeting into a failed send.

The BOSS chat upload control is a hidden ``<input type="file">`` behind a
toolbar button. We do not hard-code a single selector, because BOSS renames
those classes without notice. Instead we enumerate every file input on the
conversation page, rank them by how likely they are the image picker, tag the
winner with a data attribute, and push the files into that node through CDP
``DOM.setFileInputFiles``. Everything we discover is logged, so the selector can
be pinned after the first live run (see ``bosshunter resume-images --probe``).
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

from bosshunter.browser import evaluate, set_files

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

# Marked onto the chosen file input so CDP can target it without a brittle path.
_INPUT_TAG = "data-bh-resume-input"

# Collected before upload so we can report what the page actually offered.
JS_DISCOVER_FILE_INPUTS = """
(() => {
    const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
    return JSON.stringify({
        ok: true,
        url: location.href,
        count: inputs.length,
        inputs: inputs.map((input, index) => {
            const rect = input.getBoundingClientRect();
            const chain = [];
            let node = input;
            for (let depth = 0; depth < 3 && node && node.parentElement; depth += 1) {
                node = node.parentElement;
                const cls = typeof node.className === 'string' && node.className.trim()
                    ? '.' + node.className.trim().split(/\\s+/).join('.')
                    : '';
                chain.unshift(node.tagName.toLowerCase() + cls);
            }
            return {
                index,
                accept: (input.getAttribute('accept') || '').slice(0, 80),
                multiple: !!input.multiple,
                rendered: rect.width > 0 && rect.height > 0,
                ancestors: chain.slice(-2).join(' > '),
            };
        }),
    });
})()
"""

JS_CAPTURE_CONVERSATION = r"""
(async () => {
    const root = document.querySelector('.chat-conversation');
    if (!root || location.protocol !== 'https:' || location.hostname !== 'www.zhipin.com') {
        return JSON.stringify({ok: false});
    }
    const selected = document.querySelector('.friend-content.selected');
    const row = selected && selected.closest('li[role="listitem"]');
    const readIdentity = () => {
    const identity = [];
    for (const node of [selected, row, root].filter(Boolean)) {
        for (const attr of ['data-chat-id', 'data-session-id', 'data-uid', 'data-boss-id']) {
            const value = node.getAttribute(attr);
            if (value) identity.push(attr + '=' + value);
        }
        if (identity.length) break;
    }
    return identity;
    };
    const identity = readIdentity();
    if (!identity.length) return JSON.stringify({ok: false});
    const urls = new Set();
    // Only explicit outbound message containers can provide a send receipt.
    const outgoing = '.item-myself, .message-self, .msg-self, .is-self, .my-message, ' +
        '.message-mine, .from-me, .outgoing';
    root.querySelectorAll('img.message-image, .message-img img, .msg-img img').forEach((img) => {
        const message = img.closest('.chat-message, .message-item, .item-myself');
        if (!message || !message.matches(outgoing)) return;
        try {
            const url = new URL(img.getAttribute('src') || '', location.href);
            if (url.protocol === 'https:' &&
                (url.hostname === location.hostname || url.hostname.endsWith('.bosszhipin.com'))) {
                urls.add(url.href);
            }
        } catch (_) { /* An invalid source is not a receipt. */ }
    });
    const images = await Promise.all(Array.from(urls).map(async (url) => {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 1500);
        try {
            const response = await fetch(url, {signal: controller.signal, credentials: 'omit', redirect: 'error'});
            if (!response.ok) return null;
            const data = await response.arrayBuffer();
            const digest = await crypto.subtle.digest('SHA-256', data);
            const fingerprint = Array.from(new Uint8Array(digest), n => n.toString(16).padStart(2, '0')).join('');
            // Query strings may contain signed credentials; never persist them.
            const parsed = new URL(url);
            return {url: parsed.origin + parsed.pathname, fingerprint};
        } catch (_) {
            return null;
        } finally {
            clearTimeout(timer);
        }
    }));
    if (root !== document.querySelector('.chat-conversation') ||
        selected !== document.querySelector('.friend-content.selected') ||
        JSON.stringify(identity) !== JSON.stringify(readIdentity())) return JSON.stringify({ok: false});
    return JSON.stringify({ok: true, identity, images: images.filter(Boolean)});
})()
"""

# BOSS sometimes opens a preview pane instead of sending the file immediately.
JS_CLICK_UPLOAD_CONFIRM = """
(async () => {
    const wanted = ['发送', '确定', '确认', '发送图片'];
    const conversation = document.querySelector('.chat-conversation');
    const selected = document.querySelector('.friend-content.selected');
    const identity = () => [selected, selected && selected.closest('li[role="listitem"]'), conversation]
        .filter(Boolean).map(node => ['data-chat-id', 'data-session-id', 'data-uid', 'data-boss-id']
            .map(attr => node.getAttribute(attr) || '').join('|')).join(';');
    const initialIdentity = identity();
    const expectedFingerprints = __EXPECTED_FINGERPRINTS__;
    const input = document.querySelector('input[data-bh-resume-input]');
    if (!input || input.files.length !== expectedFingerprints.length) {
        return JSON.stringify({ok: false, error: 'attachment_mismatch'});
    }
    const selectedFiles = Array.from(input.files);
    const actual = await Promise.all(selectedFiles.map(async (file) => {
        const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
        return Array.from(new Uint8Array(digest), n => n.toString(16).padStart(2, '0')).join('');
    }));
    if (JSON.stringify(actual) !== JSON.stringify(expectedFingerprints)) {
        return JSON.stringify({ok: false, error: 'attachment_mismatch'});
    }
    const expectedCount = expectedFingerprints.length;
    const previewSelector = [
        '[class*="upload"][class*="preview"]',
        '[class*="preview"][class*="upload"]', '[class*="attachment"][class*="preview"]',
        '[class*="preview"][class*="attachment"]', '[class*="image"][class*="preview"]',
        '[class*="preview"][class*="image"]'
    ].join(',');
    const visible = (node) => {
        const rect = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        return rect.width > 0 && rect.height > 0 && style.display !== 'none' &&
            style.visibility !== 'hidden' && Number(style.opacity || 1) > 0;
    };
    const readyPreview = (root) => {
        if (!root.isConnected || !root.matches(previewSelector) || !visible(root)) return false;
        const previews = Array.from(root.querySelectorAll('img[src^="blob:"]'));
        if (previews.length !== expectedCount || previews.some((img) => !img.complete || img.naturalWidth <= 0)) {
            return false;
        }
        if (root.matches('[aria-busy="true"], .loading, [class*="uploading"], [class*="progress"]') ||
            root.querySelector('[aria-busy="true"], .loading, [class*="uploading"], [class*="progress"]')) {
            return false;
        }
        const buttons = Array.from(root.querySelectorAll('button, a, [role="button"]')).filter((button) => {
            const text = (button.innerText || button.textContent || '').trim();
            return wanted.includes(text) && visible(button);
        });
        if (buttons.length !== 1) return false;
        const button = buttons[0];
        if (button.disabled || button.getAttribute('aria-disabled') === 'true' ||
            /(^|\\s)disabled(\\s|$)/i.test(String(button.className || ''))) return false;
        return true;
    };
    const candidates = Array.from(document.querySelectorAll(previewSelector)).filter(readyPreview);
    const innermost = candidates.filter(root => !candidates.some(other => other !== root && root.contains(other)));
    if (innermost.length === 1) {
        const previews = Array.from(innermost[0].querySelectorAll('img[src^="blob:"]'));
        const previewUrls = previews.map(img => img.src);
        let previewFingerprints;
        try {
            previewFingerprints = await Promise.all(previews.map(async img => {
                const data = await (await fetch(img.src)).arrayBuffer();
                const digest = await crypto.subtle.digest('SHA-256', data);
                return Array.from(new Uint8Array(digest), n => n.toString(16).padStart(2, '0')).join('');
            }));
        } catch (_) { return JSON.stringify({ok: false, error: 'unverified_preview'}); }
        if (!readyPreview(innermost[0]) || input.files.length !== selectedFiles.length ||
            selectedFiles.some((file, index) => input.files[index] !== file) ||
            JSON.stringify(previewUrls) !== JSON.stringify(Array.from(
                innermost[0].querySelectorAll('img[src^="blob:"]'), img => img.src)) ||
            JSON.stringify(previewFingerprints.sort()) !== JSON.stringify([...expectedFingerprints].sort()) ||
            identity() !== initialIdentity || conversation !== document.querySelector('.chat-conversation') ||
            selected !== document.querySelector('.friend-content.selected')) {
            return JSON.stringify({ok: false, error: 'preview_or_conversation_changed'});
        }
        const button = Array.from(innermost[0].querySelectorAll('button, a, [role="button"]'))
            .find((node) => wanted.includes((node.innerText || node.textContent || '').trim()) && visible(node));
        if (!button || button.disabled || button.getAttribute('aria-disabled') === 'true') {
            return JSON.stringify({ok: false, error: 'button_unavailable'});
        }
        const rect = button.getBoundingClientRect();
        const top = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
        if (!top || !button.contains(top)) return JSON.stringify({ok: false, error: 'button_obscured'});
        button.click();
        return JSON.stringify({ ok: true, clicked: (button.innerText || button.textContent || '').trim() });
    }
    return JSON.stringify({ ok: false, error: 'no_confirm_button' });
})()
"""


def _upload_confirm_script(fingerprints: list[str]) -> str:
    return JS_CLICK_UPLOAD_CONFIRM.replace("__EXPECTED_FINGERPRINTS__", json.dumps(fingerprints))


def capture_conversation_images(target_id: str) -> dict[str, Any]:
    parsed = _parse_js_result(evaluate(target_id, JS_CAPTURE_CONVERSATION))
    identity = parsed.get("identity") if isinstance(parsed.get("identity"), list) else []
    images = parsed.get("images") if isinstance(parsed.get("images"), list) else []
    if not parsed.get("ok") or not identity:
        return {"conversation_key": "", "urls": [], "fingerprints": []}
    material = "\n".join(sorted(str(value) for value in identity if value))
    key = hashlib.sha256(material.encode("utf-8")).hexdigest()
    verified = [image for image in images if isinstance(image, dict)
                and isinstance(image.get("url"), str) and isinstance(image.get("fingerprint"), str)]
    return {"conversation_key": key, "urls": [image["url"] for image in verified],
            "fingerprints": [image["fingerprint"] for image in verified]}


def resume_image_fingerprints(paths: list[str]) -> list[str]:
    fingerprints = []
    for path in paths:
        digest = hashlib.sha256()
        with open(path, "rb") as image_file:
            for chunk in iter(lambda: image_file.read(65536), b""):
                digest.update(chunk)
        fingerprints.append(digest.hexdigest())
    return fingerprints


def _parse_js_result(result: Any) -> dict:
    """Best-effort JSON decode of a CDP ``evaluate`` response."""
    if not result:
        return {"success": False, "error": "no_response"}
    if isinstance(result, dict):
        return result
    try:
        return json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return {"success": False, "error": "parse_error"}


def _log(logger: Callable[[str], None] | None, message: str) -> None:
    if callable(logger):
        try:
            logger(message)
        except Exception:
            pass


def _stopping(stop_event: Any) -> bool:
    return bool(stop_event and stop_event.is_set())


def resume_image_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve and validate the configured resume images without touching disk.

    Returns a report dict and never raises: callers decide whether a warning
    should block sending.
    """
    profile = config.get("profile") if isinstance(config.get("profile"), dict) else {}
    raw_paths = profile.get("resume_images", [])
    raw_paths = raw_paths if isinstance(raw_paths, list) else []

    entries: list[dict[str, Any]] = []
    for raw in raw_paths:
        text = str(raw).strip()
        entry: dict[str, Any] = {"path": text, "exists": False, "size": None, "reason": ""}
        if not text:
            entry["reason"] = "路径为空"
            entries.append(entry)
            continue
        try:
            candidate = Path(text).expanduser()
            candidate = candidate.resolve() if candidate.is_absolute() else (Path.cwd() / candidate).resolve()
            if not candidate.exists():
                entry["reason"] = "文件不存在"
            elif candidate.suffix.lower() not in IMAGE_EXTENSIONS:
                entry["reason"] = f"不支持的格式（仅支持 {', '.join(sorted(IMAGE_EXTENSIONS))}）"
            elif candidate.stat().st_size <= 0:
                entry["reason"] = "文件为空"
            else:
                entry["exists"] = True
                entry["size"] = candidate.stat().st_size
                entry["path"] = str(candidate)
        except OSError as exc:
            entry["reason"] = f"无法读取文件: {exc}"
        entries.append(entry)

    return {
        "enabled": bool(profile.get("send_resume_images", False)),
        "entries": entries,
        "paths": [entry["path"] for entry in entries if entry["exists"]],
        "ready": any(entry["exists"] for entry in entries),
    }


def rank_file_input(input_meta: dict[str, Any]) -> int:
    """Score a discovered file input; a higher score means a likelier image picker."""
    accept = str(input_meta.get("accept") or "").lower()
    score = 0
    if "image" in accept:
        score += 100
    elif not accept or accept in {"*/*", "*"}:
        score += 5
    if input_meta.get("multiple"):
        score += 3
    # A visible input is normally the styled upload label, not the real picker.
    if input_meta.get("rendered"):
        score -= 10
    ancestors = str(input_meta.get("ancestors") or "").lower()
    if any(word in ancestors for word in ("chat", "dialog", "send", "input")):
        score += 2
    return score


def pick_file_input(inputs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the file input most likely to be the chat image picker."""
    if not inputs:
        return None
    return sorted(inputs, key=rank_file_input, reverse=True)[0]


def probe_file_inputs(target_id: str) -> dict[str, Any]:
    """Report every file input found on ``target_id`` (used by ``--probe``)."""
    parsed = _parse_js_result(evaluate(target_id, JS_DISCOVER_FILE_INPUTS))
    inputs = parsed.get("inputs") if isinstance(parsed.get("inputs"), list) else []
    for meta in inputs:
        meta["score"] = rank_file_input(meta)
    parsed["inputs"] = inputs
    return parsed


def attach_resume_images(
    target_id: str,
    config: dict[str, Any],
    *,
    job: dict[str, Any] | None = None,
    stop_event: Any = None,
    logger: Callable[[str], None] | None = None,
    max_wait_seconds: float = 12.0,
    expected_conversation_key: str = "",
) -> dict[str, Any]:
    """Upload the configured resume images into an already-open conversation.

    Never raises. The returned report always carries ``success``; callers should
    log it rather than let it influence the greeting outcome.
    """
    settings = resume_image_settings(config)
    report: dict[str, Any] = {"success": False, "files": [], "reason": ""}

    if not settings["enabled"]:
        report.update(skipped=True, reason="未开启简历图片发送")
        return report
    if not settings["paths"]:
        details = "; ".join(
            f"{entry['path']}: {entry['reason']}" for entry in settings["entries"] if entry["reason"]
        )
        _log(logger, f"    ! 简历图片已开启但不可用 → {details or '未配置简历图片'}")
        report["reason"] = details or "no_resume_images"
        return report
    if _stopping(stop_event):
        report["reason"] = "task_stopped"
        return report
    try:
        fingerprints = resume_image_fingerprints(settings["paths"])
    except OSError:
        report["reason"] = "resume_image_unreadable"
        _log(logger, "    ! 简历图片无法读取，停止上传")
        return report

    # A conversation must have a stable DOM identity before we can make an
    # idempotency decision; arbitrary images in the chat are not evidence.
    before_snapshot = capture_conversation_images(target_id)
    if not before_snapshot["conversation_key"]:
        report["reason"] = "conversation_identity_unavailable"
        _log(logger, "    ! 无法识别当前会话的稳定标识，停止上传以避免重复发送")
        return report
    if expected_conversation_key and before_snapshot["conversation_key"] != expected_conversation_key:
        report["reason"] = "conversation_changed"
        return report
    before_urls = set(before_snapshot["urls"])
    before_message_images = len(before_urls)

    inputs = probe_file_inputs(target_id)["inputs"]
    if inputs:
        summary = "、".join(
            f"#{meta.get('index')}[{meta.get('accept') or '无 accept'}{'|multiple' if meta.get('multiple') else ''}]"
            for meta in inputs[:4]
        )
        _log(logger, f"    · 会话页发现 {len(inputs)} 个上传控件: {summary}")
    else:
        _log(logger, "    ! 会话页未发现上传控件，跳过简历图片发送")
        report["reason"] = "no_file_input"
        return report

    chosen = pick_file_input(inputs)
    index = chosen.get("index", 0)
    # Mark the winning input so CDP can target it without a brittle selector path.
    marker = (
        f"(function(){{const inputs=Array.from(document.querySelectorAll('input[type=\"file\"]'));"
        f"if(!inputs.length||{index}>=inputs.length)return false;"
        f"inputs.forEach(function(node,position){{node.removeAttribute('{_INPUT_TAG}');"
        f"if(position==={index})node.setAttribute('{_INPUT_TAG}','1');}});return true;}})()"
    )
    marked = evaluate(target_id, marker)
    if marked is False or marked is None:
        _log(logger, "    ! 上传控件标记失败，跳过简历图片发送")
        report["reason"] = "mark_file_input_failed"
        return report
    multiple = bool(chosen.get("multiple"))

    _log(
        logger,
        f"    · 选择上传控件 #{index}（{chosen.get('accept') or '无 accept'}"
        f"{'，支持多选' if multiple else ''}），上传前会话图片 {before_message_images} 张",
    )

    staged: list[str] = []
    for start in range(0, len(settings["paths"]), len(settings["paths"]) if multiple else 1):
        if multiple:
            batch = settings["paths"]
        else:
            batch = settings["paths"][start : start + 1]
        if _stopping(stop_event):
            report.update(reason="task_stopped", files=staged)
            return report
        if capture_conversation_images(target_id)["conversation_key"] != before_snapshot["conversation_key"]:
            report.update(reason="conversation_changed", files=staged)
            return report
        if not set_files(target_id, f"input[{_INPUT_TAG}]", batch):
            names = ", ".join(Path(path).name for path in batch)
            _log(logger, f"    ! 简历图片写入上传控件失败: {names}")
            report.update(reason="set_files_failed", files=staged)
            return report
        staged.extend(batch)
        _log(logger, "    · 已载入简历图片: " + ", ".join(Path(path).name for path in batch))

        # A single scoped confirmation attempt; ambiguous previews never authorize a click.
        confirm = _parse_js_result(evaluate(target_id, _upload_confirm_script(fingerprints[start:start + len(batch)])))
        if confirm.get("ok"):
            _log(logger, f"    · 已点击「{confirm.get('clicked')}」确认上传")
        if multiple:
            break

    if not staged:
        report["reason"] = "no_file_staged"
        return report

    success = False
    after_snapshot = before_snapshot
    deadline = time.monotonic() + max_wait_seconds
    while time.monotonic() < deadline:
        if _stopping(stop_event):
            break
        after_snapshot = capture_conversation_images(target_id)
        new_urls = set(after_snapshot["urls"]) - before_urls
        if (after_snapshot["conversation_key"] == before_snapshot["conversation_key"] and
                len(staged) == len(settings["paths"]) and
                sorted(digest for url, digest in zip(after_snapshot["urls"], after_snapshot["fingerprints"])
                       if url in new_urls) == sorted(fingerprints)):
            success = True
            break
        time.sleep(0.8)

    new_urls = sorted(set(after_snapshot["urls"]) - before_urls)
    after_message_images = len(after_snapshot["urls"])
    report.update(
        success=success,
        files=staged,
        images_before=before_message_images,
        images_after=after_message_images,
        conversation_key=after_snapshot["conversation_key"],
        image_urls=new_urls,
        file_fingerprints=fingerprints if success else [],
    )
    if success:
        _log(logger, f"    ✓ 简历图片已发送（会话图片 {before_message_images} → {after_message_images}）")
    else:
        report["reason"] = "image_not_confirmed_in_conversation"
        _log(
            logger,
            "    ! 简历图片未在会话中确认，可能未发送成功（招呼语已送达状态不受影响）",
        )
    return report

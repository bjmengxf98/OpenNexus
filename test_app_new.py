from pathlib import Path

from api.app_new_routes import _display_user_text


HTML = (Path(__file__).parent / "static" / "app_new.html").read_text(encoding="utf-8")
HELP = (Path(__file__).parent / "docs" / "用户帮助.md").read_text(encoding="utf-8")


def test_new_app_keeps_main_page_controls():
    required_ids = {
        "menuBtn",
        "newChat",
        "convList",
        "wpsBadge",
        "helpBtn",
        "mobileUpdateBtn",
        "toolMenuBtn",
        "uploadBtn",
        "attachMode",
        "clearChat",
        "clearFiles",
        "messageInput",
        "sendBtn",
    }
    for element_id in required_ids:
        assert f'id="{element_id}"' in HTML


def test_new_app_keeps_time_groups_and_upload_type_labels():
    for label in ("今天", "昨天", "7天内", "更早"):
        assert label in HTML
    assert "图片已就绪" in HTML
    assert "文件已就绪" in HTML
    assert "is_image" in HTML
    assert "grid-template-rows:58px minmax(0,1fr)" in HTML
    assert "display:block;order:2" in HTML
    assert ".top-link,.who,.role{display:none!important}" in HTML


def test_mobile_sidebar_overlay_cannot_cover_sidebar_controls():
    app_start = HTML.index('<div class="app">')
    app_end = HTML.index('<div class="toast-wrap"')
    overlay = HTML.index('id="sidebarOverlay"')
    assert app_start < overlay < app_end
    assert ".sidebar{position:fixed;z-index:40" in HTML
    assert ".panel-overlay{position:fixed;inset:54px 0 0;z-index:39" in HTML


def test_mobile_composer_has_rounded_corners_without_idle_scrollbar():
    assert ".composer{padding:7px 8px;border-radius:18px}" in HTML
    assert "overflow-y:hidden" in HTML
    assert "input.style.overflowY=input.scrollHeight>150?'auto':'hidden'" in HTML
    assert "resizeInput();bootstrap();" in HTML


def test_history_restore_jumps_to_bottom_without_replaying_smooth_scroll():
    assert "scroll-behavior:smooth" not in HTML
    assert "function scrollBottom(smooth=false)" in HTML
    assert "addMessage(m.role,m.content,m.html,m.created_at,false)" in HTML
    assert "scrollBottom(false)" in HTML
    assert "if(autoScroll)scrollBottom(true)" in HTML


def test_history_hides_parsed_payload_but_keeps_attachment_name():
    saved = "请处理附件\n【文件：汇报材料.docx】\n这里是解析后的长文本"
    assert _display_user_text(saved) == "请处理附件\n📎 汇报材料.docx"


def test_chat_turn_can_resume_and_be_cancelled_without_replacing_chat_entrypoint():
    assert "fetch('/api/app-new/chat'" in HTML
    assert "X-Agent-Turn-ID" in HTML
    assert "/events?after=" in HTML
    assert "/cancel" in HTML
    assert "function resumeTurn(" in HTML
    assert "function cancelActiveTurn(" in HTML
    assert "sendBtn.onclick=()=>state.sending?cancelActiveTurn():send()" in HTML
    assert ".send.stop{background:#ef4444}" in HTML


def test_user_help_covers_current_major_features():
    required_topics = (
        "部门智能驾驶舱",
        "智能提醒",
        "个人微信",
        "知识库与长期记忆",
        "WorkBuddy / MCP 接入",
        "传统 WPS 表格",
        "作为 WPS 记录附件",
        "跟随系统、浅色、深色",
    )
    for topic in required_topics:
        assert topic in HELP

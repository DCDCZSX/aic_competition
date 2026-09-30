"""前端结构与合规回归测试（三页静态产物级校验）。

为什么需要这一层：界面改动（外壳、公共 CSS、模板内联脚本）不会被业务单测覆盖，
而最容易翻车的几类问题恰好只在这里暴露：
  ① 模板里出现 Jinja 敏感标记（CSS 的 `{#id{...}` 会被 Jinja 当成注释起始 → 整页 500）；
  ② 公共样式表路径写错（样式全丢但接口正常，单测看不出来）；
  ③ 页面引入第三方外链资源（比赛提交件的硬约束）；
  ④ 界面文案回潮成"演示/demo"口吻（§3 第 7 条对外呈现口径）。

运行（在 CHAIS/backend 目录下）：
    python -m unittest tests.test_frontend_structure -v
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as flask_app  # noqa: E402


class FrontendStructureTest(unittest.TestCase):
    """三页的静态结构、资源与文案口径校验。"""

    @classmethod
    def setUpClass(cls):
        flask_app.app.config["TESTING"] = True
        cls.client = flask_app.app.test_client()

    # ------------------------------------------------------------------ 工具
    def get(self, path):
        resp = self.client.get(path)
        text = resp.get_data(as_text=True)
        status = resp.status_code
        resp.close()
        return status, text

    def sections_of(self, html, container="main"):
        """取指定容器内的 section id 列表（用简单分词器，不引第三方库）。"""
        m = re.search(rf"<{container}[^>]*>(.*?)</{container}>", html, re.S)
        body = m.group(1) if m else ""
        return re.findall(r'<section id="([^"]+)"', body)

    # ------------------------------------------------------------------ 1. 可达性
    def test_pages_reachable(self):
        for path in ("/", "/patient", "/doctor"):
            with self.subTest(path=path):
                status, _ = self.get(path)
                self.assertEqual(status, 200, f"{path} 应为 200")

    def test_root_is_the_role_selection_home(self):
        """启动后应先进入首页，由首页选择患者端或医生工作台。"""
        status, html = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn('id="homeHero"', html)
        self.assertNotIn('<title>多学科就医智助系统 · 患者端</title>', html)
        self.assertNotIn('<title>多学科就医智助系统 · 医生工作台</title>', html)

    def test_home_does_not_duplicate_account_login(self):
        """主页只负责角色选择，账号和密码输入应放在对应角色页。"""
        _, html = self.get("/")
        self.assertNotIn('id="accountToggle"', html)
        self.assertNotIn('id="accountRow"', html)
        self.assertNotIn('id="login-user"', html)
        self.assertNotIn('id="login-pass"', html)
        self.assertNotIn('fillAccount(', html)

    def test_startup_banner_lists_home_as_primary_entry(self):
        """命令行启动提示的主入口必须是根目录首页，不应误导到患者端。"""
        from pathlib import Path
        source = Path(flask_app.__file__).read_text(encoding="utf-8")
        self.assertIn('[首页] http://{host}:{port}/', source)
        self.assertIn('[患者端] http://{host}:{port}/patient', source)
        self.assertIn('[医生端] http://{host}:{port}/doctor', source)

    def test_shared_stylesheet_served(self):
        """公共样式与 token 都要能取到；且 token 是**单一来源**（dsh.css 只 @import，不复制色板）。"""
        status, css = self.get("/static/css/dsh.css")
        self.assertEqual(status, 200)
        for token in (".shell{", ".rail{", ".appbar{"):
            self.assertIn(token, css, f"公共样式缺少 {token}")
        self.assertIn('@import url("/static/css/tokens.css");', css)
        self.assertNotIn("--gray-050:", css, "色板应只在 tokens.css 里定义（否则又变两份）")

        status, tokens = self.get("/static/css/tokens.css")
        self.assertEqual(status, 200)
        for token in ("--clay:", "--gray-050:", "--sp-16:", "--font-display:"):
            self.assertIn(token, tokens, f"tokens.css 缺少 {token}")

    def test_shared_tokens_use_readable_text_and_focus_colors(self):
        """正文、警告和焦点环的 token 不能回到低对比度的浅色值。"""
        _, tokens = self.get("/static/css/tokens.css")
        self.assertIn("--gray-500:#686762", tokens)
        self.assertIn("--clay-dark:#a94f35", tokens)
        self.assertIn("--warn:#8a5a00", tokens)
        self.assertIn("--focus-ring:#0b5cad", tokens)

    def test_home_uses_shared_tokens(self):
        """首页与工作台共用同一份 token（原先首页自带一份重复的 :root）。"""
        status, html = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn('href="/static/css/tokens.css"', html)
        home_root = re.search(r":root\{(.*?)\}", html, re.S)
        self.assertIsNotNone(home_root, "首页应有自己的覆盖块")
        defined = set(re.findall(r"(--[\w-]+)\s*:", home_root.group(1)))
        # 页面块只允许出现"覆盖项"或"首页独有别名"；共享 token 一律去 tokens.css
        _, tokens_css = self.get("/static/css/tokens.css")
        shared = set(re.findall(r"(--[\w-]+)\s*:", tokens_css))
        allowed = {
            # 首页覆盖：更大的编辑式尺度与导航高度
            "--display-2", "--headline-1", "--headline-3", "--headline-5", "--nav-h",
            "--shadow-sm", "--shadow-md", "--shadow-lg", "--whisper", "--surface",
            # 首页独有别名（落地页自己的命名）
            "--bg", "--border", "--border-strong", "--container", "--surface-2",
            "--text", "--text-2", "--text-3",
        }
        duplicated = defined & shared - allowed
        self.assertFalse(duplicated, f"这些 token 应在 tokens.css 里定义，不要在首页重复：{sorted(duplicated)}")

    def test_old_legacy_page_not_routed(self):
        """旧轮询版前端不再对外路由（旧产品名与旧 UI 不暴露）。"""
        self.assertEqual(self.get("/legacy")[0], 404)
        self.assertEqual(self.get("/templates/index.html")[0], 404)

    # ------------------------------------------------------------------ 2. Jinja 安全
    def test_no_jinja_markers_in_served_html(self):
        """服务端渲染后不应残留 Jinja 标记（残留说明模板里有裸 {{ / {% / {# ）。"""
        for path in ("/", "/patient", "/doctor"):
            with self.subTest(path=path):
                _, html = self.get(path)
                for marker in ("{{", "{%", "{#"):
                    self.assertNotIn(marker, html, f"{path} 残留 Jinja 标记 {marker}")

    def test_css_id_selectors_do_not_break_jinja(self):
        """回归：`{#id{...}` 会被 Jinja 当注释 → 整页 500。模板里禁止出现 `{#`。"""
        import pathlib
        tpl_dir = pathlib.Path(flask_app.app.template_folder)
        for name in ("home.html", "patient.html", "doctor.html"):
            text = (tpl_dir / name).read_text(encoding="utf-8")
            offenders = [i + 1 for i, line in enumerate(text.split("\n")) if "{#" in line]
            self.assertEqual(offenders, [], f"{name} 第 {offenders} 行含 `{{#`（改写成 `{{ #id`）")

    # ------------------------------------------------------------------ 3. 三轨外壳
    def test_patient_shell_has_four_stage_flow_without_persistent_context_rail(self):
        _, html = self.get("/patient")
        self.assertIn('href="/static/css/dsh.css"', html)
        self.assertIn('class="appbar"', html)
        self.assertIn('class="shell patient-shell"', html)
        self.assertEqual(html.count('class="rail rail-left"'), 1)
        self.assertEqual(html.count('class="rail rail-right"'), 0)
        self.assertNotIn('id="ctx-panel"', html)
        self.assertNotIn('id="ctx-inline-panel"', html)
        self.assertIn('id="steps"', html)              # 左轨流程导航
        self.assertIn('id="steps-bar"', html)
        self.assertIn('id="s-stage1"', html)
        self.assertIn('id="s-stage2"', html)
        self.assertIn('id="s-stage3"', html)
        self.assertIn('id="s-stage4"', html)

    def test_patient_sections_live_inside_main(self):
        """所有业务屏必须在 <main class="work"> 内：两条轨道都必须在 main 之外。

        原因：`show()` 会隐藏所有 `main section`；若某屏被放到轨道里，切屏时会被一起隐藏。
        """
        _, html = self.get("/patient")
        ids = self.sections_of(html, "main")
        expected = {"s-stage1", "s-stage2", "s-stage3", "s-stage4", "s-update", "s-changes", "s-done"}
        self.assertTrue(expected.issubset(set(ids)), f"main 内缺少屏：{expected - set(ids)}")
        self.assertEqual(
            {x for x in ids if x.startswith("s-stage")},
            {"s-stage1", "s-stage2", "s-stage3", "s-stage4"},
            "首次准备流程必须只有四个主阶段"
        )

        rail_left = html.index('class="rail rail-left"')
        main_open = html.index('<main class="work">')
        main_close = html.index("</main>")
        self.assertLess(rail_left, main_open, "左轨应排在 main 之前且不在 main 内")
        self.assertGreater(main_close, main_open)

    def test_patient_uses_shared_tokens_only(self):
        """患者端不应再自带蓝色主题变量（已统一为暖色 token）。"""
        _, html = self.get("/patient")
        self.assertNotIn("--blue", html)
        self.assertIn("--accent", html)

    def test_patient_stage1_is_one_concise_intake_form(self):
        """第 1 步只保留患者填写所需内容，目标和资料共用一个底部主按钮。"""
        _, html = self.get("/patient")
        start = html.index('id="s-stage1"')
        end = html.index('id="s-stage2"')
        stage1 = html[start:end]

        self.assertIn('id="s-intake-group"', stage1)
        self.assertIn('id="s-goal"', stage1)
        self.assertIn('id="s-intake"', stage1)
        self.assertEqual(stage1.count('onclick="startCaseFlow()"'), 1)
        self.assertIn('继续 → 核对资料', stage1)

        for removed in (
            "这个系统会做什么",
            "不会做什么",
            "一段资料大概长什么样",
            "为什么先脱敏",
            "提交后会看到",
            "直接开始录入资料",
            "系统会先帮你整理成画像卡",
        ):
            self.assertNotIn(removed, stage1)
        self.assertNotIn('class="upd-grid"', stage1)

    def test_patient_revalidates_persisted_session_before_showing_app(self):
        """服务重启后旧 token 会失效，不能只凭 localStorage 直接展示工作台。"""
        _, html = self.get("/patient")
        self.assertIn("async function restoreSession()", html)
        self.assertIn("api('/api/me'", html)
        self.assertIn("restoreSession().then", html)
        self.assertIn("localStorage.removeItem('dsh_session')", html)

    def test_patient_stage2_skips_visible_mask_preview_but_keeps_auto_privacy_gate(self):
        """第 2 步直接展示画像；脱敏仍在后台作为病例创建前的安全门槛。"""
        _, html = self.get("/patient")
        stage2_start = html.index('id="s-stage2"')
        stage3_start = html.index('id="s-stage3"')
        stage2 = html[stage2_start:stage3_start]
        self.assertNotIn('id="s-mask"', stage2)

    def test_patient_stage2_does_not_duplicate_stage3_analysis_progress(self):
        """回答完毕后会跳到第 3 步，进度只应在第 3 步顶部展示。"""
        _, html = self.get("/patient")
        stage2_start = html.index('id="s-stage2"')
        stage3_start = html.index('id="s-stage3"')
        stage2 = html[stage2_start:stage3_start]
        self.assertNotIn('id="analysis-note"', stage2)
        self.assertIn('id="s-profile"', stage2)
        self.assertIn('id="s-ask"', stage2)
        self.assertIn("startCaseFlow", html)
        self.assertIn("preview_token:S.previewToken", html)
        self.assertIn("blocking_hit_count", html)

    def test_doctor_detail_shell_contract(self):
        """医生详情页使用病例上下文头部、任务导航和单列主区。"""
        _, html = self.get("/doctor")
        self.assertIn('href="/static/css/dsh.css"', html)
        self.assertEqual(html.count('class="rail rail-left"'), 1)
        self.assertNotIn('class="rail rail-right"', html)
        for anchor in ('id="rail-filters"', 'id="rev-summary-inline"', 'id="rev-publish-inline"',
                       'id="rev-bar"', 'id="nav"', 'class="doctor-case-header"',
                       'class="doctor-case-nav', 'class="doctor-case-main"'):
            self.assertIn(anchor, html, f"医生工作台缺少 {anchor}")
        self.assertNotIn('id="rail-cases"', html, "病例队列不应在左轨重复渲染")
        # 业务屏必须在 <main> 内（与患者端同一约束）
        ids = set(self.sections_of(html, "main"))
        expected = {"s-list", "s-overview", "s-triage", "s-evidence", "s-mdt", "s-review", "s-history"}
        self.assertTrue(expected.issubset(ids), f"main 内缺少屏：{expected - ids}")
        # 详情页的五个主要任务必须有统一导航入口
        for task in ("overview", "triage", "evidence", "mdt", "review"):
            self.assertIn(f'data-s="{task}"', html)
        self.assertIn('class="review-help"', html)
        self.assertNotIn("--blue", html)

    def test_doctor_queue_has_server_pagination_controls(self):
        _, html = self.get("/doctor")
        self.assertIn('id="list-pager"', html)
        self.assertIn('id="f-sort"', html)
        self.assertIn('id="f-page-size"', html)
        self.assertIn("offset", html)
        self.assertIn("has_more", html)

    def test_doctor_queue_cells_have_mobile_field_labels(self):
        """窄屏隐藏表头后，每个队列字段仍要带可见字段名。"""
        _, html = self.get("/doctor")
        for label in ("病例", "主诉 / 牵头科室", "风险", "处理状态", "更新"):
            self.assertIn(f'data-label="{label}"', html)
        self.assertIn('data-queue-details', html)
        self.assertIn("查看详情", html)
        self.assertNotIn('data-label="可靠性"', html)
        self.assertNotIn('data-label="分析"', html)

    def test_doctor_review_template_exposes_auto_processed_group(self):
        _, html = self.get("/doctor")
        self.assertIn("auto_processed", html)
        self.assertIn("doctor_reviewed", html)
        self.assertIn("免审（自动）", html)

    def test_doctor_review_template_exposes_review_tiers_and_risk_counts(self):
        _, html = self.get("/doctor")
        for token in (
            "review_tier", "tier_reason", "tier_counts", "建议抽查",
            "必审", "自动处理", "高风险", "冲突", "分析失败",
        ):
            self.assertIn(token, html, f"医生审核分级缺少：{token}")
        self.assertIn("review_tier==='required'", html)
        self.assertIn("review_tier==='sample'", html)
        self.assertIn("review_tier==='automatic'", html)

    def test_revision_reopen_state_is_visible_on_both_workbenches(self):
        _, doctor = self.get("/doctor")
        _, patient = self.get("/patient")
        for token in ("content_revision", "stale_decisions", "reopened_item_ids",
                      "reopened_item_labels"):
            self.assertIn(token, doctor, f"医生端缺少修订追踪字段：{token}")
        for token in ("affected_item_ids", "reopened_item_ids", "affected_departments",
                      "reopened_item_labels",
                      "unaffected_departments"):
            self.assertIn(token, patient, f"患者端变化摘要缺少增量更新字段：{token}")

    # ------------------------------------------------------------------ 4. 合规
    def test_login_pages_are_full_bleed_two_column(self):
        """登录页必须铺满视口并分两栏（左进入方式 / 右产品说明）。

        回归背景：改公共样式时把 `#login` 的 flex 居中与 100vh 去掉，登录卡片掉到左上角，
        整屏几乎全空 —— 业务测试发现不了，只有结构断言能拦住。
        """
        for path, side_marker in (("/patient", "你会得到什么"), ("/doctor", "你在这个工作台里做什么")):
            with self.subTest(path=path):
                _, html = self.get(path)
                m = re.search(r'<div id="login"([^>]*)>', html)
                self.assertIsNotNone(m, f"{path} 缺少登录容器")
                self.assertIn("auth", m.group(1), f"{path} 登录容器缺 .auth（不再铺满分栏）")
                self.assertIn('class="auth-main"', html)
                self.assertIn('class="auth-side"', html)
                self.assertIn('class="auth-foot"', html)
                self.assertIn(side_marker, html, f"{path} 右侧说明栏缺内容")
                self.assertIn('id="login-pass"', html)
                # 旧的内联 padding 规则会把整块撑变形，必须已移除
                self.assertNotIn("#login{padding:24px;}", html)

    def test_login_pages_use_direct_role_password_login(self):
        """角色页直接显示本角色密码登录，不再要求重复点击角色入口卡。"""
        cases = (("/patient", "patient", "doctor"), ("/doctor", "doctor", "patient"))
        for path, own, other in cases:
            with self.subTest(path=path):
                _, html = self.get(path)
                login = html[html.index('<div id="login"'):]
                self.assertNotIn('class="role-card"', login, f"{path} 不应再出现重复角色入口卡")
                self.assertNotIn("showAccountLogin(", login, f"{path} 不应再要求二次点击后才显示密码框")
                self.assertIn('id="login-user"', login)
                self.assertIn('id="login-pass"', login)
                self.assertNotIn('class="login-account"', login)
                self.assertNotIn(f"const u='{own}'", login)
                self.assertNotIn(f"const username = '{own}'", login)
                # 跨端只保留弱链接，避免用户走错后无法切换
                self.assertIn(f'href="/{other}"', login, f"{path} 应有一条跨端弱链接")

    def test_login_controls_have_form_labels_and_live_errors(self):
        """登录必须能用 Enter 提交，输入框不能只依赖 placeholder。"""
        for path, form_id in (("/patient", "patient-login-form"), ("/doctor", "doctor-login-form")):
            with self.subTest(path=path):
                _, html = self.get(path)
                self.assertRegex(html, rf'<form id="{form_id}"[^>]*>')
                self.assertRegex(html, r'<label[^>]+for="login-user"')
                self.assertRegex(html, r'<label[^>]+for="login-pass"')
                self.assertIn('id="login-msg"', html)
                self.assertIn('role="alert"', html)
                self.assertIn('aria-live="assertive"', html)

    def test_patient_login_supports_first_party_registration(self):
        """患者可在患者端注册；角色由服务端决定，不能由前端伪造。"""
        _, html = self.get("/patient")
        self.assertIn('id="patient-register-form"', html)
        self.assertIn('id="register-user"', html)
        self.assertIn('id="register-pass-confirm"', html)
        self.assertIn('registerForm', html)
        self.assertIn("/api/register", html)
        self.assertNotIn('"role":"patient"', html)

    def test_patient_can_confirm_department_before_review_submission(self):
        """患者提交审核前必须看到主责科室并明确可见范围。"""
        _, html = self.get("/patient")
        for token in (
            'id="review-routing"', 'id="review-department"',
            'loadDepartments', 'renderReviewSubmission', 'submitCaseForReview',
            '/api/departments', '/submit-review', '仅该科室医生可见',
        ):
            self.assertIn(token, html, f"患者端缺少科室分流入口：{token}")
        stage3 = html[html.index('id="s-stage3"'):html.index('id="s-stage4"')]
        stage4 = html[html.index('id="s-stage4"'):]
        self.assertNotIn('id="review-routing"', stage3)
        self.assertIn('id="review-routing"', stage4)
        self.assertNotIn('reviewer_id', html)

    def test_doctor_ui_surfaces_department_scope(self):
        """医生工作台应显示当前科室范围，避免账号权限边界只存在于后台。"""
        _, html = self.get("/doctor")
        for token in ("department_name", "department_code", "当前科室", "仅显示已授权病例"):
            self.assertIn(token, html, f"医生端缺少科室范围提示：{token}")

    def test_interactive_choices_use_native_controls(self):
        """目标、疾病/问题选项和医生导航不能依赖不可聚焦的 div/span。"""
        _, patient = self.get("/patient")
        self.assertIn('<button type="button" class="goal"', patient)
        self.assertNotRegex(patient, r'<div[^>]+class="goal"[^>]+onclick=')
        self.assertIn('<button type="button" class="chip"', patient)
        self.assertNotRegex(patient, r'<span[^>]+class="chip"[^>]+onclick="setAns')
        self.assertIn('event.preventDefault();go', patient)
        self.assertNotIn('role="link" tabindex="0"', patient)

        _, doctor = self.get("/doctor")
        self.assertIn('<button type="button" class="nav-link', doctor)
        self.assertIn('aria-current="page"', doctor)
        self.assertIn("a.removeAttribute('aria-current')", doctor)
        self.assertNotRegex(doctor, r'<a[^>]+onclick="navCase')
        self.assertIn('class="filter-link', doctor)
        self.assertNotIn('href="#"', doctor, "队列筛选应使用原生 button，不应依赖无目的锚点")

    def test_doctor_review_modal_has_dialog_semantics_and_focus_restore(self):
        """审核弹窗需要被读屏器识别，并在关闭后把焦点还给触发按钮。"""
        _, html = self.get("/doctor")
        self.assertIn('role="dialog"', html)
        self.assertIn('aria-modal="true"', html)
        self.assertIn('aria-labelledby="modal-title"', html)
        self.assertIn('id="modal-close"', html)
        self.assertIn('modalPreviousFocus', html)
        self.assertIn('modalFocusables', html)
        self.assertIn('document.activeElement', html)
        self.assertIn('focus()', html)

    def test_json_key_order_is_preserved(self):
        """报告段落内部字段是"给人读的顺序"，JSON 序列化不得按字母重排。

        回归背景：Flask 默认 `sort_keys=True`，会把"判断 → 依据 → 证据状态 → 支持证据"
        打乱成字母序（中文按码点排），读起来莫名其妙。
        """
        with flask_app.app.app_context():
            payload = flask_app.app.json.dumps({"判断": 1, "依据": 2, "z_last": 3}, ensure_ascii=False)
        self.assertLess(payload.index("判断"), payload.index("依据"), "字段顺序被打乱")
        self.assertLess(payload.index("依据"), payload.index("z_last"), "字段顺序被打乱")

    def test_no_external_resources(self):
        for path in ("/", "/patient", "/doctor"):
            with self.subTest(path=path):
                _, html = self.get(path)
                self.assertEqual(re.findall(r'(?:src|href)="https?://[^"]+"', html), [])

    def test_no_demo_wording(self):
        """对外呈现口径：界面文案不得出现"演示/demo"口吻（§3 第 7 条）。"""
        for path in ("/", "/patient", "/doctor"):
            with self.subTest(path=path):
                _, html = self.get(path)
                self.assertNotIn("演示", html)
                self.assertNotIn("_demo", html)
                self.assertNotRegex(html, r"DEMO_")

    def test_medical_disclaimer_present(self):
        """三页都必须带"不提供诊断或处方"的免责表述。"""
        for path in ("/", "/patient", "/doctor"):
            with self.subTest(path=path):
                _, html = self.get(path)
                self.assertIn("不提供诊断", html)

    # ------------------------------------------------- 完整报告的逐段折叠
    def test_full_report_is_collapsible(self):
        """患者端就医详情按重点段落折叠；折叠只能影响显示，不能影响内容。

        这里守住三件事：① 用 `<details>/<summary>`（原生折叠、键盘可达）；
        ② 打印前会全部展开（否则打印出来的 PDF 只有标题没有正文）；
        ③ 打印样式里隐藏折叠控件（箭头/摘要/按钮不该印在纸上）。
        """
        _, html = self.get("/patient")
        self.assertIn('<details class="sec"', html)
        self.assertIn("<summary>", html)
        self.assertIn("sectionDigest", html, "折叠段应带内容摘要提示")
        self.assertIn("syncReportToggle", html)
        self.assertIn("openAllForPrint", html)
        self.assertIn('class="report-screen"', html)
        self.assertIn('class="print-report"', html)
        self.assertIn("buildPrintReportHtml", html)
        self.assertIn("patientFullReportEntries", html)
        self.assertIn("patientFullReportValue", html)
        self.assertIn("patientTimelineValue", html)
        self.assertIn("patientPathFlowValue", html)
        self.assertIn("patient-path-flow-parallel-group", html)
        self.assertIn("patient-path-flow-stage-marker", html)
        self.assertIn("PATIENT_FULL_REPORT_LABELS", html)
        self.assertIn("'时间线':'病程变化'", html)
        self.assertIn("病例画像", html)
        self.assertIn("就医路径", html)
        self.assertIn("医生最终意见", html)
        # 打印前自动展开、打印后恢复
        self.assertRegex(html, r"addEventListener\(\s*'beforeprint'[^)]*\)")
        self.assertRegex(html, r"addEventListener\(\s*'afterprint'[^)]*\)")
        # 旧的"每段都是 section + h2，全部铺开"实现不应回潮
        self.assertNotIn('<section id="sec-', html)

    def test_patient_shell_and_report_body_use_available_width(self):
        """患者端没有右轨时不能保留空列，报告正文也不能被固定阅读宽度截断。"""
        _, html = self.get("/patient")
        self.assertIn('class="shell patient-shell"', html)
        status, css = self.get("/static/css/dsh.css")
        self.assertEqual(status, 200)
        self.assertIn(".shell.patient-shell", css)
        self.assertIn(".patient-shell .doc-body", css)
        self.assertIn("max-width:none", css)

    def test_print_styles_expand_report_sections(self):
        """打印样式必须把折叠段落还原成正常文档（隐藏折叠控件）。"""
        status, css = self.get("/static/css/dsh.css")
        self.assertEqual(status, 200)
        print_block = css[css.index("@media print"):]
        self.assertIn("details.sec", print_block)
        self.assertIn(".sec-hint,.chev,.doc-tools", print_block.replace(" ", ""))
        self.assertIn("display:none !important", print_block.replace(" ", " "))
        self.assertIn(".report-screen{display:none!important;}", print_block.replace(" ", ""))
        self.assertIn(".print-report{display:block!important;}", print_block.replace(" ", ""))

    def test_patient_ui_has_no_internal_jargon(self):
        """患者端不出现内部代码与实现术语（前端评审 P0：降低认知负担）。"""
        _, html = self.get("/patient")
        for jargon in ("G4 门禁", "编排结果", "安全约束后", "阈值校验", "规则引擎"):
            with self.subTest(jargon=jargon):
                self.assertNotIn(jargon, html)
        self.assertNotIn("esc(t.decision_path||", html, "决策路径不应直接显示内部代码")
        self.assertNotIn("decision_path_label", html)
        self.assertNotIn("behavior_type_label", html)

    def test_patient_stage_labels_and_report_status_copy(self):
        _, html = self.get("/patient")
        for label in ("填写情况", "核对资料", "查看建议", "生成就医摘要"):
            self.assertIn(label, html)
        self.assertIn("AI 就诊准备摘要", html)
        self.assertIn("医生审核版报告", html)
        self.assertNotIn("共 9 步", html)

    def test_mdt_screening_is_explicit_and_patient_safe(self):
        _, doctor = self.get("/doctor")
        _, patient = self.get("/patient")
        for token in (
            "mdt-screening", "single_specialty", "recommend_mdt",
            "need_more_info", "urgent_care", "submitMdtScreening",
        ):
            self.assertIn(token, doctor, f"医生端缺少初筛入口或决定：{token}")
        self.assertIn("医生建议多学科共同评估", patient)
        self.assertIn("请尽快前往急诊或拨打 120", patient)
        self.assertNotIn("reviewer_id", patient)

    def test_doctor_mdt_preparation_packet_is_source_labeled_before_review(self):
        _, html = self.get("/doctor")
        for token in (
            "mdt_preparation_packet", "会前资料包", "专科 AI 参考意见",
            "来源类型", "信息缺口", "分歧与处理状态",
        ):
            self.assertIn(token, html, f"医生端会前资料包缺少：{token}")
        self.assertIn("renderMdtPreparationPacket", html)
        self.assertLess(
            html.index("renderMdtPreparationPacket"),
            html.index("function renderReview"),
            "会前资料包应在逐条审核渲染函数之前定义并可被总览调用",
        )

    def test_patient_stage3_uses_plain_direction_path_and_specialty_summary(self):
        """第三步只给患者方向、关联任务和通俗专科摘要，内部推理留在医生端。"""
        _, html = self.get("/patient")
        stage3_start = html.index('id="s-stage3"')
        stage4_start = html.index('id="s-stage4"')
        stage3 = html[stage3_start:stage4_start]

        for internal in ("规则命中（确定性，可审计）", "模型受限发言", "可靠性", "病例类型",
                         "建议向医生确认的问题", "还不确定的部分", "共识", "质询"):
            self.assertNotIn(internal, stage3)
        self.assertIn("AI 多学科参考意见", stage3)
        self.assertIn("路径中的任务", html)

    def test_patient_stage3_supports_collapsible_detail_sections(self):
        """第三阶段把路径和专科参考意见折叠，首屏优先保留方向与风险提示。"""
        _, html = self.get("/patient")
        stage3_start = html.index('id="s-stage3"')
        stage4_start = html.index('id="s-stage4"')
        stage3 = html[stage3_start:stage4_start]
        self.assertIn('data-section="path"', stage3)
        self.assertIn('data-section="mdt"', stage3)
        self.assertGreaterEqual(stage3.count("<summary>"), 2)

    def test_patient_stage3_uses_one_shared_safety_notice(self):
        """第三阶段共用一条审核状态提示，避免分诊和专科卡重复堆 warning。"""
        _, html = self.get("/patient")
        stage3_start = html.index('id="s-stage3"')
        stage4_start = html.index('id="s-stage4"')
        stage3 = html[stage3_start:stage4_start]
        self.assertIn('id="stage3-safety-note"', stage3)
        self.assertEqual(stage3.count("AI 参考内容尚未经医生审核"), 1)

    def test_doctor_review_exposes_primary_pending_task_banner(self):
        """审核主区需要把剩余必审数量放在首要位置，避免三栏信息淹没下一步。"""
        _, html = self.get("/doctor")
        self.assertIn('id="review-task-banner"', html)
        self.assertIn("还有 ${sum.pending||0} 项必审", html)
        self.assertIn('aria-live="polite"', html)

    def test_doctor_empty_context_hides_right_rail_and_reclaims_grid(self):
        """未选择病例时右栏不应占用宽屏布局，选中病例后再恢复。"""
        _, html = self.get("/doctor")
        self.assertIn('id="doctor-shell"', html)
        self.assertIn("$('doctor-shell').classList.add('has-case')", html)
        self.assertIn("$('doctor-shell').classList.remove('has-case')", html)
        self.assertIn('.shell:not(.has-case) .rail-right{display:none;}', html)

    def test_doctor_styles_use_shared_surface_tokens(self):
        """医生模板的状态和 hover 背景不能绕过共享 token。"""
        _, html = self.get("/doctor")
        _, tokens = self.get("/static/css/tokens.css")
        self.assertIn("--surface-warm:#fffdf9", tokens)
        self.assertIn("--surface-subtle:#f4f7f4", tokens)
        self.assertNotIn("background:#fffdf9", html)
        self.assertNotIn("background:#f4f7f4", html)
        self.assertIn("background:var(--surface-warm)", html)
        self.assertIn("background:var(--surface-subtle)", html)
        self.assertIn('class="queue-toolbar"', html)
        self.assertNotIn('style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px;"', html)

    def test_forms_expose_help_text_and_busy_guards(self):
        """表单错误和提交中状态要能被辅助技术感知，并阻止重复提交。"""
        _, patient = self.get("/patient")
        _, doctor = self.get("/doctor")
        for html in (patient, doctor):
            self.assertIn('id="login-help"', html)
            self.assertIn('aria-describedby="login-help"', html)
            self.assertIn("loginBusy", html)
        self.assertIn('aria-describedby="intake-help"', patient)
        self.assertIn('id="intake-help"', patient)
        self.assertIn('previewBusy', patient)
        self.assertIn('modalSubmitting', doctor)

    def test_dynamic_status_badges_have_textual_accessible_label(self):
        """状态颜色之外还要提供可读取的文字标签。"""
        _, doctor = self.get("/doctor")
        self.assertIn('role="status" aria-label="${esc(label)}"', doctor)

    def test_analysis_polling_pauses_when_hidden_and_backs_off(self):
        """后台分析请求需要在隐藏页暂停，并在失败时按上限退避。"""
        _, patient = self.get("/patient")
        _, doctor = self.get("/doctor")
        self.assertIn("const ANALYSIS_POLL_DELAYS = [1000, 2000, 4000, 8000]", patient)
        self.assertIn("analysisPollDelay", patient)
        self.assertIn("document.visibilityState", patient)
        self.assertIn("document.addEventListener('visibilitychange'", patient)
        self.assertIn("updateAnalysisStage3Dom", patient)
        self.assertIn("mdtPlayback", doctor)
        self.assertIn("document.addEventListener('visibilitychange'", doctor)

    # ------------------------------------------- 医生端关键操作的窄屏可见性
    def test_doctor_publish_entry_survives_narrow_screens(self):
        """<1440px 时右轨被 CSS 收起 → 主区必须有"发布审核版/录入线下意见"的镜像。

        真实缺陷（前端评审发现）：`.rail-right{display:none}` 在 1440px 以下隐藏右轨，
        而这些入口原来**只**写在右轨里，于是 1366/1280 屏（很常见的笔记本与投影分辨率）
        上医生审完却发不出去 —— 流程卡死，业务单测与接口测试都发现不了。
        """
        status, html = self.get("/doctor")
        self.assertEqual(status, 200)
        review = re.search(r'<section id="s-review".*?</section>', html, re.S)
        self.assertIsNotNone(review, "医生端应有报告审核屏")
        self.assertIn('id="rev-publish-inline"', review.group(0),
                      "发布入口必须有主区镜像，不能只在右轨里")
        self.assertIn("publishPanelHtml", html)
        self.assertNotIn("publishPanelHtml(sum, canPublish, 'rail')", html)
        self.assertIn("publishPanelHtml(sum, canPublish, 'inline')", html)
        self.assertNotIn('id="op-text"', html, "线下意见输入框 id 应带前缀，避免两处重复 id")

        _, css = self.get("/static/css/dsh.css")
        packed = css.replace(" ", "")
        self.assertIn(".rail-right{display:none;}", packed)
        self.assertIn(".publish-inline{display:block;}", html,
                      "医生端所有宽度都保留唯一主区发布入口")


if __name__ == "__main__":
    unittest.main(verbosity=2)

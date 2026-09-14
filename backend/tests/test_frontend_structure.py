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

    def test_shared_stylesheet_served(self):
        status, css = self.get("/static/css/dsh.css")
        self.assertEqual(status, 200)
        for token in ("--clay:", "--gray-050:", ".shell{", ".rail{", ".appbar{"):
            self.assertIn(token, css, f"公共样式缺少 {token}")

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
    def test_patient_three_track_shell(self):
        _, html = self.get("/patient")
        self.assertIn('href="/static/css/dsh.css"', html)
        self.assertIn('class="appbar"', html)
        self.assertIn('class="shell"', html)
        self.assertEqual(html.count('class="rail rail-left"'), 1)
        self.assertEqual(html.count('class="rail rail-right"'), 1)
        self.assertIn('id="ctx-panel"', html)
        self.assertIn('id="ctx-inline-panel"', html)   # <1440px 的整宽上下文
        self.assertIn('id="steps"', html)              # 左轨流程导航
        self.assertIn('id="steps-bar"', html)

    def test_patient_sections_live_inside_main(self):
        """所有业务屏必须在 <main class="work"> 内：两条轨道都必须在 main 之外。

        原因：`show()` 会隐藏所有 `main section`；若某屏被放到轨道里，切屏时会被一起隐藏。
        """
        _, html = self.get("/patient")
        ids = self.sections_of(html, "main")
        expected = {"s-goal", "s-intake", "s-mask", "s-profile", "s-ask", "s-triage",
                    "s-path", "s-mdt", "s-report", "s-update", "s-changes", "s-done"}
        self.assertTrue(expected.issubset(set(ids)), f"main 内缺少屏：{expected - set(ids)}")

        rail_left = html.index('class="rail rail-left"')
        rail_right = html.index('class="rail rail-right"')
        main_open = html.index('<main class="work">')
        main_close = html.index("</main>")
        self.assertLess(rail_left, main_open, "左轨应排在 main 之前且不在 main 内")
        self.assertLess(main_close, rail_right, "右轨应排在 main 结束之后且不在 main 内")

    def test_patient_uses_shared_tokens_only(self):
        """患者端不应再自带蓝色主题变量（已统一为暖色 token）。"""
        _, html = self.get("/patient")
        self.assertNotIn("--blue", html)
        self.assertIn("--accent", html)

    def test_doctor_three_track_shell(self):
        """医生工作台：左轨 = 待办病例 + 审阅视图；右轨 = 审核面板。"""
        _, html = self.get("/doctor")
        self.assertIn('href="/static/css/dsh.css"', html)
        self.assertEqual(html.count('class="rail rail-left"'), 1)
        self.assertEqual(html.count('class="rail rail-right"'), 1)
        for anchor in ('id="rail-cases"', 'id="rev-summary"', 'id="rev-publish"',
                       'id="rev-bar"', 'id="nav"'):
            self.assertIn(anchor, html, f"医生工作台缺少 {anchor}")
        # 业务屏必须在 <main> 内（与患者端同一约束）
        ids = set(self.sections_of(html, "main"))
        expected = {"s-list", "s-overview", "s-triage", "s-evidence", "s-mdt", "s-review", "s-history"}
        self.assertTrue(expected.issubset(ids), f"main 内缺少屏：{expected - ids}")
        # 右轨的审核面板必须在 main 之外（常驻可见，不随切屏隐藏）
        main_close = html.index("</main>")
        self.assertLess(main_close, html.index('class="rail rail-right"'))
        self.assertNotIn("--blue", html)

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
                self.assertIn('id="login-user"', html)
                self.assertIn('id="login-pass"', html)
                # 旧的内联 padding 规则会把整块撑变形，必须已移除
                self.assertNotIn("#login{padding:24px;}", html)

    def test_login_pages_offer_only_their_own_entry(self):
        """登录页一端一入口：/patient 只提供患者端入口，/doctor 只提供医生工作台入口。

        回归背景：早期"屏 0"是一个共用登录页（双角色卡）；拆成两页后整块复制，
        导致两端页面上都摆着对方的入口，用户不知道该进哪个。
        跨端只保留底部一条弱链接（"医务人员请前往医生工作台 →" / "就诊者请前往患者端 →"）。
        """
        cases = (("/patient", "patient", "doctor"), ("/doctor", "doctor", "patient"))
        for path, own, other in cases:
            with self.subTest(path=path):
                _, html = self.get(path)
                login = html[html.index('<div id="login"'):]
                cards = re.findall(r'<div class="role-card" onclick="loginAs\(\'(\w+)\'\)"', login)
                self.assertEqual(cards, [own], f"{path} 登录页应只有本端入口，实际 {cards}")
                self.assertNotIn(f"loginAs('{other}')", login, f"{path} 不应出现对端入口")
                # 跨端只允许弱链接，不出现对端的"进入"字样按钮
                self.assertIn(f'href="/{other}"', login, f"{path} 应有一条跨端弱链接")
                self.assertNotIn(f'<span class="t">进入{"医生工作台" if own == "patient" else "患者端"}</span>',
                                 login, f"{path} 不应出现对端入口卡")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

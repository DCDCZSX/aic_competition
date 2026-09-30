# -*- coding: utf-8 -*-
"""响应式布局与打印的静态回归（不需要浏览器）。

**为什么需要这一层**：布局问题属于"接口全绿、单测全过、页面却不能用"的重灾区。
真实缺陷（前端评审发现）：`dsh.css` 里 `.rail-right{display:none}`（<1440px 收起右轨），
而医生端"确认发布审核版 / 录入线下意见"原来**只写在右轨**里 —— 1366×768 笔记本或
投影窗口下医生审完却发不出去，整条流程卡死。业务单测、接口测试、结构测试**全都发现不了**。

**做法**：写一个极小的 CSS 求值器（只认 `@media` + `display` / `grid-template-columns`）：
1. 按文档顺序取页面真正加载的样式表（`<link>` 指向的 CSS + 页面内联 `<style>`）；
2. 在 8 个目标宽度上求值；
3. 结合模板里的元素嵌套（父链），判断"关键操作"在每一档宽度上是否**至少有一个可见入口**。

**不测什么**：不做像素级视觉判断（那需要真浏览器）。它守的是"能不能点到"，
视觉细节仍靠肉眼走查。
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as flask_app  # noqa: E402

# 目标宽度：超宽 / 常见投影 / 笔记本 / 平板 / 手机
WIDTHS = (1920, 1440, 1366, 1280, 1100, 992, 768, 390)

# JS 控制的显隐（不是响应式规则）：`<section class="card hide">` 由 go() 切换显示，
# 与宽度无关，求值时必须忽略，否则会把"当前未打开的那一屏"误判成被响应式隐藏。
JS_CONTROLLED = {".hide", ".sr"}

# 每个关键操作：主入口选择器 + 兜底镜像选择器（None 表示只有主入口）
CRITICAL_ACTIONS = {
    "/patient": [
        ("录入与提交（表单区）", "#history", None),
        ("结构画像", "#profile-body", None),
        ("报告摘要与打印", "#report-body", None),
        ("就诊后资料更新", "#upd-content", None),
    ],
    "/doctor": [
        ("发布审核版 + 录入线下意见", "#rev-publish-inline", None),
        ("逐条审核条目与操作按钮", "#rev-body", None),
        ("病例列表（切病例）", "#list-body", None),
        ("审阅视图导航", ".view-nav", None),
    ],
}

VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "source", "area", "base",
             "col", "embed", "param", "track", "wbr"}
TAG_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)([^>]*)>")


# --------------------------------------------------------------------------
# 极小 CSS 解析/求值
# --------------------------------------------------------------------------

def parse_css(text: str):
    """解析成 [(条件元组, 选择器, 声明体), ...]；只处理 `@media`，其余 at-rule 跳过。

    注意：必须**先剥掉注释**，否则 `/* 说明 */\n.rule{...}` 会把注释粘进选择器里。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    rules = []
    i, n = 0, len(text)
    conds, buf = [], ""
    while i < n:
        chunk = text[i:]
        if chunk.startswith("@media"):
            j = text.index("{", i)
            conds.append(text[i + len("@media"):j].strip())
            i, buf = j + 1, ""
            continue
        if chunk.startswith("@"):                      # @import/@keyframes/... 整段跳过
            j = text.find("{", i)
            if j < 0:
                break
            depth, k = 0, j
            while k < n:
                if text[k] == "{":
                    depth += 1
                elif text[k] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            i, buf = k + 1, ""
            continue
        ch = text[i]
        if ch == "{":
            selector = buf.strip()
            j = text.index("}", i)
            if selector:
                # 逗号分组选择器要拆开存（`.appbar,.rail,.actions{display:none}` 这类）
                for one in selector.split(","):
                    one = one.strip()
                    if one:
                        rules.append((tuple(conds), one, text[i + 1:j]))
            i, buf = j + 1, ""
            continue
        if ch == "}":
            if conds:
                conds.pop()
            i, buf = i + 1, ""
            continue
        buf += ch
        i += 1
    return rules


def condition_applies(condition: str, width: int, print_media: bool = False) -> bool:
    """判断媒体条件是否生效。`print_media=True` 表示在"打印"这一档下求值。"""
    cond = condition.lower()
    if cond.startswith("print"):
        return print_media
    if "prefers-reduced-motion" in cond or "hover" in cond:
        return False
    if print_media:
        return True                      # 屏幕规则在打印时同样按当前宽度生效
    matched = False
    for kind, value in re.findall(r"\((min|max)-width\s*:\s*(\d+)px\)", cond):
        matched = True
        if kind == "min" and width < int(value):
            return False
        if kind == "max" and width > int(value):
            return False
    return matched or cond in ("all", "screen")


def declarations_at(rules, selector: str, width: int, print_media: bool = False):
    """取该选择器在指定宽度（或打印档）下最终生效的声明（源码顺序，后者覆盖前者）。"""
    result = {}
    for conds, sel, body in rules:
        if sel != selector:
            continue
        if not all(condition_applies(c, width, print_media) for c in conds):
            continue
        for decl in body.split(";"):
            if ":" in decl:
                name, value = decl.split(":", 1)
                result[name.strip()] = value.replace("!important", "").strip()
    return result


def hidden(rules, selector: str, width: int) -> bool:
    """该选择器在指定宽度下是否被 `display:none` 隐藏（忽略 JS 控制的显隐类）。"""
    if selector in JS_CONTROLLED:
        return False
    return declarations_at(rules, selector, width).get("display") == "none"


def element_chain(html: str, target: str):
    """返回目标元素（id 或 class 选择器）从外到内的选择器候选链；找不到返回 None。"""
    stack = []

    def candidates(tag: str, attrs: str):
        out = ["." + tag] if False else []             # 不按标签名隐藏，避免误判
        cls = re.search(r'class="([^"]*)"', attrs)
        if cls:
            out += ["." + c for c in cls.group(1).split() if c]
        ident = re.search(r'id="([^"]*)"', attrs)
        if ident:
            out.append("#" + ident.group(1))
        return out or [tag]

    for m in TAG_RE.finditer(html):
        closing, tag, attrs = m.group(1), m.group(2).lower(), m.group(3)
        if closing:
            if stack:
                stack.pop()
            continue
        cands = candidates(tag, attrs)
        if target in cands:
            return [c for level in stack for c in level] + cands
        if tag in VOID_TAGS or attrs.rstrip().endswith("/"):
            continue
        stack.append(cands)
    return None


def visible(html: str, rules, selector: str, width: int) -> bool:
    """元素在该宽度下是否可见（自身或任一祖先被 display:none 即不可见）。"""
    chain = element_chain(html, selector)
    if chain is None:
        return False
    return not any(hidden(rules, c, width) for c in chain)


# --------------------------------------------------------------------------
class ResponsiveLayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        flask_app.app.config["TESTING"] = True
        cls.client = flask_app.app.test_client()

    def fetch_css(self, href, seen):
        """取样式表，并**递归展开 @import**（tokens.css 就是经 dsh.css 的 @import 进来的）。"""
        if href in seen:
            return ""
        seen.add(href)
        resp = self.client.get(href)
        css = resp.get_data(as_text=True)
        resp.close()
        imported = ""
        for sub in re.findall(r'@import\s+url\("([^"]+)"\)', css):
            imported += self.fetch_css(sub, seen) + "\n"
        return imported + css

    def stylesheets(self, html):
        """按文档顺序取该页真正生效的全部 CSS（link 指向的文件 + 内联 style，含 @import）。"""
        seen = set()
        parts = []
        for href in re.findall(r'<link[^>]+rel="stylesheet"[^>]+href="([^"]+)"', html):
            if href.startswith("/"):
                parts.append(self.fetch_css(href, seen))
        parts += re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)
        return "\n".join(parts)

    def page(self, path):
        """取页面 + 它真正加载的样式表（按文档顺序拼接），返回 (html, rules)。"""
        resp = self.client.get(path)
        html = resp.get_data(as_text=True)
        resp.close()
        return html, parse_css(self.stylesheets(html))

    # ---------------------------------------------------- 关键操作始终可达
    def test_critical_actions_reachable_at_every_width(self):
        """每一档宽度下，每个关键操作都要有**至少一个可见入口**（真实缺陷的回归）。"""
        for path, actions in CRITICAL_ACTIONS.items():
            html, rules = self.page(path)
            for label, primary, mirror in actions:
                for width in WIDTHS:
                    with self.subTest(page=path, action=label, width=width):
                        ok = visible(html, rules, primary, width)
                        if mirror:
                            ok = ok or visible(html, rules, mirror, width)
                        self.assertTrue(
                            ok,
                            f"{path} 在 {width}px 下「{label}」没有任何可见入口"
                            f"（主入口 {primary} 与镜像 {mirror} 都被隐藏）")

    def test_wide_screen_hides_the_mirrors(self):
        """≥1440px 时右轨常驻，主区镜像应隐藏（否则同一操作在屏上出现两次，医生会犹豫点哪个）。"""
        for path, actions in CRITICAL_ACTIONS.items():
            html, rules = self.page(path)
            for label, _primary, mirror in actions:
                if not mirror:
                    continue
                with self.subTest(page=path, action=label):
                    self.assertFalse(visible(html, rules, mirror, 1920),
                                     f"{path} 宽屏下不应同时显示镜像 {mirror}（{label}）")

    def test_narrow_screen_uses_the_mirror(self):
        """<1440px 右轨被收起时，关键操作必须由主区镜像承接。"""
        for path, actions in CRITICAL_ACTIONS.items():
            html, rules = self.page(path)
            for label, primary, mirror in actions:
                if not mirror:
                    continue
                for width in (1366, 1280, 1100, 992, 768, 390):
                    with self.subTest(page=path, action=label, width=width):
                        self.assertFalse(visible(html, rules, primary, width),
                                         f"{path} {width}px 下右轨本应收起")
                        self.assertTrue(visible(html, rules, mirror, width),
                                        f"{path} {width}px 下镜像 {mirror} 必须可见（{label}）")

    # ---------------------------------------------------- 布局折叠约定
    def test_shell_collapses_to_single_column_on_narrow_screens(self):
        """窄屏下外壳必须塌成单列，否则 240px 左轨会把主区挤成窄条。"""
        for path, narrow_limit in (("/patient", 1000), ("/doctor", 1100)):
            html, rules = self.page(path)
            with self.subTest(page=path):
                wide = declarations_at(rules, ".shell", 1920).get("grid-template-columns", "")
                self.assertIn("--rail-left", wide, f"{path} 宽屏应保留左轨列")
                for width in (narrow_limit - 1, 992, 768, 390):
                    narrow = declarations_at(rules, ".shell", width).get("grid-template-columns", "")
                    self.assertNotIn("--rail-left", narrow,
                                     f"{path} {width}px 下应塌成单列，实际 {narrow!r}")

    def test_multi_column_areas_are_single_column_on_narrow_screens(self):
        """报告/多学科/路径/摘要/审核列表这些区域，窄屏下不能挤成多列。"""
        for path, selectors in (("/patient", (".doc", ".mdt", ".path-grid", ".sum-grid")),
                                ("/doctor", ("#rev-body", "#list-body"))):
            html, rules = self.page(path)
            for selector in selectors:
                with self.subTest(page=path, selector=selector):
                    if path == "/patient" and selector == ".doc":
                        self.assertEqual(
                            declarations_at(rules, selector, 768).get("display"),
                            "block",
                            "患者就医详情应保持单栏文档流",
                        )
                        continue
                    value = declarations_at(rules, selector, 768).get("grid-template-columns", "")
                    self.assertIn("1fr", value, f"{path} {selector} 窄屏应为单列")
                    self.assertNotIn("repeat(", value, f"{path} {selector} 窄屏不应仍是多列")

    def test_doctor_case_detail_does_not_reserve_a_fixed_right_rail(self):
        """病例详情应使用单列主区，右侧审核面板不能继续挤压正文。"""
        _html, rules = self.page("/doctor")
        selectors = {selector for _conditions, selector, _body in rules}
        self.assertIn(".doctor-case-main", selectors)
        self.assertIn(".doctor-case-nav", selectors)
        self.assertEqual(
            declarations_at(rules, ".shell.has-case", 1920).get("grid-template-columns"),
            "minmax(0,1fr)",
        )

    def test_doctor_review_stays_single_column_on_wide_screens(self):
        """审核分组按上下顺序阅读，宽屏也不能重新变成左右两列。"""
        _html, rules = self.page("/doctor")
        self.assertEqual(
            declarations_at(rules, "#rev-body", 1920).get("grid-template-columns"),
            "1fr",
        )

    def test_narrow_screens_shrink_page_padding(self):
        """≤640px 收窄页面内边距，避免正文被挤成一条。"""
        _html, rules = self.page("/patient")
        self.assertEqual(declarations_at(rules, ":root", 390).get("--page-pad"), "16px")
        self.assertNotEqual(declarations_at(rules, ":root", 1280).get("--page-pad"), "16px")

    def test_touch_targets_keep_a_minimum_size_on_phone(self):
        """手机端主要按钮和动态选项要有足够的触控面积。"""
        _html, rules = self.page("/patient")
        for selector in (".btn-sm", ".mini", "button.chip"):
            with self.subTest(selector=selector):
                decls = declarations_at(rules, selector, 390)
                self.assertGreaterEqual(int(decls.get("min-height", "0").replace("px", "")), 44)
                self.assertGreaterEqual(int(decls.get("min-width", "0").replace("px", "")), 44)

    # ---------------------------------------------------- 打印
    def test_print_hides_shell_and_expands_report(self):
        """打印：外壳/操作条不印在纸上；折叠的报告段必须还原成完整文档。"""
        _html, rules = self.page("/patient")
        for selector in (".appbar", ".rail", ".actions", ".no-print"):
            with self.subTest(selector=selector):
                decls = declarations_at(rules, selector, 1280, print_media=True)
                self.assertEqual(decls.get("display"), "none",
                                 f"打印时 {selector} 应隐藏（decls={decls}）")
        self.assertEqual(declarations_at(rules, "details.sec", 1280, print_media=True).get("overflow"),
                         "visible")
        self.assertEqual(declarations_at(rules, ".sec-body", 1280, print_media=True).get("padding"), "0")
        for selector in (".sec-hint", ".chev", ".doc-tools"):
            with self.subTest(selector=selector):
                decls = declarations_at(rules, selector, 1280, print_media=True)
                self.assertEqual(decls.get("display"), "none",
                                 f"打印时不应出现折叠控件 {selector}（decls={decls}）")

    def test_patient_path_flow_is_responsive_and_screen_only(self):
        """患者路径流程图不产生横向滚动，阶段轨道和并行组自适应，打印仍使用文字准备单。"""
        _html, rules = self.page("/patient")
        self.assertEqual(
            declarations_at(rules, ".patient-path-flow-list", 1280).get("display"),
            "grid",
        )
        self.assertEqual(
            declarations_at(rules, ".patient-path-flow-list", 768).get("display"),
            "grid",
        )
        self.assertEqual(
            declarations_at(rules, ".patient-path-flow-stage", 1280).get("display"),
            "grid",
        )
        self.assertEqual(
            declarations_at(rules, ".patient-path-flow-stage", 768).get("display"),
            "grid",
        )
        self.assertEqual(
            declarations_at(rules, ".patient-path-flow-parallel-list", 768).get("grid-template-columns"),
            "1fr",
        )
        self.assertEqual(
            declarations_at(rules, ".patient-path-flow-list", 1280).get("overflow-x"),
            "visible",
        )
        self.assertEqual(
            declarations_at(rules, ".patient-path-flow", 1280, print_media=True).get("display"),
            "none",
        )
        self.assertEqual(
            declarations_at(rules, ".shell.patient-shell", 1440).get("grid-template-columns"),
            "var(--rail-left) minmax(0,1fr)",
        )
        self.assertEqual(
            declarations_at(rules, ".patient-shell .doc-body", 1440).get("max-width"),
            "none",
        )

    def test_print_uses_width_independent_rules(self):
        """`@media print` 不能被宽度条件吞掉；屏幕档下打印规则不生效。"""
        _html, rules = self.page("/doctor")
        print_conditions = [c for c in {cond for conds, _s, _b in rules for cond in conds}
                            if c.startswith("print")]
        self.assertTrue(print_conditions, "样式表里应有 @media print 区块")
        for width in WIDTHS:
            self.assertIsNone(declarations_at(rules, ".appbar", width).get("display"),
                              f"{width}px 屏幕下不应隐藏顶栏")

    # ---------------------------------------------------- 设计 token 完备性
    def test_every_used_token_is_defined(self):
        """页面用到的每个 `var(--x)` 都必须有定义。

        这是 6.D 抽 `tokens.css` 后最容易翻车的地方：首页原先自带一份完整 :root，
        抽成共享文件后若漏搬一个 token，**整块样式会静默失效**（不报错，只是变难看）。
        """
        for path in ("/", "/patient", "/doctor"):
            resp = self.client.get(path)
            html = resp.get_data(as_text=True)
            resp.close()
            css = self.stylesheets(html)
            defined = set(re.findall(r"(--[\w-]+)\s*:", css))
            used = set(re.findall(r"var\((--[\w-]+)", css))
            # 有些 token 是**运行时**由 JS 写进去的（如滚动动画的 --reveal-index / --indicator-x），
            # 它们本来就不该在 CSS 里定义；行内 style 里现写的也算已定义。
            runtime = set(re.findall(r"setProperty\(\s*['\"](--[\w-]+)['\"]", html))
            runtime |= set(re.findall(r"(--[\w-]+)\s*:",
                                      " ".join(re.findall(r'style="([^"]*)"', html))))
            with self.subTest(page=path):
                missing = used - defined - runtime
                self.assertFalse(missing, f"{path} 用到了未定义的 token：{sorted(missing)}")
                self.assertTrue(defined, f"{path} 应有可用的 token")


if __name__ == "__main__":
    unittest.main(verbosity=2)

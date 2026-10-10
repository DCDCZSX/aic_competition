---
name: 医序 · 账号入口
description: 患者登录与注册、医生登录的局部设计规则。
colors:
  gray-950: "#141413"
  gray-750: "#30302e"
  gray-600: "#5e5d59"
  gray-500: "#686762"
  gray-300: "#d1cfc5"
  gray-250: "#dedcd1"
  gray-050: "#faf9f5"
  gray-000: "#fff"
  clay-dark: "#a94f35"
  focus-ring: "#0b5cad"
  danger: "#B3402E"
  oat: "#e3dacc"
typography:
  display:
    fontFamily: '"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",system-ui,-apple-system,sans-serif'
    fontSize: "clamp(40px,4.6vw,72px)"
    fontWeight: 800
    lineHeight: 1.22
    letterSpacing: "-.035em"
  headline:
    fontFamily: '"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",system-ui,-apple-system,sans-serif'
    fontSize: "clamp(28px,2.7vw,40px)"
    fontWeight: 800
    lineHeight: 1.25
    letterSpacing: "-.025em"
  body:
    fontFamily: '"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",system-ui,-apple-system,sans-serif'
    fontSize: "16px"
    lineHeight: 1.6
  label:
    fontFamily: '"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",system-ui,-apple-system,sans-serif'
    fontSize: "15px"
    fontWeight: 600
  label-action:
    fontFamily: '"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",system-ui,-apple-system,sans-serif'
    fontSize: "16px"
    fontWeight: 600
rounded:
  br-8: "8px"
spacing:
  sp-8: "8px"
  sp-12: "12px"
  sp-16: "16px"
  sp-24: "24px"
  sp-28: "28px"
components:
  button-primary:
    backgroundColor: "{colors.gray-950}"
    textColor: "{colors.gray-000}"
    typography: "{typography.label-action}"
    rounded: "{rounded.br-8}"
    padding: "14px 20px"
    width: "100%"
  button-primary-hover:
    backgroundColor: "{colors.gray-750}"
  input-account:
    backgroundColor: "{colors.gray-000}"
    textColor: "{colors.gray-950}"
    rounded: "{rounded.br-8}"
    padding: "14px 16px"
    height: "54px"
    width: "100%"
  link-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.clay-dark}"
    padding: "8px 0"
  nav-role:
    textColor: "{colors.gray-950}"
    padding: "8px 0"
  nav-home:
    textColor: "{colors.gray-950}"
    padding: "8px 0"
  safety-note:
    textColor: "{colors.gray-500}"
    padding: "22px 0 0"
  error-alert:
    backgroundColor: "rgba(179,64,46,.07)"
    textColor: "{colors.danger}"
    rounded: "{rounded.br-8}"
    padding: "12px 16px"
---

# Design System: 医序 · 账号入口

## Overview

**Creative North Star: "资料有序，判断有据"**

这是 /patient 与 /doctor 认证前界面及患者注册的局部记录。医序帮助患者整理就医资料，由医生核对资料并作最终判断；本文件只约束账号入口的视觉与交互呈现。

暖白故事区以不同人物表达患者整理资料、医生核对资料的使用场景；白色表单区通过粗体中文标题、明确标签和黑色提交按钮引导操作。两种角色共用表单尺度、状态反馈与导航语言。

**Key Characteristics:**

- 暖白故事区与白色表单区形成平面分区。
- 患者与医生分别使用对应人物插图。
- 每个当前表单只有一个全宽主提交按钮。
- 小屏优先显示品牌、表单、说明与角色入口。

## Colors

暖调中性色承载界面，墨黑强调操作，陶土色标记次级入口；具体值以 frontmatter 为准，对应共享样式中的同名令牌。

### Primary

- **墨黑**（gray-950）：标题、正文、主按钮、品牌及角色导航；**深灰**（gray-750）用于主按钮悬停。

### Secondary

- **陶土深棕**（clay-dark）：患者注册、返回登录及文字导航的悬停状态。

### Neutral

- **暖白**（gray-050）与**纯白**（gray-000）：分别承载故事区和表单区。
- **次级正文灰**（gray-600）与**提示灰**（gray-500）：说明、占位符和安全说明。
- **字段边界灰**（gray-300）与**分隔灰**（gray-250）：输入边界和说明区分隔线。
- **燕麦色**（oat）：文字选择背景。

焦点蓝（focus-ring）用于键盘与字段焦点；错误红（danger）配合明确错误文字。sidecar 的合成色阶仅用于展示，不扩充实现的颜色令牌。

**The Action Contrast Rule.** 当前表单的主操作使用墨黑底、白字；次级账号操作使用陶土色文字。

## Typography

**Display Font / Body Font:** 共用 frontmatter 中的中文无衬线栈；已核验的 Windows 浏览器实际使用 Microsoft YaHei。字体结果随平台和已安装字体变化。

**Character:** 标题依靠字重与尺度建立层级；表单文字保持常规阅读尺度。此处记录已批准账号入口的字体实现。

### Hierarchy

- **Display:** 故事区双行标题，使用 display 角色。
- **Headline:** 登录与注册表单标题，使用 headline 角色；中屏（≤960px）调整为（32px），手机（≤520px）为（28px）。
- **Body:** 常规正文与输入文字；表单导语在桌面为（17px），手机为（15px），行高（1.65）。
- **Label:** 持久字段标签使用 label；主按钮使用 label-action。
- **Supporting:** 文字入口与安全说明为（14–15px）；手机安全说明为（13px），行高（1.8）。

## Layout

宽屏采用故事区（55%）与表单区（45%）双列；暖白背景延伸到同侧顶栏。顶栏和故事文案共用水平留白（clamp(32px,6vw,92px)）。表单居中，最大宽度（448px），所在列宽度扣除（112px）。

表单采用单列：字段标签与输入间距（8px），字段与主操作间距（24px），表单与导语间距（36px）。注册的提交及返回入口也保持单列。输入和主按钮共享操作高度（54px；按钮为最小高度），文字入口的最小点击高度（44px）。

中屏（≤960px）隐藏故事区与暖白背景，表单使用白色单列；主区留白（48px 24px 64px）。手机（≤520px）主区留白为（28px 24px 40px），表单上间距（28px），组间距（22px）。窄屏（≤360px）水平留白降至（18px）。内容可随注册字段增长向下滚动。

## Elevation & Depth

界面以背景色、留白和细线区分层级。表单直接置于白色页面；输入和主按钮均无阴影。插图中的纸张、人物与植物保留素材本身的质感，不定义成 UI 阴影令牌。

## Shapes

输入、主按钮和错误提示共用轻圆角（br-8）。字段边界为细实线；标签与输入的外层组合无边框、无额外内边距。图标为同一线条语言的内联 SVG。

## Components

### Buttons

明确、全宽的主操作。登录与注册共用 button-primary；悬停转为深灰。请求中按钮禁用、透明度（.65）、等待光标，并显示“正在登录…”或“正在注册…”。焦点使用外偏移（2px）的双像素焦点轮廓。

### Inputs / Fields

标签始终可见，placeholder 只承担提示。字段使用白底、细灰边和统一高度；聚焦时边界与外轮廓使用焦点蓝。账号和密码保留对应的 autocomplete 设置。

### Navigation

注册、返回登录使用陶土色文字入口；主页和角色切换以墨黑文字配内联方向箭头。悬停使用陶土色与下划线（偏移 4px）。患者切换登录和注册时更新标题、导语，清空错误，并聚焦新表单的账号字段。

### Safety Note / Error Alert

安全说明居中，用上方细线与表单分隔；患者说明资料脱敏与外部模型，医生说明 AI 提示与核对责任。错误提示使用浅红背景、细红边与明确文字，通过 alert / assertive 播报；登录失败后聚焦账号字段。

## Do's and Don'ts

### Do:

- **Do** 让患者、医生和注册标题明确当前角色及动作。
- **Do** 保持每个可见表单只有一个全宽墨黑主按钮。
- **Do** 保留持久标签、明确错误文字、焦点轮廓和请求中的文字反馈。
- **Do** 在小屏保留品牌、返回主页、表单、安全说明及角色切换。

### Don't:

- **Don't** 给账号入口的表单外层添加卡片边框或阴影。
- **Don't** 用 placeholder 替代字段标签。
- **Don't** 把故事区插图的颜色与素材尺寸推广为通用组件令牌。
- **Don't** 将本文件的分栏、字号或组件规则自动推广到认证后的工作台。

<!-- Evidence: backend/templates/patient.html #login and auth-mode handlers; backend/templates/doctor.html #login and login handler; backend/static/css/dsh.css 4.13 plus inherited field, button, focus and banner rules; backend/static/css/tokens.css. Local boundary: docs/ui/auth. -->


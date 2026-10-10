---
name: 医序 · 工作台
description: 患者整理资料、医生核对并发布结果的操作界面。
colors:
  warm-white: "#faf9f5"
  white: "#fff"
  ink: "#141413"
  ink-hover: "#34332f"
  clay-dark: "#a94f35"
  line: "#e8e6dc"
  field-line: "#c9c5ba"
  focus-ring: "#0b5cad"
  oat: "#e3dacc"
typography:
  display:
    fontFamily: '"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",system-ui,-apple-system,sans-serif'
    fontSize: "28px"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: "-.02em"
  headline:
    fontFamily: '"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",system-ui,-apple-system,sans-serif'
    fontSize: "24px"
    fontWeight: 700
    lineHeight: 1.35
    letterSpacing: "-.02em"
  body:
    fontFamily: '"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",system-ui,-apple-system,sans-serif'
    fontSize: "16px"
    lineHeight: 1.6
  label:
    fontFamily: '"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",system-ui,-apple-system,sans-serif'
    fontSize: "14px"
    fontWeight: 600
rounded:
  sm: "8px"
  md: "12px"
spacing:
  sm: "8px"
  md: "16px"
  lg: "24px"
  xl: "32px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.white}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
    height: "44px"
  button-primary-hover:
    backgroundColor: "{colors.ink-hover}"
  input:
    backgroundColor: "{colors.white}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "12px 16px"
    height: "48px"
  divider:
    backgroundColor: "{colors.line}"
    height: "1px"
---

# Design System: 医序 · 工作台

## Overview

**Creative North Star: “资料有序，判断有据”**

工作台把患者的资料整理和医生的专业判断放进同一套暖白界面。视觉重点落在病例主诉、事实来源、风险状态和下一步操作；页面通过留白、分区标题和细线组织信息，让长内容可以连续阅读。

患者端沿四个阶段推进：填写情况、核对资料、查看建议、查看医生结果。医生端从风险优先队列进入病例，依次查看摘要、AI 资料包、分流确认、MDT 协作与会诊记录、报告审核。流程状态、风险文字、来源标识和报告版本始终保留。

**Key Characteristics:**

- 暖白页面、白色内容区、墨黑主操作和陶土色文字入口。
- 工作台标题与控件使用中文无衬线栈，标题依靠字重和尺度建立层级。
- 资料区使用分区与细线，减少重复的嵌套容器。
- 手机队列以主诉为第一阅读线，风险和处理状态并列显示。

## Colors

色彩保持克制：墨黑承担标题、品牌和提交动作；陶土深棕用于次级文字入口与少量强调；暖白和白色区分页面与内容区。风险、成功、警告和错误继续使用共享语义色，并与文字标签一起出现。

### Primary

- **墨黑**（#141413）：页面标题、正文重点、当前主操作和品牌。
- **暖白**（#faf9f5）：工作台页面底色。
- **纯白**（#fff）：顶栏、工作卡片和输入区域。

### Secondary

- **陶土深棕**（#a94f35）：历史记录、文字导航和小范围强调。
- **焦点蓝**（#0b5cad）：键盘焦点轮廓，保证操作位置清楚。

### Neutral

- **分隔灰**（#e8e6dc）：列表、分区和报告时间线的细线。
- **字段边界灰**（#c9c5ba）：输入和选择控件边界。
- **燕麦色**（#e3dacc）：文本选择背景。

## Typography

标题和正文共用 `"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",system-ui,-apple-system,sans-serif`。工作台标题桌面为 28px，手机为 24px；分区标题为 18px；正文保持 15–16px，说明和病例元信息为 13–14px。病例号使用等宽数字便于识别，长文本保持自然换行。

## Layout

患者工作台桌面使用左侧四阶段导航与主工作区；手机将四阶段压缩为完整的两列导航，新增病历与我的病历并排。录入方式使用一条分段选择栏，主诉输入和病情资料拥有连续的阅读宽度。

医生队列桌面保留病例、主诉/牵头科室、风险、处理状态、更新时间五列。手机将病例号、主诉、风险/状态、更新时间分成明确的阅读行；阶段导航在进入新阶段时把当前标签滚入可见区域。

病例详情桌面允许页面连续滚动，移动端使用横向阶段导航和纵向内容流。内容区以 24–32px 区域间距和 8–12px 字段间距建立节奏。验收宽度为 1440、1280、1024、960、768、390、320px。

## Elevation & Depth

工作台以平面分区为主：白色内容卡片配细边界，信息内部使用细线、标题和留白。列表和报告不依赖阴影表达层级；交互反馈使用边界、背景和焦点轮廓。长报告和正式记录保持稳定，不使用装饰性位移。

## Shapes

输入、选择控件、按钮和提示共用 8px 轻圆角；主要内容区使用 12px 圆角。移动端可点击按钮和输入目标至少 44px 高，复选框使用完整标签作为点击范围。细实线区分资料分区，状态胶囊只承载短状态和明确文字。

## Components

### Patient intake and records

阶段导航显示两位序号、阶段名和当前状态。新增病历是墨黑主按钮，我的病历为次级按钮。病例列表将本次主诉放在标题位置，病例号、更新时间、主责方向和报告版本组成元信息；打开病例是主操作，历史记录是陶土文字入口。

### Doctor queue

队列按风险优先显示，筛选、排序、分页和详情展开保持原有操作。统计条使用简洁的文字分组；病例条目保留风险、处理状态、更新时间、分析方法、关键分歧和临界提示。

### Evidence, routing and MDT

病例摘要区分事实、来源和信息缺口；AI 资料包区分已掌握事实、AI 参考判断、待医生判断和缺口。分流先选择处理方向，再填写主责科室、参与范围、会诊目标和确认理由。MDT 依次显示已确认范围、AI 参考、分歧及正式医生记录，并显示生成中、失败、过期、部分结果和重试入口。

### Review and patient result

报告审核把必审、已完成审核、发布前条件、最终建议和患者报告预览分成连续区段。预览后才出现确认发布；患者结果显示医生身份、发布时间、报告版本、最终建议、下一步安排和信息缺口。历史报告使用时间线和版本入口，打印版保持独立的阅读结构。

## Do's and Don'ts

### Do:

- **Do** 让每个阶段都有一个明确的当前主操作，并给出可恢复的错误和加载状态。
- **Do** 把来源贴近对应事实，把风险和状态同时用文字表达。
- **Do** 用细线、分区标题和空白组织长资料，保留键盘焦点和减少动态效果支持。
- **Do** 在 320px 到 1440px 间检查真实文本、按钮触摸范围、阶段可见性和内容单元宽度。

### Don't:

- **Don't** 在工作台内部继续堆叠同层级的重复边框。
- **Don't** 用缩小文字换取手机队列的表面无溢出。
- **Don't** 把 AI 参考内容写成正式医生记录，或把未发布报告显示成已发布结果。

<!-- Evidence: backend/static/css/workbench.css; backend/templates/patient.html; backend/templates/doctor.html; output/playwright/workbench-current-*.png. Boundary: authenticated patient and doctor workbenches; home, auth and print surfaces retain their own rules. -->

"""AdaMAS-unit report (2026-11-12): the plan and the results so far, as a PDF. Numbers from the session record."""
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

OUT = Path(sys.argv[1])
FONT = "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"
pdfmetrics.registerFont(TTFont("CJK", FONT, subfontIndex=0))

INK, MUTED, RULE, ACCENT, SOFT = "#1f2933", "#52606d", "#cbd2d9", "#2f6f9f", "#eef2f6"

# ------------------------------------------------------------------ chart
fp = font_manager.FontProperties(fname=FONT)
groups = [
    ("补丁前\ntier50 π0", [0.086, 0.281, 0.135], "#9aa5b1"),
    ("首次基线\n修补后（含 bug）", [0.086, 0.165, 0.294], "#c99a5b"),
    ("干净基线\n修补前", [0.182, 0.129, 0.102], "#9aa5b1"),
    ("干净基线\n修补后", [0.294, 0.281, 0.274], ACCENT),
    ("候选 v6\n（第 1 次迭代）", [0.234, 0.238], "#b5524a"),
]
fig, ax = plt.subplots(figsize=(7.2, 3.3), dpi=200)
for i, (label, vals, c) in enumerate(groups):
    ax.scatter([i] * len(vals), vals, s=46, color=c, zorder=3, edgecolor="white", linewidth=0.8)
    m = sum(vals) / len(vals)
    ax.plot([i - 0.22, i + 0.22], [m, m], color=c, linewidth=2, zorder=2)
ax.axhline(0.2927, color=ACCENT, linestyle=(0, (4, 3)), linewidth=1)
ax.text(4.45, 0.2927 + 0.006, "接受线 0.2927", fontproperties=fp, fontsize=7.5, color=ACCENT, ha="right")
ax.set_xticks(range(len(groups)))
ax.set_xticklabels([g[0] for g in groups], fontproperties=fp, fontsize=7.5)
ax.set_ylabel("held out 反馈半比率", fontproperties=fp, fontsize=8.5)
ax.set_ylim(0.05, 0.33)
ax.tick_params(axis="y", labelsize=7.5)
for s in ("top", "right"):
    ax.spines[s].set_visible(False)
ax.grid(axis="y", color="#e4e7eb", linewidth=0.6, zorder=0)
fig.tight_layout()
chart = OUT.parent / "ratios_chart.png"
fig.savefig(chart)

# ------------------------------------------------------------------ styles
base = dict(fontName="CJK", textColor=colors.HexColor(INK), wordWrap="CJK")
H0 = ParagraphStyle("t", fontSize=19, leading=26, spaceAfter=4, **base)
SUB = ParagraphStyle("s", fontSize=9.5, leading=14, textColor=colors.HexColor(MUTED), fontName="CJK", wordWrap="CJK")
H1 = ParagraphStyle("h1", fontSize=13.5, leading=19, spaceBefore=12, spaceAfter=5, textColor=colors.HexColor(ACCENT),
                    fontName="CJK", wordWrap="CJK")
H2 = ParagraphStyle("h2", fontSize=10.8, leading=15, spaceBefore=7, spaceAfter=3, **base)
P = ParagraphStyle("p", fontSize=9.4, leading=14.6, spaceAfter=4, **base)
LI = ParagraphStyle("li", parent=P, leftIndent=11, bulletIndent=2, spaceAfter=2)
CELL = ParagraphStyle("c", fontSize=7.9, leading=10.6, **base)
CELLH = ParagraphStyle("ch", fontSize=7.9, leading=10.6, fontName="CJK", textColor=colors.white, wordWrap="CJK")
CAP = ParagraphStyle("cap", fontSize=8, leading=11, textColor=colors.HexColor(MUTED), fontName="CJK", alignment=TA_CENTER,
                     wordWrap="CJK", spaceAfter=6)
BOX = ParagraphStyle("box", parent=P, fontSize=9.4, leading=14.4)


def p(t):
    return Paragraph(t, P)


def bullets(items):
    return [Paragraph(t, LI, bulletText="•") for t in items]


def table(rows, widths, head=True, hl=None):
    data = [[Paragraph(str(c), CELLH if (head and i == 0) else CELL) for c in r] for i, r in enumerate(rows)]
    t = Table(data, colWidths=[w * mm for w in widths], repeatRows=1 if head else 0)
    st = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor(RULE)), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
          ("TOPPADDING", (0, 0), (-1, -1), 2.6), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.6),
          ("LEFTPADDING", (0, 0), (-1, -1), 3.5), ("RIGHTPADDING", (0, 0), (-1, -1), 3.5)]
    if head:
        st.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(ACCENT)))
    for r in hl or []:
        st.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor(SOFT)))
    t.setStyle(TableStyle(st))
    return t


def callout(text):
    t = Table([[Paragraph(text, BOX)]], colWidths=[170 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(SOFT)),
                           ("LINEBEFORE", (0, 0), (0, -1), 2.2, colors.HexColor(ACCENT)),
                           ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                           ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return t


def footer(c, doc):
    c.saveState()
    c.setFont("CJK", 7.5)
    c.setFillColor(colors.HexColor(MUTED))
    c.drawString(20 * mm, 11 * mm, "AdaMAS-unit · 收尾导入检查与预算进化 · 2026-11-12")
    c.drawRightString(190 * mm, 11 * mm, f"第 {doc.page} 页")
    c.restoreState()


s = []
s += [Paragraph("AdaMAS-unit：收尾导入检查与预算进化", H0),
      Paragraph("方案与结果报告 · 截至 2026-11-12 · 题目 nl2_deepdiff · 预算档 tier50（B = 1,586,206 token）", SUB),
      Spacer(1, 8)]

# ------------------------------------------------------------------ summary
s.append(Paragraph("摘要", H1))
s.append(callout(
    "1. 之前 held out 反馈半比率在同一策略下波动很大（0.086 到 0.281），主因是整个测试文件收集失败："
    "最终代码缺少文档点名的名字，71 个收集失败的文件里 68 个属于这一类。<br/>"
    "2. 为此给所有对照组加了同一个收尾导入检查和一次受限修补。修补全部成功的干净基线三次比率为 "
    "0.294 / 0.281 / 0.274，标准差从 0.083 降到 <b>0.008</b>，噪声阈值 ε = 0.01，候选需两轮都超过 0.2927。<br/>"
    "3. 在新基线上重新开始预算进化。第 1 次迭代的候选表 v6 被拒（0.234 / 0.238），其中一轮修补被撤销，"
    "驱动程序按停止条件停下，等待决定。<br/>"
    "4. 全部运行没有 CRITICAL 越界，没有运行作废。"))

# ------------------------------------------------------------------ plan
s.append(Paragraph("1. 系统方案", H1))
s.append(p("AdaMAS-unit 把一个仓库的生成拆成固定顺序的五个单元，每个单元由一个智能体构建，通过冻结的验收门，失败时最多修复 3 次。"
           "一次完整的多单元运行称为一个轮回，所有单元共用一个预算账本（token、时钟、上下文压缩次数、修复次数）。"
           "轮回之间只有预算规划者在进化：进化者读账本里的数字，提出新的预算表（各单元份额、修复上限、审批参数）。"))
s.append(table([
    ["单元", "名称", "token 份额（π<sub>0</sub>）"],
    ["U1", "foundation_contracts_and_utilities", "0.20"],
    ["U2", "recursive_diff_engine", "0.20"],
    ["U3", "delta_difference_application", "0.20"],
    ["U4", "search_and_hash_features", "0.20"],
    ["U5", "serialization_cli_and_public_integration", "0.20"],
], [16, 110, 44]))
s.append(Spacer(1, 4))
s.append(Paragraph("判定规则（E1）", H2))
s += bullets([
    "第一优先：可提交（最终代码能导入、文档点名的名字能导入、考卷能收集）的轮回数不比现任差。",
    "第二优先：held out 反馈半比率。两轮都高于现任均值 + ε 接受；两轮都不高于均值拒绝；其余加第 3 轮，3 轮中至少 2 轮高于均值 + ε 才接受。",
    "ε = max(现任各轮极差的一半, 0.01)。冻结考卷通过数、过门单元数、浪费只记录，不参与判定。",
    "最多 5 次迭代，连续 2 次被拒就停。进化者只看到 U1 到 U5 的匿名编号和数字，看不到代码、测试或单元真名。",
])
s.append(Paragraph("held out 与沙盒", H2))
s += bullets([
    "held out 按函数、按测试文件分层、固定种子拆成两半：反馈半 303 个函数，评估半 302 个函数。评估半留给 E2，至今没有读取，也没有计算。",
    "held out 只由密封程序读取，只输出数字。参考实现在反馈半上的比率为 0.9703，评分链路已核对。",
    "所有智能体会话和工作区代码都在沙盒内运行。每次回报都包含沙盒越界警告一节。",
])

# ------------------------------------------------------------------ diagnosis
s.append(Paragraph("2. 问题诊断：反馈半比率为什么波动大", H1))
s.append(p("对补丁前的 10 个轮回逐个文件做收集与运行（只用反馈半，只输出数字）。比率 = 收集率 × 收集后通过率。"))
s.append(table([
    ["轮回", "收集率", "收集后通过率", "比率", "收集不到的文件 / 22"],
    ["cal-pi0-r3", "0.409", "0.323", "0.132", "6"],
    ["tier50-pi0-r3", "0.337", "0.255", "0.086", "9"],
    ["tier50-pi0-r4", "0.614", "0.457", "0.281", "6"],
    ["tier50-pi0-r5", "0.327", "0.414", "0.135", "10"],
    ["tier75-pi0-r3", "0.376", "0.289", "0.109", "7"],
    ["tier75-pi0-r4", "0.667", "0.233", "0.155", "5"],
    ["tier75-pi0-r5", "0.327", "0.263", "0.086", "10"],
    ["tier100-pi0-r3", "0.848", "0.311", "0.264", "2"],
    ["e1-i1-v1-r1", "0.327", "0.293", "0.096", "10"],
    ["e1-i1-v1-r2", "0.396", "0.275", "0.109", "6"],
], [42, 28, 32, 26, 42]))
s.append(Spacer(1, 5))
s += bullets([
    "对数方差分解（全部 10 次）：收集率约占 2/3，收集后通过率约占 1/4。比率与收集率的相关是 0.85，收集率与收集后通过率几乎不相关（0.10）。",
    "71 个收集不到的文件里，68 个是属性或名称错误：held out 文件导入的名字，最终代码里没有。",
    "和哪个单元没做完关系不明显：cal-pi0-r3 五个单元全过门、冻结考卷 52/52，收集率却只有 0.41。",
])
s.append(Paragraph("这 68 个缺失名字在文档里的情况", H2))
s.append(table([
    ["类别", "文件数"],
    ["1. 文档写了名字和所在模块", "65（其中 60 个正是从文档写的模块导入）"],
    ["2. 文档写了名字，没写模块", "0"],
    ["3. 文档里没有这个名字", "3"],
], [80, 90]))
s.append(Spacer(1, 4))
s.append(p("顺带发现：设计文档用多行括号形式的 <font face='CJK'>from deepdiff.x import (...)</font> 列出公开接口，"
           "AdaMAS 原来的提取规则读不到这种写法，按旧规则这 68 个会全部被算作"
           "“文档没写”。之前在 AdaMAS 上做的天花板分析，可能因此低估了"
           "“文档写了却没做对”的比例。这一点已记入 AdaMAS-unit 的 EXPERIMENT_LOG。"))

# ------------------------------------------------------------------ the patch
s.append(Paragraph("3. 方案：所有对照组统一的收尾导入检查", H1))
s.append(p("目的是去掉"
           "“名字是否碰巧对上”带来的噪声，对所有组同样处理，不给任何组额外优势。"))
s.append(table([
    ["环节", "做法"],
    ["导入清单", "只由设计文档推导，所有轮回开始前冻结（184 个名字：115 个写明模块，69 个没写；16 个模块；0 个命令行入口）。所有组共用一份，记录哈希。"],
    ["检查", "程序完成，不调模型；在沙盒内、代码副本上运行：清单名字能导入、包内模块能导入、__all__ 名字存在、命令行入口能打印帮助。"],
    ["单元结束时", "只检查和记录缺失了哪些名字，不修补。缺失数（只给数量）提供给进化者作诊断。"],
    ["最终提交前", "有问题时启动一次修补会话：15 万 token、600 秒、改动不超过 150 行。用独立账本，不计入轮回预算，不影响规划者。"],
    ["修补范围（程序检查）", "只能是导入、导出、__all__、缺失名字的最小定义，以及只含导入和对允许名字赋值的 try/except ImportError。不得改已有函数或类。违反任一条就整体撤销，恢复修补前的代码。"],
    ["没写模块的名字", "只记录，不检查、不修补（其中混有 conftest fixture、nox 会话、pytest 钩子，空跑时每次会报 36 到 63 个缺失）。"],
], [36, 134]))
s.append(Spacer(1, 4))
s.append(p("正对照最终 80/80 通过，覆盖：清单不含 held out 或密封路径；检查不改动工作目录；合法修补保留、越界修补撤销；"
           "修补不计入轮回账本；两组共用同一模块；不启用时行为与补丁前逐字节一致；进化者输入只有数量、没有名字。"))

# ------------------------------------------------------------------ results
s.append(PageBreak())
s.append(Paragraph("4. 基线结果", H1))
s.append(Image(str(chart), width=170 * mm, height=170 * mm * 3.3 / 7.2))
s.append(Paragraph("图 1　各组 held out 反馈半比率（点为单轮，横线为均值；虚线为新基线下的接受线）", CAP))
s.append(Paragraph("首次基线（tier50-ic，3 轮）", H2))
s.append(table([
    ["轮回", "修补", "问题数 前→后", "比率 前 / 后", "说明"],
    ["r1", "撤销", "22→22", "0.086 / 0.086", "try/except 导入当时不在规则内（且改写了已有名字）"],
    ["r2", "撤销", "13→13", "0.165 / 0.165", "范围检查的 bug：同名函数定义两次被误判为修改"],
    ["r3", "保留", "32→0", "0.125 / 0.294", "收集率 0.337→0.848"],
], [14, 16, 26, 30, 84]))
s.append(Spacer(1, 3))
s.append(p("bug 已修复并补正对照；按决定允许受限的 try/except 导入。这组结果被 bug 污染，不用于判定。"))
s.append(Paragraph("干净基线（tier50-ic2，3 轮）", H2))
s.append(table([
    ["轮回", "修补", "问题数 前→后", "修补用量 token / 秒 / 行", "比率 前→后", "收集率 前→后", "收集后通过率", "冻结 / 过门"],
    ["r1", "保留", "38→0", "108,318 / 78 / 71", "0.182→0.294", "0.49→0.87", "0.336", "45 / 3"],
    ["r2", "保留", "11→0", "70,393 / 50 / 24", "0.129→0.281", "0.37→0.85", "0.331", "46 / 3"],
    ["r3", "保留", "26→0", "88,148 / 59 / 60", "0.102→0.274", "0.33→0.85", "0.323", "49 / 4"],
], [12, 13, 20, 34, 24, 22, 22, 23]))
s.append(Spacer(1, 5))
s.append(table([
    ["范围", "均值", "极差", "标准差"],
    ["补丁前 tier50（参考）", "0.167", "0.195", "0.083"],
    ["干净基线，修补前", "0.138", "0.079", "0.033"],
    ["干净基线，修补后", "0.283", "0.020", "0.008"],
], [70, 33, 33, 34], hl=[3]))
s.append(Spacer(1, 4))
s.append(p("修补成功后收集率稳定在 0.85 到 0.87，剩下的差别在收集后通过率（0.32 到 0.34），反映的是功能实现本身。"
           "新基线：现任为 π<sub>0</sub>，ε = max(0.0099, 0.01) = 0.01，接受线 0.2927。"))

# ------------------------------------------------------------------ E1
s.append(Paragraph("5. 预算进化 E1 v3：第 1 次迭代", H1))
s.append(p("进化者的输入在开跑前新增了每个单元结束时缺失的名字数（只给数量）。候选表 v6："))
s.append(table([
    ["单元", "U1", "U2", "U3", "U4", "U5"],
    ["token 份额（π<sub>0</sub> 为 0.20）", "0.17", "0.18", "0.18", "0.21", "0.26"],
    ["修复上限（π<sub>0</sub> 为 3）", "1", "1", "2", "3", "3"],
], [50, 24, 24, 24, 24, 24]))
s.append(Spacer(1, 3))
s.append(p("审批参数：追加上限按后续单元的表内份额预留（later_table_shares）；每个单元追加总量不超过其初始分配（unit_topup_limit = 1）。"))
s.append(Paragraph("进化者的理由（摘要）", H2))
s += bullets([
    "U5 每轮都最后耗尽，缺失名字最多，修复常常空启动，因此把 U5 份额提到允许上限 0.26，保留 3 次修复。",
    "U4 两次没过门，份额提到 0.21、修复上限 3；U1、U2 一直过门，降份额、修复上限降到 1。",
    "审批参数改动是为了防止前面单元的追加吃掉 U5 的预算。",
])
s.append(Paragraph("结果", H2))
s.append(table([
    ["轮回", "可提交", "比率", "修补", "冻结考卷", "过门单元数"],
    ["r1", "1", "0.234", "保留（9→0，17,715 token，31 行）", "48", "0"],
    ["r2", "0", "0.238", "撤销（18→18，97,020 token，57 行）", "39", "1"],
], [14, 16, 16, 76, 22, 26]))
s.append(Spacer(1, 4))
s += bullets([
    "判定：拒绝（可提交为 0 的轮回比现任多）。两轮比率也都低于现任均值 0.283。用量 320 万 token，在预估 340 万以内。",
    "r2 的修补为补上缺失的 not_found，先定义了一个不在清单里的私有辅助类 _NotFound，按规则整体撤销。这是规则在起作用，不是 bug。",
    "参考重放（不参与判定）：允许该辅助类时 r2 问题数降到 0，比率 0.261，仍低于 0.283。所以拒绝不是修补失败造成的。",
    "推测：把前面单元的修复上限压到 1、限制追加，使前面单元做得更差（r1 五个单元都没过门），问题累积到后面。这一点尚待数据验证。",
])

# ------------------------------------------------------------------ breach, usage, state
s.append(Paragraph("6. 沙盒越界警告", H1))
s.append(table([
    ["批次", "运行数", "WARNING", "CRITICAL", "作废"],
    ["首次基线 tier50-ic", "3", "4（写 /tmp 被拦下）", "0", "0"],
    ["干净基线 tier50-ic2", "3", "1（写 /tmp 被拦下）", "0", "0"],
    ["E1 v3 第 1 次迭代", "2", "2（写 /tmp 被拦下）", "0", "0"],
], [52, 22, 52, 22, 22]))
s.append(Spacer(1, 3))
s.append(p("所有 WARNING 都是构建者或修复者往 /tmp 写临时文件，被沙盒拦下，没有越界成功的命令。"))

s.append(Paragraph("7. 用量", H1))
s.append(table([
    ["项目", "约 token"],
    ["首次基线（3 轮 + 修补）", "490 万"],
    ["干净基线（3 轮 + 修补）", "490 万"],
    ["E1 v3 第 1 次迭代", "320 万"],
    ["补丁之后合计", "约 1,300 万"],
], [100, 70]))
s.append(Spacer(1, 3))
s.append(p("进化剩余上限约 2,200 万；如有候选被接受，与 π<sub>0</sub> 的配对确认（各 3 轮）另需约 1,000 万。"))

s.append(Paragraph("8. 当前状态与待决事项", H1))
s.append(callout(
    "<b>数据状态（2026-11-12 发现）：</b>本机的 /root/projects/AdaMAS-unit 和 /root/projects/AdaMAS 目前是空目录，"
    "轮回数据（outputs/，未纳入 git）在本机上已不可见。代码在 GitHub 上保存到提交 f9fb1df；"
    "此后的本地提交 4855fb9（进化者输入加缺失名字数、驱动程序停止条件）尚未推送，在本机上也已不可见。"
    "本报告的数字来自运行时的记录。继续实验前需要先确认数据能否恢复，或在新环境中重建。"))
s.append(Spacer(1, 5))
s += bullets([
    "修补能否定义只供缺失名字使用的私有辅助名（如 _NotFound）。建议保持严格规则：v6 的结论不受影响，放宽后修补更容易夹带新逻辑。",
    "是否继续第 2 次迭代；修补被撤销时，是每次停下，还是按实际结果计入并在回报中标出。",
    "提交 4855fb9 的恢复：若本地副本无法找回，需要按本报告第 5 节所述的改动重新实现并再过正对照。",
])

doc = SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm,
                        bottomMargin=18 * mm, title="AdaMAS-unit 收尾导入检查与预算进化报告", author="AdaMAS-unit")
doc.build(s, onFirstPage=footer, onLaterPages=footer)
print("wrote", OUT)

# AdaMAS-unit

AdaMAS 多阶段方法的单元化重构：按里程碑串行执行单元，单元之间只传代码；每个单元经过出题者的验收门，至多修复 3 次；
整个仓库共用一本预算账本（token、等待时间、上下文压缩次数、修复次数），由独立的预算规划者审批追加申请。
智能体框架为 mini-swe-agent。

v0.2：一次完整的多阶段执行称为一个轮回；只有预算规划者在轮回之间进化（预算表、通用规则、轮回摘要），
考卷只出一次并冻结，经过验证的规则晋升为 harness 机制。

- 设计（v0.2，合成稿）：[docs/DESIGN.md](docs/DESIGN.md)
- 用户的设计扩展原文：[docs/AdaMAS-unit：可进化的预算规划者（设计扩展 v0.2）.md](<docs/AdaMAS-unit：可进化的预算规划者（设计扩展 v0.2）.md>)
- 如何使用现有框架（AdaMAS、mini-swe-agent）：[docs/USING_ADAMAS.md](docs/USING_ADAMAS.md)
- 沙盒越界警告格式：[docs/sandbox_breach_log.md](docs/sandbox_breach_log.md)

状态：设计稿，尚无代码。

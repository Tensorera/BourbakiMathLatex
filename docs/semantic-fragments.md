# 语义分片修缮

本轮处理目录表中的 23 册，每册包含英文、中文两个版本。正式内容位于
`ConvertedLatex(EN)/<书名>` 和 `ConvertedLatex(CH)/<书名>`；`work/` 是
快照、模型决策、编译和验收记录，不是正文入口。

## 阅读和命名

先读 `chapters/tableofcontents.json`（英文）或 `chapters/目录.json`（中文），
按 `entries` 数组的顺序读取所需文件。目录顺序是正文顺序，不能用文件名字典序
代替。每项提供结构 ID、父节点、层级、双语标题和路径，路径均相对每册根目录。
完整旧来源的字节/行范围位于每册根的 `fragment_provenance.json`，按需读取。

文件名采用 `结构编号-短英文名称.tex`，例如
`ch04-s01-ss01-polynomial-defs.tex`。`ch` 是章，`s` 是节，`ss` 是小节；
英文名称仅用小写 ASCII 字母、数字和单个连字符，总长不超过 20 字符。
同一结构的中英文使用相同文件名。前言、章/节引言、习题、附录、历史说明、
索引等使用明确的角色编号，保留原有阅读顺序。编号取自原 LaTeX 结构和计数器，
不重新编号标题、命题或交叉引用。

通常最小单元是小节。章、节标题及其引言也各有文件；缺乏更小结构标题的内容
不凭主题猜测拆分。跨边界的环境、列表、数学公式、宏参数不得切开。
目录中的 `warnings` 会列出因此保留在上级文件内的结构标题。
集合论的旧 `include` 章文件是编译包装层：目录通过 `assemblies` 标明，
它们保留原有分页与辅助文件检查点，阅读时直接使用 `entries`。

## 工作入口

不要运行 Tactus 默认的全部编号脚本。只选本轮所需入口：

```bash
tactus watch --root . --port 47831
tactus check --root . .tactus/scripts/210_semantic_fragments.hs
tactus run --root . --script .tactus/scripts/210_semantic_fragments.hs -- --order 4 --plan-only
tactus run --root . --script .tactus/scripts/210_semantic_fragments.hs
```

`210` 每册一个双语任务，使用 `codex / gpt-6.1-sol / xhigh`，并发上限 3。
模型只给现有结构节点命名；程序从冻结快照精确复制正文，禁止模型重写正文。
旧输入文件的 EOF 和父文件行末可能各产生一次 TeX 空白。合并时程序使用标注的
`\relax{}` 装配边界复现此效果；来源正文逐字节复制，生成的装配字节单独映射。
`prepare/apply/verify` 的独立程序位于
`.tactus/scripts/Support/semantic_fragments.py`。已保存的有效计划会复用；
失败或结果不明时先检查落盘结果，不盲目重试。

正式 `main.pdf` 在分片阶段保持原字节。额外的 PDF 验收入口
`.tactus/scripts/Support/fragment_pdf_gate.py` 在 `work/` 中独立编译旧快照和
新目录，用独立 Poppler 进程逐页比较 96 dpi 像素；文字及字形位置、页面尺寸、
书签和链接在独立 PDF 检查进程中比较。像素要求完全相同，不使用容差。
本机字体路径、PDF 工具环境和验收结果存放在 `work/semantic_fragments/`。
模型参数使用本机 provider 可执行文件，迁移机器时修改
`Support/SemanticProviders.hs` 的路径；Clef SDK 路径属于本机 Cabal 配置。

## 阶段顺序

1. 完成分片修缮和 PDF 一致性验收。
2. 提交并推送 GitHub，建立可单独回退的检查点。
3. 才开始段落候选识别和修复。

段落阶段使用 `opencode / zhipuai-coding-plan/glm-5.3 / max`，并发上限 5。
只从 LaTeX 空行提取候选，不使用 OCR。模型看到边界前后的两句文字，
只返回需要合并或不能确定的 ID；其余保留。程序核验来源哈希、边界范围和
模型结果，删除批准的空行，保留所有非空行字节。目录结构不变，另行保存
决策、备份、事务和修复审计。此阶段允许预期的段落重排，因此必须与
分片阶段的 PDF 严格一致验收和 Git 检查点分开。

旧转换/翻译脚本与旧 manifest 是历史记录，不能按旧 `fragment_NNN` 名称
重新运行它们来覆盖新结构。超链接和翻译修正不在本轮任务范围内。

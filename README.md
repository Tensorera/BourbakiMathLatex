# BourbakiMathLatex

本项目整理了尼古拉·布尔巴基（Nicolas Bourbaki）数学著作的 LaTeX 版本，便于数学内容的阅读、检索、编译与进一步校订。

## 目录说明

- `ConvertedLatex(EN)`：英文 LaTeX 版本。
- `ConvertedLatex(CH)`：中文 LaTeX 版本。

内容按著作及章节组织，涵盖集合论、代数、一般拓扑、实变函数、拓扑向量空间、积分、李群与李代数、交换代数、谱理论、微分与解析流形、代数拓扑及数学史等主题。各子目录通常包含：

- `main.tex`：主文档入口；
- `chapters/`：分章节的 LaTeX 源文件；
- `images/`：正文所需图片；
- `main.pdf`：已编译的预览文件；
- `build_manifest.json` 或 `translation_manifest.json`：转换或翻译记录。

## 编译

建议安装较完整的 TeX Live，并使用 XeLaTeX 编译。在具体著作目录中运行：

```bash
latexmk -xelatex main.tex
```

也可以多次运行 `xelatex main.tex`，以正确生成目录和交叉引用。中文版本依赖 `ctex` 及相应的中英文字体。

## 说明

本项目主要用于个人学习、研究与 LaTeX 整理。自动转换或翻译内容可能存在遗漏和错误，请结合原始资料核对。原著及相关材料的版权归相应权利人所有，使用时请遵守适用法律及原始材料的授权范围。

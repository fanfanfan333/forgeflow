# T26 保真语料库（docx_corpus）

本目录下所有 `.docx` 均由 `tests/corpus/build_corpus.py` **合成**（python-docx +
直接 OOXML 手术），**不含任何真实客户数据**（红线 16）。特征矩阵见
`../corpus/manifest.yaml`。请勿手工编辑；`python tests/corpus/build_corpus.py` 可复现。

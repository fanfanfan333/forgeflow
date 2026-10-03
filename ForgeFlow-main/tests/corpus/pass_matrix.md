# T26 保真语料库 · Pass Matrix（特征 × 操作）

- 特征数：15；样本数：41；每类样本数 ≥2。
- 操作：`noop` = 空操作往返（逐部件规范化相同）；`edit` = 定向编辑（仅目标段落变化）。

| 特征 | 特征名 | 样本 | noop | edit |
| --- | --- | --- | --- | --- |
| comments | 批注 | comments_01.docx | PASS | PASS |
| comments | 批注 | comments_02.docx | PASS | PASS |
| comments | 批注 | comments_03.docx | PASS | PASS |
| revisions | 已有修订 | revisions_01.docx | PASS | PASS |
| revisions | 已有修订 | revisions_02.docx | PASS | PASS |
| revisions | 已有修订 | revisions_03.docx | PASS | PASS |
| fields | 域（目录/页码/交叉引用） | fields_01.docx | PASS | PASS |
| fields | 域（目录/页码/交叉引用） | fields_02.docx | PASS | PASS |
| fields | 域（目录/页码/交叉引用） | fields_03.docx | PASS | PASS |
| footnotes | 脚注/尾注 | footnotes_01.docx | PASS | PASS |
| footnotes | 脚注/尾注 | footnotes_02.docx | PASS | PASS |
| footnotes | 脚注/尾注 | footnotes_03.docx | PASS | PASS |
| merged_cells | 合并单元格 | merged_cells_01.docx | PASS | PASS |
| merged_cells | 合并单元格 | merged_cells_02.docx | PASS | PASS |
| merged_cells | 合并单元格 | merged_cells_03.docx | PASS | PASS |
| nested_tables | 嵌套表格 | nested_tables_01.docx | PASS | PASS |
| nested_tables | 嵌套表格 | nested_tables_02.docx | PASS | PASS |
| textboxes | 文本框/形状 | textboxes_01.docx | PASS | PASS |
| textboxes | 文本框/形状 | textboxes_02.docx | PASS | PASS |
| textboxes | 文本框/形状 | textboxes_03.docx | PASS | PASS |
| floating_images | 浮动图片 | floating_images_01.docx | PASS | PASS |
| floating_images | 浮动图片 | floating_images_02.docx | PASS | PASS |
| headers_footers | 页眉页脚 + 分节 | headers_footers_01.docx | PASS | PASS |
| headers_footers | 页眉页脚 + 分节 | headers_footers_02.docx | PASS | PASS |
| headers_footers | 页眉页脚 + 分节 | headers_footers_03.docx | PASS | PASS |
| numbering | 多级编号列表 | numbering_01.docx | PASS | PASS |
| numbering | 多级编号列表 | numbering_02.docx | PASS | PASS |
| numbering | 多级编号列表 | numbering_03.docx | PASS | PASS |
| content_controls | 内容控件 | content_controls_01.docx | PASS | PASS |
| content_controls | 内容控件 | content_controls_02.docx | PASS | PASS |
| hyperlinks | 超链接/书签 | hyperlinks_01.docx | PASS | PASS |
| hyperlinks | 超链接/书签 | hyperlinks_02.docx | PASS | PASS |
| hyperlinks | 超链接/书签 | hyperlinks_03.docx | PASS | PASS |
| equations | 公式（OMML） | equations_01.docx | PASS | PASS |
| equations | 公式（OMML） | equations_02.docx | PASS | PASS |
| equations | 公式（OMML） | equations_03.docx | PASS | PASS |
| styles | 样式继承/主题字体 | styles_01.docx | PASS | PASS |
| styles | 样式继承/主题字体 | styles_02.docx | PASS | PASS |
| styles | 样式继承/主题字体 | styles_03.docx | PASS | PASS |
| large_document | 大文档（≥100 页） | large_document_01.docx | PASS | PASS |
| large_document | 大文档（≥100 页） | large_document_02.docx | PASS | PASS |

**结论**：全矩阵 noop 与 edit 均 PASS；无特征空覆盖。

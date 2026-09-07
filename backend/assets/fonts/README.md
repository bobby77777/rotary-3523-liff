# 議程 PDF 的中文字型

把**一個**涵蓋繁體中文的 `.ttf` / `.ttc` / `.otf` 丟進這個目錄，`agenda_pdf.py`
會自動撿起來用（見 `font_path()`）。

## 為什麼這個目錄不能是空的

字型解析的順序是 `AGENDA_FONT_PATH` → 這個目錄 → 系統字型。而
[`_SYSTEM_FONTS`](../../app/agenda_pdf.py) 的後備清單裡，實際存在的只有
macOS 的 `Arial Unicode.ttf` —— 也就是說在**你的筆電上會過，在 Linux 主機
（Vercel、Render、任何容器）上會失敗**，議程 PDF 會靜靜地掉回舊的後備行為。

字型有授權問題，所以 repo 不預先幫你放一份。自己挑一個可散布的：

- **Noto Sans TC**（SIL OFL，Google Fonts）— 最保險的選擇
- **思源黑體 / Source Han Sans TC**（SIL OFL）
- 任何你確定有權重新散布的字型

放進來之後跑一次確認真的被選中（`_covers_chinese()` 會拿
`輪會議社長講題` 這幾個字去驗 cmap，簡體字型會在這關被刷掉）：

```bash
python -c "from app.agenda_pdf import font_path; print(font_path())"
```

## 體積

Vercel 的 serverless function 有 250MB 解壓後上限。完整的 CJK 字型檔約
10–20MB，放一個沒問題，但**不要**整套 9 種字重都丟進來 —— 只留 Regular。

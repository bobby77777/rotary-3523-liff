// 前端唯一需要改的設定檔。
//
// 這裡的網址以前是硬寫在 index / bulletin / calendar / golf / finance 五支 HTML
// 裡的，換一次部署環境就要改五個地方、還很容易漏掉其中一支。集中到這裡之後，
// 換後端只動這一行。
//
// 這是純靜態站台、沒有 build step，所以不能用環境變數注入 —— 換環境就是直接改
// 這個檔再 push。Vercel 的 preview deployment 想指到別的後端時，改這裡再開一條
// 分支即可。
window.RC3523_API_BASE = 'https://rotary-liff-api.vercel.app';

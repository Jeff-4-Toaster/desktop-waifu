# DesktopWaifu｜灰姑娘桌面陪伴

Windows 桌面角色與本機 AI 陪伴：透明桌面角色、表情與視角控制、文字對話、角色背景檢索、語音辨識、朗讀、明確記憶與提醒。

本儲存庫提供執行所需的專案來源、角色執行模型與必要的建置／依賴準備工具。**這是來源碼，不能直接雙擊啟動**；不包含編譯好的程式、第三方執行環境或大型 AI 權重，也沒有在本儲存庫提供完整安裝包。

## 目錄

- `app/desktop/`：Win32 桌面視窗、WebView2 聊天視窗與語音輔助程式。
- `app/native/`：Inochi2D 渲染介面。
- `app/companion/`：本機 Python 對話服務、網頁介面與模型清單。
- `app/companion/persona.json`：角色語氣、指令與人工撰寫的通用示範；不是私人對話紀錄。
- `app/companion/character-background/cinderella.json`：程式實際讀取的角色背景與來源分類，含劇透。
- `assets/models/cinderella/crystal_maid/desktop-v01/`：桌面角色實際使用的 INP 模型。
- `tools/`：必要的建置工具、固定版本與第三方告知。

## 建置前提

目標為 Windows x64、OpenGL 3.3。CUDA 推論需要相容的 NVIDIA 顯示卡與驅動；實際容量需求依模型與上下文設定而異。

準備 PowerShell 7、Python 3.13、Git、7-Zip，以及包含 C++ x64 工具與 Windows SDK 的 Visual Studio Build Tools。渲染器需要 SDL2 2.28.2；現有腳本從 `C:/Program Files/Inochi Creator/SDL2.dll` 複製，使用不同安裝位置時須先調整 `tools/build-renderer.ps1`。

在專案根目錄執行：

```powershell
pwsh -File tools/bootstrap-environment.ps1
python tools/setup-companion.py
pwsh -File tools/build-desktop.ps1
pwsh -File tools/build-companion.ps1
```

前兩步會明確從上游下載依賴，AI 權重較大，請先確認磁碟空間與網路。工具只在專案內準備依賴，不包含雲端推論。選配 CPU 中文朗讀可使用 `tools/prepare-local-tts.py --download` 後執行 `tools/build-local-tts.ps1`；固定 WebView2 的準備方式見 `tools/prepare-fixed-webview.py --help`，沒有固定版時使用系統 WebView2。

建置產物位於 `build/desktop/`。自行組合執行資料夾時，桌面主程式旁需有 `cinderella_renderer.dll`、`SDL2.dll`、`menu-expressions.png`，將 INP 複製為 `model.inp`，並依 `app/desktop/companion.h` 的相對位置配置 `CompanionChat.exe`、`SpeechHelper.exe`、`companion/`、`python/`、`backend/llama/`、`backend/whisper/` 與 `models/`。`companion/` 來自 `app/companion/`，其餘原生依賴來自建置結果及 `.tools/`。依賴來源與雜湊列於 `tools/companion-lock.json`。這個精簡來源庫未提供一鍵封裝工具；組合時也必須保留各依賴的原始授權。

## 使用與資料

編譯與配置完成後啟動 `DesktopWaifu.exe`；拖曳角色移動，右鍵開啟選單，「聊聊」開啟對話。語音需先允許麥克風。角色背景是有限範圍的整理，AI 仍可能答錯或編造內容。

個人的聊天、記憶、提醒與設定在 `%LOCALAPPDATA%/DesktopWaifu`，不是角色背景資料。請勿將該資料夾、備份 ZIP、資料庫、WebView2 profile、錄音或截圖加入儲存庫或發行包。

此來源庫排除 `docs/`、專案歷史規畫、代理程式對話、驗證紀錄、美術草稿、開發快取及既有發行包。大型模型由固定來源另行取得。提交時請明確選擇檔案並檢閱 staged diff；忽略規則不能清除已提交的歷史。

## 權利與授權

這是非官方同人專案，角色及相關素材的權利仍屬原權利人；未將整個專案或角色素材授予開源授權。第三方軟體依各自授權提供，告知見 `app/desktop/licenses/` 與 `tools/licenses/`。散布編譯結果時須保留實際使用元件的完整原始授權。

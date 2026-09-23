/**
 * 藏鋒資本 Glacier — GitHub Actions 每日觸發器
 *
 * 用途：取代 GitHub 內建 schedule（實測延遲 2.7~13 小時），
 *      改由 Apps Script 在指定時間主動觸發 workflow_dispatch。
 *
 * 設定步驟見檔案最下方註解。
 */

const REPO = 'josh59408-ui/glacier';
const BRANCH = 'main';

// ─────────────────────────────────────────────
// 觸發函式（時間觸發器會呼叫這兩個）
// ─────────────────────────────────────────────

/** 台股：台北 週一~週五 收盤後觸發 */
function triggerTW() {
  const day = new Date().getDay(); // 0=日, 6=六
  if (day === 0 || day === 6) {
    Logger.log('週末，台股跳過');
    return;
  }
  dispatchWorkflow('daily.yml', '台股');
}

/** 美股：台北 週二~週六 早上觸發（對應美股前一交易日收盤） */
function triggerUS() {
  const day = new Date().getDay();
  if (day === 0 || day === 1) {
    // 台北週日=美股週六休市、台北週一=美股週日休市
    Logger.log('美股休市日，跳過');
    return;
  }
  dispatchWorkflow('us-daily.yml', '美股');
}

/** 台股每月股票清單更新：每月 1 日台北 09:00 */
function triggerMonthlyTW() {
  dispatchWorkflow('monthly.yml', '台股每月');
}

/** 美股每月股票清單更新：每月 1 日台北 09:30 */
function triggerMonthlyUS() {
  dispatchWorkflow('us-monthly.yml', '美股每月');
}

// ─────────────────────────────────────────────
// 核心：呼叫 GitHub API 觸發 workflow
// ─────────────────────────────────────────────

function dispatchWorkflow(workflowFile, label) {
  const token = PropertiesService.getScriptProperties().getProperty('GH_TOKEN');
  if (!token) {
    throw new Error('尚未設定 GH_TOKEN（專案設定 → 指令碼屬性）');
  }

  const url = `https://api.github.com/repos/${REPO}/actions/workflows/${workflowFile}/dispatches`;

  const res = UrlFetchApp.fetch(url, {
    method: 'post',
    headers: {
      'Authorization': 'Bearer ' + token,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    contentType: 'application/json',
    payload: JSON.stringify({ ref: BRANCH }),
    muteHttpExceptions: true,
  });

  const code = res.getResponseCode();
  const now = Utilities.formatDate(new Date(), 'Asia/Taipei', 'MM-dd HH:mm');

  if (code === 204) {
    Logger.log(`✅ ${now} ${label} 觸發成功（${workflowFile}）`);
  } else {
    const msg = `❌ ${label} 觸發失敗\nHTTP ${code}\n${res.getContentText()}`;
    Logger.log(msg);
    notifyFailure(label, code, res.getContentText());
  }
}

/** 觸發失敗時寄信通知自己 */
function notifyFailure(label, code, body) {
  try {
    MailApp.sendEmail({
      to: Session.getEffectiveUser().getEmail(),
      subject: `[Glacier] ${label} 每日任務觸發失敗 (HTTP ${code})`,
      body: `時間：${new Date()}\n\nHTTP ${code}\n\n${body}\n\n` +
            `請至 https://github.com/${REPO}/actions 檢查，或手動觸發。`,
    });
  } catch (e) {
    Logger.log('寄信通知失敗：' + e);
  }
}

// ─────────────────────────────────────────────
// 一鍵設定時間觸發器（執行一次即可）
// ─────────────────────────────────────────────

function setupTriggers() {
  // 清除本專案舊的觸發器，避免重複
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));

  // 台股：台北 14:30（13:30 收盤後 1 小時）
  ScriptApp.newTrigger('triggerTW')
    .timeBased().atHour(14).nearMinute(30).everyDays(1).create();

  // 美股：台北 06:00（美股 05:00 收盤後 1 小時，含夏令時間緩衝）
  ScriptApp.newTrigger('triggerUS')
    .timeBased().atHour(6).nearMinute(0).everyDays(1).create();

  // 每月 1 日：股票清單更新（取代 GitHub 內建 schedule，避免 60 天無 commit 被停用）
  ScriptApp.newTrigger('triggerMonthlyTW')
    .timeBased().onMonthDay(1).atHour(9).nearMinute(0).create();
  ScriptApp.newTrigger('triggerMonthlyUS')
    .timeBased().onMonthDay(1).atHour(9).nearMinute(30).create();

  Logger.log('✅ 觸發器已建立：台股 14:30、美股 06:00、每月 1 日 09:00/09:30（台北時間）');
  ScriptApp.getProjectTriggers().forEach(t =>
    Logger.log(`  - ${t.getHandlerFunction()}`)
  );
}

/** 測試用：立刻觸發台股一次（驗證 token 與權限） */
function testTW() {
  dispatchWorkflow('daily.yml', '台股(測試)');
}

/** 測試用：立刻觸發美股一次 */
function testUS() {
  dispatchWorkflow('us-daily.yml', '美股(測試)');
}

/* ═══════════════════════════════════════════════
   設定步驟
   ═══════════════════════════════════════════════

   1. 建 GitHub Token（fine-grained PAT）
      github.com/settings/personal-access-tokens/new
      - Repository access → Only select repositories → glacier
      - Permissions → Repository permissions → Actions: Read and write
      - 產生後複製 token（只會顯示一次）

   2. 建 Apps Script 專案
      script.google.com → 新專案 → 貼上這整份程式碼

   3. 存 Token（不要寫在程式碼裡！）
      左側「專案設定」→ 指令碼屬性 → 新增屬性
      - 屬性名稱：GH_TOKEN
      - 值：貼上步驟 1 的 token

   4. 設定時區
      專案設定 → 時區 → (GMT+08:00) Taipei

   5. 測試
      上方函式選 testTW → 執行（第一次會要求授權）
      → 檢查 github.com/josh59408-ui/glacier/actions 有沒有跑起來

   6. 建立每日觸發器
      函式選 setupTriggers → 執行一次
      → 完成！之後每天自動觸發

   ═══════════════════════════════════════════════ */

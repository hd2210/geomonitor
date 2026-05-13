const state = {
  user: null,
  admin: false,
  config: null,
  platforms: [],
  generated: [],
  currentMonitorId: null,
  pollTimer: null,
};

const $ = (id) => document.getElementById(id);
const isAdminPage = location.pathname === "/admin";

async function init() {
  bindStaticEvents();
  if (isAdminPage) {
    await initAdmin();
  } else {
    await initUser();
  }
}

function bindStaticEvents() {
  $("sendCodeButton").addEventListener("click", sendCode);
  $("loginButton").addEventListener("click", login);
  $("logoutButton").addEventListener("click", logout);
  $("adminLoginButton").addEventListener("click", adminLogin);
  $("adminLogoutButton").addEventListener("click", adminLogout);
  $("generateQuestionsButton").addEventListener("click", generateQuestions);
  $("startMonitorButton").addEventListener("click", startMonitor);
  $("refreshMonitorsButton").addEventListener("click", loadMonitors);
  $("saveAdminConfigButton").addEventListener("click", saveAdminConfig);
  $("addAdminModelButton").addEventListener("click", addAdminModel);
  $("refreshAdminUsersButton").addEventListener("click", loadAdminUsers);
  document.querySelectorAll("[data-user-view]").forEach((button) => {
    button.addEventListener("click", () => setUserView(button.dataset.userView));
  });
  document.querySelectorAll("input[name='adminRunMode']").forEach((input) => {
    input.addEventListener("change", () => {
      state.config.run_mode = input.value;
      renderAdminConfig();
    });
  });
}

async function initUser() {
  $("adminRoot").classList.add("hidden");
  const me = await fetchJson("/api/auth/me");
  state.user = me.user;
  if (!state.user) {
    $("loginRoot").classList.remove("hidden");
    $("userRoot").classList.add("hidden");
    return;
  }
  $("loginRoot").classList.add("hidden");
  $("userRoot").classList.remove("hidden");
  $("userName").textContent = `${state.user.company_name} · ${state.user.phone}`;
  setUserView("create");
  await loadPlatforms();
  await loadMonitors();
}

async function initAdmin() {
  $("loginRoot").classList.add("hidden");
  $("userRoot").classList.add("hidden");
  $("adminRoot").classList.remove("hidden");
  const me = await fetchJson("/api/admin/me");
  state.admin = me.authenticated;
  $("adminLoginPanel").classList.toggle("hidden", state.admin);
  $("adminPanel").classList.toggle("hidden", !state.admin);
  if (state.admin) {
    await loadAdminConfig();
    await loadAdminUsers();
  }
}

async function sendCode() {
  const phone = $("loginPhone").value.trim();
  const companyName = $("loginCompany").value.trim();
  $("loginMessage").textContent = "正在发送验证码...";
  try {
    const payload = await fetchJson("/api/auth/send-code", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({phone, company_name: companyName}),
    });
    $("loginMessage").textContent = payload.message || "验证码已发送。";
  } catch (error) {
    $("loginMessage").textContent = error.message;
  }
}

async function login() {
  try {
    await fetchJson("/api/auth/login", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        phone: $("loginPhone").value.trim(),
        company_name: $("loginCompany").value.trim(),
        code: $("loginCode").value.trim(),
      }),
    });
    await initUser();
  } catch (error) {
    $("loginMessage").textContent = error.message;
  }
}

async function logout() {
  await fetchJson("/api/auth/logout", {method: "POST"});
  location.reload();
}

async function adminLogin() {
  $("adminLoginMessage").textContent = "正在验证...";
  try {
    await fetchJson("/api/admin/login", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({password: $("adminPassword").value}),
    });
    await initAdmin();
  } catch (error) {
    $("adminLoginMessage").textContent = error.message;
  }
}

async function adminLogout() {
  await fetchJson("/api/admin/logout", {method: "POST"});
  location.reload();
}

async function loadPlatforms() {
  const payload = await fetchJson("/api/user/platforms");
  state.platforms = payload.platforms;
  $("enabledPlatformNames").textContent = state.platforms.map((item) => item.platform_name).join("、") || "暂无启用平台";
  renderPlatformChoices();
}

function renderPlatformChoices() {
  $("platformChoices").innerHTML = state.platforms.map((platform) => `
    <label class="checkbox-pill">
      <input type="checkbox" value="${escapeHtml(platform.platform_id)}" checked />
      <span>${escapeHtml(platform.platform_name)}</span>
    </label>
  `).join("");
}

async function generateQuestions() {
  const brandName = $("brandName").value.trim();
  const intention = $("intention").value.trim();
  $("createMessage").textContent = "正在用 GPT-5.5 生成问题...";
  $("generateQuestionsButton").disabled = true;
  try {
    const payload = await fetchJson("/api/monitor/generate-questions", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({brand_name: brandName, intention}),
    });
    state.generated = payload.questions;
    renderGeneratedQuestions();
    $("questionConfirmPanel").classList.remove("hidden");
    $("createMessage").textContent = `已生成 15 个问题。剩余可创建次数：${payload.remaining_quota}`;
  } catch (error) {
    $("createMessage").textContent = error.message;
  } finally {
    $("generateQuestionsButton").disabled = false;
  }
}

function renderGeneratedQuestions() {
  $("generatedQuestions").innerHTML = state.generated.map((item, index) => `
    <label class="question-editor-row">
      <span>${escapeHtml(item.question_id)}</span>
      <input data-question-index="${index}" value="${escapeHtml(item.question)}" />
    </label>
  `).join("");
  document.querySelectorAll("[data-question-index]").forEach((input) => {
    input.addEventListener("input", () => {
      state.generated[Number(input.dataset.questionIndex)].question = input.value;
    });
  });
}

async function startMonitor() {
  const selected = [...document.querySelectorAll("#platformChoices input:checked")].map((item) => item.value);
  $("createMessage").textContent = "正在创建监测任务...";
  $("startMonitorButton").disabled = true;
  try {
    const payload = await fetchJson("/api/monitor/start", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        brand_name: $("brandName").value.trim(),
        intention: $("intention").value.trim(),
        questions: state.generated,
        selected_platforms: selected,
      }),
    });
    state.currentMonitorId = payload.monitor.id;
    $("createMessage").textContent = "监测已开始。";
    setUserView("results");
    await loadMonitors();
    startMonitorPolling(payload.monitor.id);
  } catch (error) {
    $("createMessage").textContent = error.message;
  } finally {
    $("startMonitorButton").disabled = false;
  }
}

function startMonitorPolling(monitorId) {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    const payload = await fetchJson(`/api/monitor?id=${encodeURIComponent(monitorId)}`);
    renderMonitorDetail(payload);
    if (!isActiveMonitorStatus(payload.monitor.status)) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      await loadMonitors();
    }
  }, 2000);
}

async function loadMonitors() {
  const monitors = await fetchJson("/api/monitors");
  updateMonitorQuota(monitors);
  $("monitorList").innerHTML = monitors.map((monitor) => `
    <button class="monitor-item" data-monitor-id="${monitor.id}">
      <strong>${escapeHtml(monitor.brand_name)} · ${escapeHtml(monitor.intention)}</strong>
      <span>${escapeHtml(monitor.status)} · ${escapeHtml(monitor.created_at)}${monitor.completed_at ? ` · 完成 ${escapeHtml(monitor.completed_at)}` : ""}</span>
    </button>
  `).join("") || `<div class="empty-state compact">暂无监测任务</div>`;
  document.querySelectorAll("[data-monitor-id]").forEach((button) => {
    button.addEventListener("click", () => loadMonitorDetail(button.dataset.monitorId));
  });
  if (monitors.length && !state.currentMonitorId) {
    state.currentMonitorId = monitors[0].id;
    await loadMonitorDetail(state.currentMonitorId);
  }
}

function updateMonitorQuota(monitors) {
  const quotaTotal = Number(state.user?.quota_total ?? 3);
  const quota = Math.max(quotaTotal - monitors.length, 0);
  $("monitorQuota").textContent = `可用监控次数：${quota}`;
}

async function loadMonitorDetail(id) {
  state.currentMonitorId = id;
  const payload = await fetchJson(`/api/monitor?id=${encodeURIComponent(id)}`);
  renderMonitorDetail(payload);
  if (isActiveMonitorStatus(payload.monitor.status)) startMonitorPolling(id);
}

function renderMonitorDetail(payload) {
  const monitor = payload.monitor;
  const total = monitor.progress_total || 0;
  const current = monitor.progress_current || 0;
  const failedCount = payload.run ? payload.run.answers.filter((answer) => !isSuccessfulAnswer(answer)).length : 0;
  const canRetry = failedCount > 0 && !isActiveMonitorStatus(monitor.status);
  $("monitorDetail").innerHTML = `
    <section class="panel">
      <div class="panel-header">
        <div>
          <h3>${escapeHtml(monitor.brand_name)} · ${escapeHtml(monitor.intention)}</h3>
          <p class="panel-note">${escapeHtml(monitor.status)} · ${escapeHtml(monitor.progress_message || "")}</p>
        </div>
        ${canRetry ? `<button id="retryFailedButton" class="secondary-button">重试失败请求 (${failedCount})</button>` : ""}
      </div>
      <div class="bar large"><div class="bar-fill" style="width:${total ? Math.round(current / total * 100) : 0}%"></div></div>
      <p class="panel-note">${current}/${total} · ${escapeHtml(monitor.notification_message || "")}</p>
    </section>
    ${renderRunResult(payload.run)}
  `;
  $("retryFailedButton")?.addEventListener("click", () => retryFailedRequests(monitor.id));
  if (payload.run) bindRunResultControls(payload.run);
}

async function retryFailedRequests(monitorId) {
  const button = $("retryFailedButton");
  if (button) {
    button.disabled = true;
    button.textContent = "正在启动重试...";
  }
  try {
    await fetchJson("/api/monitor/retry", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({monitor_id: monitorId}),
    });
    startMonitorPolling(monitorId);
  } catch (error) {
    if (button) {
      button.disabled = false;
      button.textContent = error.message;
    }
  }
}

function renderRunResult(run) {
  if (!run) return "";
  const platforms = [...new Set(run.answers.map((item) => item.platform_id))].sort();
  const questions = uniqueBy(run.answers, (item) => item.question_id).sort((a, b) => a.question_id.localeCompare(b.question_id));
  return `
    <section class="panel">
      <h3>品牌提及率与排名</h3>
      <div class="keyword-grid">
        ${run.global_summary.map((row) => `
          <div class="keyword-card">
            <strong>${escapeHtml(row.keyword)}</strong>
            <span class="rate">${formatPercent(Number(row.appearance_rate || 0))}</span>
            <span class="eyebrow">均排 ${escapeHtml(row.avg_rank || "-")} · 最佳 ${escapeHtml(row.best_rank || "-")}</span>
          </div>
        `).join("")}
      </div>
    </section>
    <section class="panel">
      <nav class="tabs result-tabs">
        <button class="tab active" data-result-tab="platforms">各平台表现</button>
        <button class="tab" data-result-tab="questions">问题列表</button>
        <button class="tab" data-result-tab="answers">回答详情</button>
      </nav>
      <section id="resultTab-platforms" class="result-tab-panel">
        <div class="filters compact-filters">
          <select id="platformSummaryFilter">
            <option value="">全部平台</option>
            ${platforms.map((platform) => `<option value="${escapeHtml(platform)}">${escapeHtml(platform)}</option>`).join("")}
          </select>
        </div>
        <div class="table-wrap"><table><thead><tr><th>平台</th><th>品牌</th><th>出现</th><th>出现率</th><th>均排</th></tr></thead><tbody id="platformSummaryBody">
          ${renderPlatformSummaryRows(run.platform_summary)}
        </tbody></table></div>
      </section>
      <section id="resultTab-questions" class="result-tab-panel hidden">
        <div class="filters compact-filters">
          <select id="questionPlatformFilter">
            <option value="">全部平台</option>
            ${platforms.map((platform) => `<option value="${escapeHtml(platform)}">${escapeHtml(platform)}</option>`).join("")}
          </select>
        </div>
        <div id="resultQuestionList" class="question-list">${renderQuestionCards(run.answers, "")}</div>
      </section>
      <section id="resultTab-answers" class="result-tab-panel hidden">
        <div class="filters compact-filters">
          <select id="answerPlatformFilter">
            <option value="">全部平台</option>
            ${platforms.map((platform) => `<option value="${escapeHtml(platform)}">${escapeHtml(platform)}</option>`).join("")}
          </select>
          <select id="answerQuestionFilter">
            <option value="">全部问题</option>
            ${questions.map((question) => `<option value="${escapeHtml(question.question_id)}">${escapeHtml(question.question_id)}</option>`).join("")}
          </select>
        </div>
        <div id="resultAnswerList" class="answer-list">${renderAnswerCards(run.answers, run.run_id)}</div>
      </section>
    </section>
  `;
}

function bindRunResultControls(run) {
  document.querySelectorAll("[data-result-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("[data-result-tab]").forEach((item) => item.classList.toggle("active", item === button));
      document.querySelectorAll(".result-tab-panel").forEach((panel) => panel.classList.add("hidden"));
      $(`resultTab-${button.dataset.resultTab}`).classList.remove("hidden");
    });
  });
  $("platformSummaryFilter")?.addEventListener("change", (event) => {
    const platform = event.target.value;
    $("platformSummaryBody").innerHTML = renderPlatformSummaryRows(run.platform_summary.filter((row) => !platform || row.platform_id === platform));
  });
  $("questionPlatformFilter")?.addEventListener("change", (event) => {
    $("resultQuestionList").innerHTML = renderQuestionCards(run.answers, event.target.value);
  });
  const renderFilteredAnswers = () => {
    const platform = $("answerPlatformFilter").value;
    const question = $("answerQuestionFilter").value;
    $("resultAnswerList").innerHTML = renderAnswerCards(
      run.answers.filter((answer) => (!platform || answer.platform_id === platform) && (!question || answer.question_id === question)),
      run.run_id,
    );
  };
  $("answerPlatformFilter")?.addEventListener("change", renderFilteredAnswers);
  $("answerQuestionFilter")?.addEventListener("change", renderFilteredAnswers);
}

function renderPlatformSummaryRows(rows) {
  return rows.map((row) => `<tr><td>${escapeHtml(row.platform_id)}</td><td>${escapeHtml(row.keyword)}</td><td>${escapeHtml(row.appeared_count)}/${escapeHtml(row.total_questions)}</td><td>${formatPercent(Number(row.appearance_rate || 0))}</td><td>${escapeHtml(row.avg_rank || "-")}</td></tr>`).join("");
}

function renderQuestionCards(answers, platform) {
  const rows = platform ? answers.filter((item) => item.platform_id === platform) : answers;
  return uniqueBy(rows, (item) => item.question_id)
    .sort((a, b) => a.question_id.localeCompare(b.question_id))
    .map((item) => {
      const scoped = rows.filter((answer) => answer.question_id === item.question_id);
      const success = scoped.filter((answer) => isSuccessfulAnswer(answer)).length;
      return `<div class="question-card"><strong>${escapeHtml(item.question_id)} · ${success}/${scoped.length} success</strong><p>${escapeHtml(item.question)}</p></div>`;
    })
    .join("");
}

function renderAnswerCards(answers, runId) {
  return answers.map((answer) => {
    const screenshotUrl = answer.screenshot_path ? `/runs/${encodeURIComponent(runId)}/${answer.screenshot_path}` : "";
    return `<div class="answer-card"><div><div class="badge-row"><span class="badge ${escapeHtml(answer.status)}">${escapeHtml(answer.status)}</span><span class="badge">${escapeHtml(answer.platform_id)}</span><span class="badge">${escapeHtml(answer.question_id)}</span></div><strong>${escapeHtml(answer.question)}</strong><p>${escapeHtml(answer.error_message || truncate(answer.answer_text || "", 220))}</p></div>${screenshotUrl ? `<a class="answer-action" href="${escapeHtml(screenshotUrl)}" target="_blank" rel="noreferrer">查看截图</a>` : ""}</div>`;
  }).join("");
}

function setUserView(view) {
  document.querySelectorAll("[data-user-view]").forEach((button) => button.classList.toggle("active", button.dataset.userView === view));
  $("createView").classList.toggle("hidden", view !== "create");
  $("resultsView").classList.toggle("hidden", view !== "results");
}

async function loadAdminConfig() {
  state.config = await fetchJson("/api/admin/config");
  renderAdminConfig();
}

async function loadAdminUsers() {
  const payload = await fetchJson("/api/admin/users");
  renderAdminUsers(payload.users || []);
}

function renderAdminUsers(users) {
  $("adminUsersPanel").classList.remove("hidden");
  $("adminUsers").innerHTML = users.map((user) => `
    <article class="admin-user-card" data-admin-user="${escapeHtml(user.id)}">
      <div class="admin-user-head">
        <div>
          <strong>${escapeHtml(user.company_name)}</strong>
          <span>${escapeHtml(user.phone)} · 注册 ${escapeHtml(user.created_at)} · 最近登录 ${escapeHtml(user.last_login_at)}</span>
        </div>
        <div class="quota-editor">
          <label><span>可创建总次数</span><input class="admin-user-quota" type="number" min="0" max="999" value="${escapeHtml(user.quota_total ?? 3)}" /></label>
          <button class="secondary-button admin-save-quota" type="button">保存次数</button>
        </div>
      </div>
      <p class="panel-note">已创建 ${escapeHtml(user.monitor_count || 0)} 次，可用 ${escapeHtml(user.remaining_quota || 0)} 次。</p>
      <div class="admin-monitor-list">
        ${(user.monitors || []).map((monitor) => `
          <div class="admin-monitor-row">
            <div>
              <strong>${escapeHtml(monitor.brand_name)} · ${escapeHtml(monitor.intention)}</strong>
              <span>${escapeHtml(monitor.status)} · 创建 ${escapeHtml(monitor.created_at)}${monitor.completed_at ? ` · 完成 ${escapeHtml(monitor.completed_at)}` : ""}</span>
            </div>
            <span class="badge">${escapeHtml(monitor.run_id || "no run")}</span>
          </div>
        `).join("") || `<div class="empty-state compact">暂无监测任务</div>`}
      </div>
    </article>
  `).join("") || `<div class="empty-state compact">暂无注册用户</div>`;
  document.querySelectorAll("[data-admin-user]").forEach((card) => {
    card.querySelector(".admin-save-quota").addEventListener("click", () => saveUserQuota(card));
  });
}

async function saveUserQuota(card) {
  const userId = Number(card.dataset.adminUser);
  const quotaTotal = Number(card.querySelector(".admin-user-quota").value || 0);
  await fetchJson("/api/admin/user-quota", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({user_id: userId, quota_total: quotaTotal}),
  });
  await loadAdminUsers();
}

function renderAdminConfig() {
  $("adminConfigPanel").classList.toggle("hidden", false);
  state.config.runner = state.config.runner || {};
  document.querySelectorAll("input[name='adminRunMode']").forEach((input) => input.checked = input.value === (state.config.run_mode || "browser"));
  $("adminBrowserConcurrency").value = state.config.runner.browser_concurrency || 2;
  $("adminApiConcurrency").value = state.config.runner.api_concurrency || 5;
  $("adminBrowserPanel").classList.toggle("hidden", (state.config.run_mode || "browser") !== "browser");
  $("adminApiPanel").classList.toggle("hidden", state.config.run_mode !== "api");
  $("adminWebsites").innerHTML = state.config.browser_platforms.map((platform, index) => `
    <div class="config-card" data-admin-website="${index}">
      <label class="checkbox-field"><input class="admin-website-enabled" type="checkbox" ${platform.enabled !== false ? "checked" : ""}/> <span>启用</span></label>
      <label><span>网站 ID</span><input class="admin-website-id" value="${escapeHtml(platform.platform_id)}"/></label>
      <label><span>网站名称</span><input class="admin-website-name" value="${escapeHtml(platform.platform_name)}"/></label>
      <label><span>访问地址</span><input class="admin-website-url" value="${escapeHtml(platform.url || "")}"/></label>
      <button class="secondary-button admin-prepare-login" type="button">准备登录</button>
    </div>
  `).join("");
  $("adminModels").innerHTML = state.config.api_platforms.map((platform, index) => `
    <div class="config-card" data-admin-model="${index}">
      <label><span>平台 ID</span><input class="admin-model-id" value="${escapeHtml(platform.platform_id)}"/></label>
      <label><span>显示名称</span><input class="admin-model-name" value="${escapeHtml(platform.platform_name)}"/></label>
      <label><span>模型 ID</span><input class="admin-model-value" value="${escapeHtml(platform.model || "")}"/></label>
      <label><span>API Base URL</span><input class="admin-model-url" value="${escapeHtml(platform.api_base_url || "")}"/></label>
      <label class="checkbox-field"><input class="admin-model-enabled" type="checkbox" ${platform.enabled !== false ? "checked" : ""}/> <span>启用</span></label>
    </div>
  `).join("");
  bindAdminEditors();
}

function bindAdminEditors() {
  $("adminBrowserConcurrency").addEventListener("input", (e) => state.config.runner.browser_concurrency = Number(e.target.value || 2));
  $("adminApiConcurrency").addEventListener("input", (e) => state.config.runner.api_concurrency = Number(e.target.value || 5));
  document.querySelectorAll("[data-admin-website]").forEach((card) => {
    const platform = state.config.browser_platforms[Number(card.dataset.adminWebsite)];
    card.querySelector(".admin-website-enabled").addEventListener("change", (e) => platform.enabled = e.target.checked);
    card.querySelector(".admin-website-id").addEventListener("input", (e) => platform.platform_id = e.target.value);
    card.querySelector(".admin-website-name").addEventListener("input", (e) => platform.platform_name = e.target.value);
    card.querySelector(".admin-website-url").addEventListener("input", (e) => platform.url = e.target.value);
    card.querySelector(".admin-prepare-login").addEventListener("click", () => prepareLogin(platform.platform_id));
  });
  document.querySelectorAll("[data-admin-model]").forEach((card) => {
    const platform = state.config.api_platforms[Number(card.dataset.adminModel)];
    card.querySelector(".admin-model-enabled").addEventListener("change", (e) => platform.enabled = e.target.checked);
    card.querySelector(".admin-model-id").addEventListener("input", (e) => platform.platform_id = e.target.value);
    card.querySelector(".admin-model-name").addEventListener("input", (e) => platform.platform_name = e.target.value);
    card.querySelector(".admin-model-value").addEventListener("input", (e) => platform.model = e.target.value);
    card.querySelector(".admin-model-url").addEventListener("input", (e) => platform.api_base_url = e.target.value);
  });
}

function addAdminModel() {
  state.config.api_platforms.push({platform_id: `model_${state.config.api_platforms.length + 1}`, platform_name: "新模型", method: "api", model: "gpt-5.5", enabled: true, web_search: true});
  renderAdminConfig();
}

async function saveAdminConfig() {
  $("adminConfigMessage").textContent = "正在保存...";
  try {
    await fetchJson("/api/admin/config", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
      run_mode: state.config.run_mode || "browser",
      questions: state.config.questions && state.config.questions.length ? state.config.questions : [{question_id: "Q001", question: "placeholder"}],
      target_keywords: state.config.target_keywords && state.config.target_keywords.length ? state.config.target_keywords : [{keyword: "placeholder", aliases: []}],
      browser_platforms: state.config.browser_platforms,
      api_platforms: state.config.api_platforms,
      runner: {
        browser_concurrency: Number(state.config.runner?.browser_concurrency || 2),
        api_concurrency: Number(state.config.runner?.api_concurrency || 5),
      },
    })});
    $("adminConfigMessage").textContent = "已保存。";
    await loadAdminConfig();
  } catch (error) {
    $("adminConfigMessage").textContent = error.message;
  }
}

async function prepareLogin(platformId) {
  $("adminLoginLog").textContent = `正在打开 ${platformId} 登录窗口...`;
  await fetchJson("/api/login-prepare", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({platform_id: platformId})});
  const timer = setInterval(async () => {
    const status = await fetchJson("/api/login-status");
    $("adminLoginLog").textContent = status.lines.join("\n");
    if (!status.running) clearInterval(timer);
  }, 1500);
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const message = await response.text();
    try {
      const payload = JSON.parse(message);
      throw new Error(payload.error || `HTTP ${response.status}`);
    } catch (error) {
      if (error instanceof SyntaxError) {
        throw new Error(message.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim() || `HTTP ${response.status}`);
      }
      throw error;
    }
  }
  return response.json();
}

function uniqueBy(items, keyFn) {
  const seen = new Set();
  return items.filter((item) => {
    const key = keyFn(item);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function isActiveMonitorStatus(status) {
  return ["queued", "running", "retrying"].includes(status);
}

function isSuccessfulAnswer(answer) {
  return ["success", "partial_success"].includes(answer.status);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"}[char]));
}

function truncate(value, length) {
  const text = String(value || "");
  return text.length <= length ? text : `${text.slice(0, length - 1)}…`;
}

function formatPercent(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

init();

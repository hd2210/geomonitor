const state = {
  user: null,
  admin: false,
  config: null,
  platforms: [],
  questionCount: 15,
  generated: [],
  currentMonitorId: null,
  activeMonitorId: null,
  pollTimer: null,
  monitorListTimer: null,
  adminTab: "config",
  reportChart: null,
  accountStatuses: [],
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
  document.querySelectorAll("[data-admin-tab]").forEach((button) => {
    button.addEventListener("click", () => setAdminTab(button.dataset.adminTab));
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
  $("adminTabs")?.classList.toggle("hidden", !state.admin);
  $("saveAdminConfigButton")?.classList.toggle("hidden", !state.admin);
  $("adminLogoutButton")?.classList.toggle("hidden", !state.admin);
  if (state.admin) {
    await loadAdminConfig();
    await loadAdminUsers();
    setAdminTab("config");
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
  state.questionCount = payload.question_count || 15;
  $("enabledPlatformNames").textContent = state.platforms.map((item) => item.platform_name).join("、") || "暂无启用平台";
  $("configuredQuestionCount").textContent = state.questionCount;
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
  $("createMessage").textContent = "正在生成问题...";
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
    $("createMessage").textContent = `已生成 ${payload.question_count || payload.questions.length} 个问题。剩余可创建次数：${payload.remaining_quota}`;
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
    state.activeMonitorId = payload.monitor.id;
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
  state.activeMonitorId = monitorId;
  state.pollTimer = setInterval(async () => {
    try {
      const payload = await fetchJson(`/api/monitor?id=${encodeURIComponent(monitorId)}`);
      await loadMonitors({preserveDetail: true});
      if (isActiveMonitorStatus(payload.monitor.status)) {
        if (state.currentMonitorId === monitorId) {
          renderMonitorUnavailable(payload.monitor);
        }
        return;
      }
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      state.activeMonitorId = null;
      await loadMonitors({preserveDetail: true});
      if (state.currentMonitorId === monitorId || !$("monitorDetail").innerHTML.trim()) {
        await loadMonitorDetail(monitorId);
      }
    } catch (error) {
      console.warn("monitor polling failed", error);
    }
  }, 2000);
}

async function loadMonitors(options = {}) {
  const monitors = await fetchJson("/api/monitors");
  updateMonitorQuota(monitors);
  $("monitorList").innerHTML = monitors.map((monitor) => `
    <button class="monitor-item ${monitor.id === state.currentMonitorId ? "active" : ""}" data-monitor-id="${monitor.id}" ${isActiveMonitorStatus(monitor.status) ? "data-active-monitor='true'" : ""}>
      <strong>${escapeHtml(monitor.brand_name)} · ${escapeHtml(monitor.intention)}</strong>
      <span>${escapeHtml(monitor.status)} · ${escapeHtml(monitor.created_at)}${monitor.completed_at ? ` · 完成 ${escapeHtml(monitor.completed_at)}` : ""}</span>
      ${isActiveMonitorStatus(monitor.status) ? `<em>${escapeHtml(monitor.progress_message || "运行中")} · ${escapeHtml(monitor.progress_current || 0)}/${escapeHtml(monitor.progress_total || 0)}</em>` : ""}
    </button>
  `).join("") || `<div class="empty-state compact">暂无监测任务</div>`;
  document.querySelectorAll("[data-monitor-id]").forEach((button) => {
    button.addEventListener("click", () => {
      const id = Number(button.dataset.monitorId);
      const monitor = monitors.find((item) => Number(item.id) === id);
      if (monitor && isActiveMonitorStatus(monitor.status)) {
        state.currentMonitorId = id;
        renderMonitorUnavailable(monitor);
        return;
      }
      loadMonitorDetail(id);
    });
  });
  updateMonitorListPolling(monitors);
  const current = monitors.find((monitor) => Number(monitor.id) === Number(state.currentMonitorId));
  if (options.preserveDetail && current && !isActiveMonitorStatus(current.status) && $("monitorDetail")?.querySelector(".running-panel")) {
    await loadMonitorDetail(current.id);
  }
  if (!options.preserveDetail && monitors.length && !state.currentMonitorId) {
    const firstCompleted = monitors.find((monitor) => !isActiveMonitorStatus(monitor.status));
    if (firstCompleted) {
      state.currentMonitorId = firstCompleted.id;
      await loadMonitorDetail(state.currentMonitorId);
    } else {
      state.currentMonitorId = monitors[0].id;
      renderMonitorUnavailable(monitors[0]);
    }
  }
}

function updateMonitorListPolling(monitors) {
  const active = monitors.find((monitor) => isActiveMonitorStatus(monitor.status));
  if (active && !state.monitorListTimer) {
    state.monitorListTimer = setInterval(() => {
      loadMonitors({preserveDetail: true}).catch((error) => console.warn("monitor list polling failed", error));
    }, 3000);
  }
  if (!active && state.monitorListTimer) {
    clearInterval(state.monitorListTimer);
    state.monitorListTimer = null;
  }
  const currentActive = monitors.find((monitor) => Number(monitor.id) === Number(state.currentMonitorId) && isActiveMonitorStatus(monitor.status));
  if (currentActive && !state.pollTimer) {
    startMonitorPolling(currentActive.id);
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
  if (isActiveMonitorStatus(payload.monitor.status)) {
    renderMonitorUnavailable(payload.monitor);
    startMonitorPolling(id);
    return;
  }
  renderMonitorDetail(payload);
}

function renderMonitorUnavailable(monitor) {
  const total = monitor.progress_total || 0;
  const current = monitor.progress_current || 0;
  $("monitorDetail").innerHTML = `
    <section class="panel running-panel">
      <div class="panel-header">
        <div>
          <h3>${escapeHtml(monitor.brand_name)} · ${escapeHtml(monitor.intention)}</h3>
          <p class="panel-note">任务正在运行，完成后可查看完整报告。</p>
        </div>
        <span class="badge running">运行中</span>
      </div>
      <div class="bar large"><div class="bar-fill" style="width:${total ? Math.round(current / total * 100) : 0}%"></div></div>
      <p class="panel-note">${current}/${total} · ${escapeHtml(monitor.progress_message || "")}</p>
    </section>
  `;
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
    ${renderRunResult(payload.run, monitor)}
  `;
  $("retryFailedButton")?.addEventListener("click", () => retryFailedRequests(monitor.id));
  if (payload.run) bindRunResultControls(payload.run, monitor);
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

function renderRunResult(run, monitor, options = {}) {
  if (!run) return "";
  const platforms = [...new Set(run.answers.map((item) => item.platform_id))].sort();
  const questions = uniqueBy(run.answers, (item) => item.question_id).sort((a, b) => a.question_id.localeCompare(b.question_id));
  const metrics = buildReportMetrics(run, monitor, "");
  return `
    <section class="report-hero panel">
      <div>
        <p class="panel-note">监测意图：</p>
        <h3>${escapeHtml(monitor.intention)}</h3>
        <p class="panel-note">本品名称：<strong>${escapeHtml(monitor.brand_name)}</strong></p>
      </div>
      <div class="report-actions">
        <span class="badge success">${escapeHtml(monitor.status)}</span>
      </div>
    </section>
    <section class="panel report-filter-panel">
      <label>
        <span>查看平台</span>
        <select id="platformSummaryFilter">
          <option value="">全部平台</option>
          ${platforms.map((platform) => `<option value="${escapeHtml(platform)}">${escapeHtml(platform)}</option>`).join("")}
        </select>
      </label>
    </section>
    <section class="metric-strip">
      <article class="metric-card"><span>提及率 / 平均排名</span><strong id="metricTargetRate">${formatPercent(metrics.targetRate)} / ${escapeHtml(metrics.targetAvgRank)}</strong></article>
      <article class="metric-card"><span>Top1推荐率</span><strong id="metricTop1Rate">${formatPercent(metrics.top1Rate)}</strong></article>
      <article class="metric-card"><span>Top3推荐率</span><strong id="metricTop3Rate">${formatPercent(metrics.top3Rate)}</strong></article>
      <article class="metric-card"><span>Top5推荐率</span><strong id="metricTop5Rate">${formatPercent(metrics.top5Rate)}</strong></article>
      <article class="metric-card"><span>引用信源</span><strong id="metricCitationCount">${escapeHtml(metrics.citationCount)}</strong></article>
    </section>
    <section class="panel">
      <nav class="tabs result-tabs">
        <button class="tab active" data-result-tab="platforms">各平台表现</button>
        <button class="tab" data-result-tab="questions">问题列表</button>
        <button class="tab" data-result-tab="citations">引用信源</button>
        <button class="tab" data-result-tab="answers">回答详情</button>
      </nav>
      <section id="resultTab-platforms" class="result-tab-panel">
        <div class="report-grid">
          <article class="report-card">
            <h3>提及率排行榜</h3>
            <div class="table-wrap"><table><thead><tr><th>排名</th><th>品牌</th><th>占比</th></tr></thead><tbody id="globalRankingBody">
              ${renderGlobalRankingRows(run.global_summary, monitor.brand_name)}
            </tbody></table></div>
          </article>
          <article class="report-card">
            <h3>本品在各AI平台提及率排名</h3>
            <canvas id="platformRankChart" height="170"></canvas>
          </article>
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
        <div id="resultAnswerList" class="answer-list">${renderAnswerCards(run.answers, run.run_id, monitor.id, options)}</div>
      </section>
      <section id="resultTab-citations" class="result-tab-panel hidden">
        <div class="filters compact-filters">
          <select id="citationPlatformFilter">
            <option value="">全部平台</option>
            ${platforms.map((platform) => `<option value="${escapeHtml(platform)}">${escapeHtml(platform)}</option>`).join("")}
          </select>
        </div>
        <div id="citationSourceList" class="citation-layout">${renderCitationSources(run.answers, "")}</div>
      </section>
    </section>
  `;
}

function bindRunResultControls(run, monitor, options = {}) {
  document.querySelectorAll("[data-result-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("[data-result-tab]").forEach((item) => item.classList.toggle("active", item === button));
      document.querySelectorAll(".result-tab-panel").forEach((panel) => panel.classList.add("hidden"));
      $(`resultTab-${button.dataset.resultTab}`).classList.remove("hidden");
    });
  });
  $("platformSummaryFilter")?.addEventListener("change", (event) => {
    updatePlatformReport(run, monitor, event.target.value);
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
      state.currentMonitorId,
      options,
    );
    if (options.allowRetry !== false) bindRetryAnswerButtons();
  };
  $("answerPlatformFilter")?.addEventListener("change", renderFilteredAnswers);
  $("answerQuestionFilter")?.addEventListener("change", renderFilteredAnswers);
  if (options.allowRetry !== false) bindRetryAnswerButtons();
  $("citationPlatformFilter")?.addEventListener("change", (event) => {
    $("citationSourceList").innerHTML = renderCitationSources(run.answers, event.target.value);
    bindCitationSourceClicks(run.answers, event.target.value);
  });
  bindCitationSourceClicks(run.answers, "");
  updatePlatformReport(run, monitor, "");
}

function updatePlatformReport(run, monitor, platform) {
  const metrics = buildReportMetrics(run, monitor, platform);
  $("metricTargetRate").textContent = `${formatPercent(metrics.targetRate)} / ${escapeHtml(metrics.targetAvgRank)}`;
  $("metricTop1Rate").textContent = formatPercent(metrics.top1Rate);
  $("metricTop3Rate").textContent = formatPercent(metrics.top3Rate);
  $("metricTop5Rate").textContent = formatPercent(metrics.top5Rate);
  $("metricCitationCount").textContent = metrics.citationCount;
  const rankingRows = platform ? run.platform_summary.filter((row) => row.platform_id === platform) : run.global_summary;
  $("globalRankingBody").innerHTML = renderGlobalRankingRows(rankingRows, monitor.brand_name);
  $("platformSummaryBody").innerHTML = renderPlatformSummaryRows(run.platform_summary.filter((row) => !platform || row.platform_id === platform));
  renderReportCharts(run, monitor, platform);
}

function buildReportMetrics(run, monitor, platform = "") {
  const summaryRows = platform ? run.platform_summary.filter((row) => row.platform_id === platform) : run.global_summary;
  const target = summaryRows.find((row) => normalizeText(row.keyword) === normalizeText(monitor.brand_name)) || summaryRows[0] || {};
  const successfulAnswers = run.answers.filter((answer) => isSuccessfulAnswer(answer) && (!platform || answer.platform_id === platform));
  const targetAnalyses = (run.analyses || [])
    .filter((item) => !platform || item.platform_id === platform)
    .flatMap((item) => item.keyword_analysis || [])
    .filter((item) => normalizeText(item.keyword) === normalizeText(target.keyword || monitor.brand_name) && item.appeared);
  const denominator = successfulAnswers.length || Number(target.total_answers || target.total_questions || 0) || 1;
  const countRank = (limit) => targetAnalyses.filter((item) => Number(item.rank || 999) <= limit).length / denominator;
  return {
    targetRate: Number(target.appearance_rate || 0),
    targetAvgRank: target.avg_rank || "-",
    top1Rate: countRank(1),
    top3Rate: countRank(3),
    top5Rate: countRank(5),
    citationCount: citationRows(run.answers, platform).length,
  };
}

function renderGlobalRankingRows(rows, brandName) {
  return [...rows]
    .sort((a, b) => Number(b.appearance_rate || 0) - Number(a.appearance_rate || 0))
    .map((row, index) => `<tr><td><span class="rank-index">${index + 1}</span></td><td>${escapeHtml(row.keyword)}${normalizeText(row.keyword) === normalizeText(brandName) ? ` <span class="brand-tag">当前品牌</span>` : ""}</td><td class="number-cell">${formatPercent(Number(row.appearance_rate || 0))}</td></tr>`)
    .join("");
}

function renderReportCharts(run, monitor, platform = "") {
  if (!window.Chart) return;
  const canvas = $("platformRankChart");
  if (!canvas) return;
  if (state.reportChart) {
    state.reportChart.destroy();
    state.reportChart = null;
  }
  const targetKeyword = (run.global_summary.find((row) => normalizeText(row.keyword) === normalizeText(monitor.brand_name)) || run.global_summary[0] || {}).keyword;
  const platformRows = run.platform_summary
    .filter((row) => row.keyword === targetKeyword && (!platform || row.platform_id === platform))
    .slice(0, 8);
  state.reportChart = new Chart(canvas, {
    type: "bar",
    data: {
      labels: platformRows.map((row) => row.platform_id),
      datasets: [{
        label: "提及率",
        data: platformRows.map((row) => Number(row.appearance_rate || 0) * 100),
        backgroundColor: "#2563ff",
        borderRadius: 4,
      }],
    },
    options: {
      responsive: true,
      plugins: {legend: {display: false}},
      scales: {y: {beginAtZero: true, max: 100, ticks: {callback: (value) => `${value}%`}}},
    },
  });
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

function renderAnswerCards(answers, runId, monitorId, options = {}) {
  return answers.map((answer) => {
    const screenshotUrl = answer.screenshot_path ? `/runs/${encodeURIComponent(runId)}/${answer.screenshot_path}` : "";
    const canRetry = options.allowRetry !== false && ["failed", "partial_success", "blocked"].includes(answer.status);
    const citationNote = answer.citation_error ? `引用信源抓取失败：${answer.citation_error}` : `引用信源：${(answer.citations || []).length} 条`;
    const answerUrl = answer.answer_url || "";
    const accountBadge = answer.account_id ? `<span class="badge">${escapeHtml(answer.account_name || answer.account_id)}</span>` : "";
    const questionBadge = answerUrl
      ? `<a class="badge question-link" href="${escapeHtml(answerUrl)}" target="_blank" rel="noreferrer" title="打开回答">${escapeHtml(answer.question_id)}</a>`
      : `<span class="badge">${escapeHtml(answer.question_id)}</span>`;
    return `<div class="answer-card"><div><div class="badge-row"><span class="badge ${escapeHtml(answer.status)}">${escapeHtml(answer.status)}</span><span class="badge">${escapeHtml(answer.platform_id)}</span>${accountBadge}${questionBadge}</div><strong>${escapeHtml(answer.question)}</strong><p>${escapeHtml(answer.error_message || truncate(answer.answer_text || "", 220))}</p><p class="panel-note">${escapeHtml(citationNote)}</p></div><div class="answer-actions">${screenshotUrl ? `<a class="answer-action" href="${escapeHtml(screenshotUrl)}" target="_blank" rel="noreferrer">查看截图</a>` : ""}${canRetry ? `<button class="answer-action retry-answer" data-monitor-id="${escapeHtml(monitorId)}" data-platform-id="${escapeHtml(answer.platform_id)}" data-question-id="${escapeHtml(answer.question_id)}">单条重试</button>` : ""}</div></div>`;
  }).join("");
}

function renderCitationSources(answers, platform) {
  const rows = citationRows(answers, platform);
  if (!rows.length) return `<div class="empty-state compact">暂无引用信源数据</div>`;
  const sourceCounts = new Map();
  rows.forEach((row) => {
    const key = hostnameFromUrl(row.url);
    sourceCounts.set(key, (sourceCounts.get(key) || 0) + 1);
  });
  return `
    <div class="citation-sources">
      ${[...sourceCounts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([site, count], index) => `
        <button class="citation-source ${index === 0 ? "active" : ""}" data-site-name="${escapeHtml(site)}">
          <strong>${escapeHtml(site)}</strong>
          <span>${count} 次引用</span>
        </button>
      `).join("")}
    </div>
    <div id="citationPageList" class="citation-pages"></div>
  `;
}

function bindCitationSourceClicks(answers, platform) {
  const buttons = [...document.querySelectorAll(".citation-source")];
  const renderPages = (site) => {
    buttons.forEach((button) => button.classList.toggle("active", button.dataset.siteName === site));
    $("citationPageList").innerHTML = renderCitationPages(citationRows(answers, platform), site);
  };
  buttons.forEach((button) => button.addEventListener("click", () => renderPages(button.dataset.siteName)));
  if (buttons[0]) renderPages(buttons[0].dataset.siteName);
}

function renderCitationPages(rows, site) {
  const pageMap = new Map();
  rows.filter((row) => hostnameFromUrl(row.url) === site).forEach((row) => {
    const key = row.url;
    const current = pageMap.get(key) || {title: row.title || row.url, url: row.url, count: 0};
    current.count += 1;
    pageMap.set(key, current);
  });
  return [...pageMap.values()]
    .sort((a, b) => b.count - a.count || a.title.localeCompare(b.title))
    .map((page) => `<a class="citation-page" href="${escapeHtml(page.url)}" target="_blank" rel="noreferrer"><strong>${escapeHtml(page.title)}</strong><span>${page.count} 次引用</span></a>`)
    .join("") || `<div class="empty-state compact">暂无引用网页</div>`;
}

function citationRows(answers, platform) {
  return answers
    .filter((answer) => !platform || answer.platform_id === platform)
    .flatMap((answer) => (answer.citations || []).map((citation) => ({
      platform_id: answer.platform_id,
      title: citation.title || citation.url,
      site_name: hostnameFromUrl(citation.url),
      url: citation.url,
    })))
    .filter((row) => row.url);
}

async function retrySingleAnswer(button) {
  button.disabled = true;
  button.textContent = "重试中...";
  await fetchJson("/api/monitor/retry-answer", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      monitor_id: Number(button.dataset.monitorId),
      platform_id: button.dataset.platformId,
      question_id: button.dataset.questionId,
    }),
  });
  startMonitorPolling(Number(button.dataset.monitorId));
}

function bindRetryAnswerButtons() {
  document.querySelectorAll(".retry-answer").forEach((button) => {
    button.addEventListener("click", () => retrySingleAnswer(button));
  });
}

function setUserView(view) {
  document.querySelectorAll("[data-user-view]").forEach((button) => button.classList.toggle("active", button.dataset.userView === view));
  $("createView").classList.toggle("hidden", view !== "create");
  $("resultsView").classList.toggle("hidden", view !== "results");
  document.querySelector(".user-main")?.classList.toggle("create-mode", view === "create");
  document.querySelector(".user-main")?.classList.toggle("results-mode", view === "results");
}

async function loadAdminConfig() {
  state.config = await fetchJson("/api/admin/config");
  await loadAdminAccountStatuses();
  renderAdminConfig();
}

async function loadAdminAccountStatuses() {
  const payload = await fetchJson("/api/admin/account-statuses");
  state.accountStatuses = payload.accounts || [];
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
  $("adminMonitors").innerHTML = users.map((user) => `
    <article class="admin-user-card">
      <div class="admin-user-head">
        <div>
          <strong>${escapeHtml(user.company_name)} · ${escapeHtml(user.phone)}</strong>
          <span>已创建 ${escapeHtml(user.monitor_count || 0)} 次，可用 ${escapeHtml(user.remaining_quota || 0)} 次</span>
        </div>
      </div>
      <div class="admin-monitor-list">
        ${(user.monitors || []).map((monitor) => `
          <button class="admin-monitor-row clickable" type="button" data-admin-monitor-id="${escapeHtml(monitor.id)}">
            <div>
              <strong>${escapeHtml(monitor.brand_name)} · ${escapeHtml(monitor.intention)}</strong>
              <span>${escapeHtml(monitor.status)} · 创建 ${escapeHtml(monitor.created_at)}${monitor.completed_at ? ` · 完成 ${escapeHtml(monitor.completed_at)}` : ""}</span>
            </div>
            <span class="badge">${escapeHtml(monitor.run_id || "no run")}</span>
          </button>
        `).join("") || `<div class="empty-state compact">暂无监测任务</div>`}
      </div>
    </article>
  `).join("") || `<div class="empty-state compact">暂无监测任务</div>`;
  document.querySelectorAll("[data-admin-user]").forEach((card) => {
    card.querySelector(".admin-save-quota").addEventListener("click", () => saveUserQuota(card));
  });
  document.querySelectorAll("[data-admin-monitor-id]").forEach((button) => {
    button.addEventListener("click", () => loadAdminMonitorDetail(button.dataset.adminMonitorId));
  });
}

async function loadAdminMonitorDetail(monitorId) {
  $("adminMonitorDetail").innerHTML = `<section class="panel"><p class="panel-note">正在加载监测详情...</p></section>`;
  try {
    const payload = await fetchJson(`/api/admin/monitor?id=${encodeURIComponent(monitorId)}`);
    const monitor = payload.monitor;
    const run = payload.run;
    const total = monitor.progress_total || 0;
    const current = monitor.progress_current || 0;
    $("adminMonitorDetail").innerHTML = `
      <section class="panel">
        <div class="panel-header">
          <div>
            <h3>${escapeHtml(monitor.brand_name)} · ${escapeHtml(monitor.intention)}</h3>
            <p class="panel-note">${escapeHtml(monitor.status)} · ${escapeHtml(monitor.progress_message || "")}</p>
          </div>
          <span class="badge">${escapeHtml(monitor.run_id || "no run")}</span>
        </div>
        <div class="bar large"><div class="bar-fill" style="width:${total ? Math.round(current / total * 100) : 0}%"></div></div>
        <p class="panel-note">${current}/${total}</p>
      </section>
      ${run ? renderRunResult(run, monitor, {allowRetry: false}) : `<section class="panel"><div class="empty-state compact">暂无可展示的运行结果</div></section>`}
    `;
    if (run) bindRunResultControls(run, monitor, {allowRetry: false});
  } catch (error) {
    $("adminMonitorDetail").innerHTML = `<section class="panel"><div class="empty-state compact">${escapeHtml(error.message)}</div></section>`;
  }
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
  $("adminQuestionCount").value = state.config.runner.question_count || 15;
  $("adminBrowserPanel").classList.toggle("hidden", (state.config.run_mode || "browser") !== "browser");
  $("adminApiPanel").classList.toggle("hidden", state.config.run_mode !== "api");
  $("adminWebsites").innerHTML = state.config.browser_platforms.map((platform, index) => `
    <section class="platform-config-card" data-admin-website="${index}">
      <header class="platform-config-head">
        <div>
          <strong>${escapeHtml(platform.platform_name || platform.platform_id)}</strong>
          <span>${escapeHtml(platform.platform_id)} · ${(platform.browser_mode || "playwright") === "cdp" ? "CDP 真实 Chrome" : "Playwright Chromium"}</span>
        </div>
        <div class="platform-config-actions">
          <label class="checkbox-field"><input class="admin-website-enabled" type="checkbox" ${platform.enabled !== false ? "checked" : ""}/> <span>启用</span></label>
          <button class="secondary-button admin-prepare-login" type="button">平台登录</button>
        </div>
      </header>
      <section class="platform-config-section">
        <div class="section-title-row">
          <strong>基础配置</strong>
          <span>平台级配置用于默认登录态和无账号池时的 CDP 连接。</span>
        </div>
        <div class="platform-field-grid">
          <label><span>网站 ID</span><input class="admin-website-id" value="${escapeHtml(platform.platform_id)}"/></label>
          <label><span>网站名称</span><input class="admin-website-name" value="${escapeHtml(platform.platform_name)}"/></label>
          <label class="wide-field"><span>访问地址</span><input class="admin-website-url" value="${escapeHtml(platform.url || "")}"/></label>
          <label><span>浏览器模式</span><select class="admin-website-browser-mode"><option value="playwright" ${(platform.browser_mode || "playwright") === "playwright" ? "selected" : ""}>Playwright Chromium</option><option value="cdp" ${platform.browser_mode === "cdp" ? "selected" : ""}>CDP 真实 Chrome</option></select></label>
          <label><span>CDP 地址</span><input class="admin-website-cdp-url" value="${escapeHtml(platform.cdp_url || "http://127.0.0.1:9222")}"/></label>
          <label class="wide-field"><span>Chrome 路径（可选）</span><input class="admin-website-chrome-path" value="${escapeHtml(platform.chrome_path || "")}" placeholder="Windows 如 C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"/></label>
          <label class="wide-field"><span>Chrome 用户数据目录（可选）</span><input class="admin-website-chrome-profile" value="${escapeHtml(platform.chrome_user_data_dir || "")}" placeholder="./data/cdp-profiles/doubao"/></label>
          <label class="wide-field"><span>引用信源标识（每行一个）</span><textarea class="admin-website-citations" rows="4" placeholder="例如：引用&#10;来源">${escapeHtml((platform.citation_triggers || []).join("\n"))}</textarea></label>
        </div>
      </section>
      <section class="platform-config-section">
        <div class="section-title-row">
          <div>
            <strong>账号池</strong>
            <span>同一平台内按问题轮换账号，状态只影响当前平台。</span>
          </div>
          <button class="secondary-button admin-add-account" type="button">新增账号</button>
        </div>
        <div class="account-editor-list">${renderBrowserAccountEditors(platform, index)}</div>
      </section>
    </section>
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
  setAdminTab(state.adminTab || "config");
}

function renderBrowserAccountEditors(platform, platformIndex) {
  const accounts = platform.accounts || [];
  if (!accounts.length) return `<div class="empty-state compact">未配置账号池时会使用平台默认 CDP 配置。</div>`;
  return accounts.map((account, accountIndex) => {
    const status = accountStatusFor(platform.platform_id, account.account_id);
    return `
    <article class="account-config-row ${escapeHtml(status.status || "ready")}" data-admin-website="${platformIndex}" data-admin-account="${accountIndex}">
      <header class="account-row-head">
        <div>
          <strong>${escapeHtml(account.account_name || account.account_id || `账号${accountIndex + 1}`)}</strong>
          <span>${escapeHtml(account.account_id || "-")}</span>
        </div>
        <div class="account-row-status">
          <span class="badge ${escapeHtml(status.status || "ready")}">${escapeHtml(status.status || "ready")}</span>
          <label class="checkbox-field"><input class="admin-account-enabled" type="checkbox" ${account.enabled !== false ? "checked" : ""}/> <span>启用</span></label>
        </div>
      </header>
      <div class="account-status-summary">
        <span>最近问题：${escapeHtml(status.question_id || "-")}</span>
        <span>最近使用：${escapeHtml(status.last_used_at || "-")}</span>
        <span>最近成功：${escapeHtml(status.last_success_at || "-")}</span>
        ${status.error_message ? `<strong>${escapeHtml(status.error_message)}</strong>` : ""}
      </div>
      <div class="account-field-grid">
        <label><span>账号 ID</span><input class="admin-account-id" value="${escapeHtml(account.account_id || "")}"/></label>
        <label><span>账号名称</span><input class="admin-account-name" value="${escapeHtml(account.account_name || "")}"/></label>
        <label><span>CDP 地址</span><input class="admin-account-cdp-url" value="${escapeHtml(account.cdp_url || "")}" placeholder="http://127.0.0.1:9222"/></label>
        <label><span>Chrome 路径</span><input class="admin-account-chrome-path" value="${escapeHtml(account.chrome_path || "")}"/></label>
        <label class="wide-field"><span>用户数据目录</span><input class="admin-account-profile" value="${escapeHtml(account.chrome_user_data_dir || "")}" placeholder="./data/cdp-profiles/${escapeHtml(platform.platform_id || "platform")}/${escapeHtml(account.account_id || "account")}"/></label>
      </div>
      <div class="account-actions">
        <button class="secondary-button admin-prepare-login-account" type="button" data-platform-id="${escapeHtml(platform.platform_id)}" data-account-id="${escapeHtml(account.account_id)}">账号登录</button>
        <button class="secondary-button admin-clear-account-status" type="button" data-platform-id="${escapeHtml(platform.platform_id)}" data-account-id="${escapeHtml(account.account_id)}">清除状态</button>
        <button class="secondary-button admin-remove-account" type="button">删除账号</button>
      </div>
    </article>
  `;
  }).join("");
}

function accountStatusFor(platformId, accountId) {
  return (state.accountStatuses || []).find((item) => item.platform_id === platformId && item.account_id === accountId) || {status: "ready"};
}

function setAdminTab(tab) {
  state.adminTab = tab;
  document.querySelectorAll("[data-admin-tab]").forEach((button) => button.classList.toggle("active", button.dataset.adminTab === tab));
  document.querySelectorAll("[data-admin-panel]").forEach((panel) => panel.classList.toggle("hidden", panel.dataset.adminPanel !== tab));
  const saveVisible = state.admin && ["config", "websites", "api"].includes(tab);
  $("saveAdminConfigButton")?.classList.toggle("hidden", !saveVisible);
}

function bindAdminEditors() {
  $("adminBrowserConcurrency").addEventListener("input", (e) => state.config.runner.browser_concurrency = Number(e.target.value || 2));
  $("adminApiConcurrency").addEventListener("input", (e) => state.config.runner.api_concurrency = Number(e.target.value || 5));
  $("adminQuestionCount").addEventListener("input", (e) => state.config.runner.question_count = Number(e.target.value || 15));
  document.querySelectorAll("#adminWebsites > .platform-config-card[data-admin-website]").forEach((card) => {
    const platform = state.config.browser_platforms[Number(card.dataset.adminWebsite)];
    card.querySelector(".admin-website-enabled").addEventListener("change", (e) => platform.enabled = e.target.checked);
    card.querySelector(".admin-website-id").addEventListener("input", (e) => platform.platform_id = e.target.value);
    card.querySelector(".admin-website-name").addEventListener("input", (e) => platform.platform_name = e.target.value);
    card.querySelector(".admin-website-url").addEventListener("input", (e) => platform.url = e.target.value);
    card.querySelector(".admin-website-browser-mode").addEventListener("change", (e) => platform.browser_mode = e.target.value);
    card.querySelector(".admin-website-cdp-url").addEventListener("input", (e) => platform.cdp_url = e.target.value);
    card.querySelector(".admin-website-chrome-path").addEventListener("input", (e) => platform.chrome_path = e.target.value);
    card.querySelector(".admin-website-chrome-profile").addEventListener("input", (e) => platform.chrome_user_data_dir = e.target.value);
    card.querySelector(".admin-website-citations").addEventListener("input", (e) => platform.citation_triggers = e.target.value.split("\n").map((item) => item.trim()).filter(Boolean));
    card.querySelector(".admin-prepare-login").addEventListener("click", () => prepareLogin(platform.platform_id));
    card.querySelector(".admin-add-account").addEventListener("click", () => {
      platform.accounts = platform.accounts || [];
      const next = platform.accounts.length + 1;
      platform.accounts.push({
        account_id: `${platform.platform_id || "platform"}_${next}`,
        account_name: `账号${next}`,
        enabled: true,
        cdp_url: "",
        chrome_user_data_dir: `./data/cdp-profiles/${platform.platform_id || "platform"}/account-${next}`,
      });
      renderAdminConfig();
    });
  });
  document.querySelectorAll("[data-admin-account]").forEach((row) => {
    const platform = state.config.browser_platforms[Number(row.dataset.adminWebsite)];
    const accountIndex = Number(row.dataset.adminAccount);
    const account = platform.accounts[accountIndex];
    row.querySelector(".admin-account-enabled").addEventListener("change", (e) => account.enabled = e.target.checked);
    row.querySelector(".admin-account-id").addEventListener("input", (e) => account.account_id = e.target.value);
    row.querySelector(".admin-account-name").addEventListener("input", (e) => account.account_name = e.target.value);
    row.querySelector(".admin-account-cdp-url").addEventListener("input", (e) => account.cdp_url = e.target.value);
    row.querySelector(".admin-account-chrome-path").addEventListener("input", (e) => account.chrome_path = e.target.value);
    row.querySelector(".admin-account-profile").addEventListener("input", (e) => account.chrome_user_data_dir = e.target.value);
    row.querySelector(".admin-remove-account").addEventListener("click", () => {
      platform.accounts.splice(accountIndex, 1);
      renderAdminConfig();
    });
  });
  document.querySelectorAll(".admin-prepare-login-account").forEach((button) => {
    button.addEventListener("click", () => prepareLogin(button.dataset.platformId, button.dataset.accountId));
  });
  document.querySelectorAll(".admin-clear-account-status").forEach((button) => {
    button.addEventListener("click", () => clearAccountStatus(button.dataset.platformId, button.dataset.accountId));
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
        question_count: Number(state.config.runner?.question_count || 15),
      },
    })});
    $("adminConfigMessage").textContent = "已保存。";
    await loadAdminConfig();
  } catch (error) {
    $("adminConfigMessage").textContent = error.message;
  }
}

async function clearAccountStatus(platformId, accountId) {
  await fetchJson("/api/admin/account-status/clear", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({platform_id: platformId, account_id: accountId}),
  });
  await loadAdminAccountStatuses();
  renderAdminConfig();
}

async function prepareLogin(platformId, accountId = "") {
  const label = accountId ? `${platformId}/${accountId}` : platformId;
  $("adminLoginLog").textContent = `正在打开 ${label} 登录窗口...`;
  await fetchJson("/api/login-prepare", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({platform_id: platformId, account_id: accountId})});
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

function normalizeText(value) {
  return String(value || "").trim().toLowerCase();
}

function hostnameFromUrl(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

init();

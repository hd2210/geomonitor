const state = {
  runs: [],
  currentRunId: null,
  payload: null,
  config: null,
  activeTab: "overview",
  activeView: "results",
  runStatusTimer: null,
};

const $ = (id) => document.getElementById(id);

async function init() {
  bindEvents();
  await loadRuns();
  await refreshRunStatus();
}

function bindEvents() {
  $("refreshButton").addEventListener("click", loadRuns);
  $("questionSearch").addEventListener("input", renderQuestions);
  $("platformFilter").addEventListener("change", renderAnswers);
  $("statusFilter").addEventListener("change", renderAnswers);
  $("closeDialog").addEventListener("click", () => $("answerDialog").close());
  $("configQuestionsText").addEventListener("input", syncQuestionsFromText);
  $("addKeywordButton").addEventListener("click", addKeyword);
  $("saveConfigButton").addEventListener("click", saveConfig);
  $("startRunButton").addEventListener("click", startRun);
  document.querySelectorAll(".primary-nav-item").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => setTab(button.dataset.tab));
  });
}

async function loadRuns() {
  await loadConfig();
  state.runs = await fetchJson("/api/runs");
  renderRuns();
  if (state.runs.length === 0) {
    $("emptyState").classList.remove("hidden");
    $("dashboard").classList.add("hidden");
    if (state.activeView === "results") setPageTitle("结果查看", "暂无运行结果");
    return;
  }
  const selected = state.currentRunId || state.runs[0].run_id;
  await loadRun(selected);
}

async function loadConfig() {
  state.config = await fetchJson("/api/config");
  renderConfig();
}

async function loadRun(runId) {
  state.currentRunId = runId;
  state.payload = await fetchJson(`/api/run?id=${encodeURIComponent(runId)}`);
  $("emptyState").classList.add("hidden");
  $("dashboard").classList.remove("hidden");
  if (state.activeView === "results") setPageTitle("结果查看", runId);
  renderRuns();
  renderMetrics();
  renderOverview();
  renderQuestions();
  renderKeywords();
  renderAnswerFilters();
  renderAnswers();
}

function setView(view) {
  state.activeView = view;
  document.querySelectorAll(".primary-nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  document.querySelectorAll(".view-panel").forEach((panel) => panel.classList.add("hidden"));
  $(`view-${view}`).classList.remove("hidden");
  $("runSidebar").classList.toggle("hidden", view !== "results");

  if (view === "results") {
    setPageTitle("结果查看", state.currentRunId || "选择一次运行结果");
  } else if (view === "config") {
    setPageTitle("配置管理", "问题与关键词");
  } else if (view === "run") {
    setPageTitle("运行任务", "立即执行监测");
    refreshRunStatus();
  }
}

function setPageTitle(eyebrow, title) {
  $("topEyebrow").textContent = eyebrow;
  $("pageTitle").textContent = title;
}

function renderRuns() {
  $("runList").innerHTML = state.runs
    .map((run) => {
      const active = run.run_id === state.currentRunId ? " active" : "";
      return `
        <button class="run-item${active}" data-run-id="${escapeHtml(run.run_id)}">
          <strong>${escapeHtml(run.run_id)}</strong>
          <span>${run.success_count}/${run.answer_count} success · ${run.question_count} questions</span>
          <span>${escapeHtml(run.platforms.join(", ") || "No platforms")}</span>
        </button>
      `;
    })
    .join("");
  document.querySelectorAll(".run-item").forEach((button) => {
    button.addEventListener("click", () => loadRun(button.dataset.runId));
  });
}

function renderMetrics() {
  const answers = state.payload.answers;
  const success = answers.filter((item) => ["success", "partial_success"].includes(item.status)).length;
  const platforms = new Set(answers.map((item) => item.platform_id));
  const keywords = new Set(state.payload.global_summary.map((item) => item.keyword));
  $("metricAnswers").textContent = answers.length;
  $("metricSuccess").textContent = success;
  $("metricPlatforms").textContent = platforms.size;
  $("metricKeywords").textContent = keywords.size;
}

function renderOverview() {
  $("globalSummary").innerHTML = state.payload.global_summary
    .map((row) => {
      const rate = numeric(row.appearance_rate);
      return `
        <div class="summary-row">
          <strong>${escapeHtml(row.keyword)}</strong>
          <div class="bar"><div class="bar-fill" style="width:${Math.max(0, Math.min(100, rate * 100))}%"></div></div>
          <span>${formatPercent(rate)}</span>
        </div>
      `;
    })
    .join("");

  $("platformSummary").innerHTML = state.payload.platform_summary
    .map(
      (row) => `
        <tr>
          <td>${escapeHtml(row.platform_id)}</td>
          <td>${escapeHtml(row.keyword)}</td>
          <td>${escapeHtml(row.appeared_count)}/${escapeHtml(row.total_questions)}</td>
          <td>${formatPercent(numeric(row.appearance_rate))}</td>
          <td>${escapeHtml(row.avg_rank || "-")}</td>
          <td>${escapeHtml(row.best_rank || "-")}</td>
        </tr>
      `,
    )
    .join("");
}

function renderQuestions() {
  const query = $("questionSearch").value.trim().toLowerCase();
  const questions = uniqueBy(state.payload.answers, (item) => item.question_id)
    .filter((item) => !query || `${item.question_id} ${item.question}`.toLowerCase().includes(query))
    .sort((a, b) => a.question_id.localeCompare(b.question_id));

  $("questionList").innerHTML = questions
    .map((question) => {
      const rows = state.payload.answers.filter((item) => item.question_id === question.question_id);
      const success = rows.filter((item) => ["success", "partial_success"].includes(item.status)).length;
      return `
        <div class="question-card">
          <strong>${escapeHtml(question.question_id)} · ${success}/${rows.length} success</strong>
          <p>${escapeHtml(question.question)}</p>
        </div>
      `;
    })
    .join("");
}

function renderKeywords() {
  $("keywordGrid").innerHTML = state.payload.global_summary
    .map((row) => {
      const rate = numeric(row.appearance_rate);
      return `
        <div class="keyword-card">
          <strong>${escapeHtml(row.keyword)}</strong>
          <span class="rate">${formatPercent(rate)}</span>
          <div class="bar"><div class="bar-fill" style="width:${rate * 100}%"></div></div>
          <span class="eyebrow">${escapeHtml(row.appeared_count)} / ${escapeHtml(row.total_answers)} appeared · avg rank ${escapeHtml(row.avg_rank || "-")}</span>
        </div>
      `;
    })
    .join("");
}

function renderConfig() {
  if (!state.config) return;
  $("configPath").textContent = state.config.config_path;
  $("configQuestionCount").textContent = `${state.config.questions.length} 个问题`;
  $("configKeywordCount").textContent = `${state.config.target_keywords.length} 个关键词`;
  $("configQuestionsText").value = state.config.questions.map((item) => item.question).join("\n");
  $("configKeywords").innerHTML = state.config.target_keywords.map(renderKeywordEditor).join("");
  bindConfigEditors();
}

function renderKeywordEditor(keyword, index) {
  return `
    <div class="config-card" data-keyword-index="${index}">
      <div class="field-grid">
        <label>
          <span>Keyword</span>
          <input class="config-keyword-name" value="${escapeHtml(keyword.keyword)}" />
        </label>
        <button class="danger-button remove-keyword" title="删除关键词">删除</button>
      </div>
      <label>
        <span>Aliases，用英文逗号分隔</span>
        <textarea class="config-keyword-aliases" rows="3">${escapeHtml((keyword.aliases || []).join(", "))}</textarea>
      </label>
    </div>
  `;
}

function bindConfigEditors() {
  document.querySelectorAll(".remove-keyword").forEach((button) => {
    button.addEventListener("click", () => {
      const index = Number(button.closest("[data-keyword-index]").dataset.keywordIndex);
      state.config.target_keywords.splice(index, 1);
      renderConfig();
    });
  });
  document.querySelectorAll("[data-keyword-index]").forEach((card) => {
    const index = Number(card.dataset.keywordIndex);
    card.querySelector(".config-keyword-name").addEventListener("input", (event) => {
      state.config.target_keywords[index].keyword = event.target.value;
    });
    card.querySelector(".config-keyword-aliases").addEventListener("input", (event) => {
      state.config.target_keywords[index].aliases = splitAliases(event.target.value);
    });
  });
}

function addKeyword() {
  state.config.target_keywords.push({ keyword: "", aliases: [] });
  renderConfig();
}

function syncQuestionsFromText() {
  const questions = questionsFromText($("configQuestionsText").value);
  state.config.questions = questions;
  $("configQuestionCount").textContent = `${questions.length} 个问题`;
}

async function saveConfig() {
  const payload = collectConfigPayload();
  $("configStatus").textContent = "正在保存...";
  $("saveConfigButton").disabled = true;
  try {
    await fetchJson("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    await loadConfig();
    $("configStatus").textContent = "配置已保存。下一次运行会使用新的问题和关键词。";
  } catch (error) {
    $("configStatus").textContent = `保存失败：${error.message}`;
  } finally {
    $("saveConfigButton").disabled = false;
  }
}

function collectConfigPayload() {
  return {
    questions: state.config.questions.map((item) => ({
      question_id: item.question_id.trim(),
      question: item.question.trim(),
    })),
    target_keywords: state.config.target_keywords.map((item) => ({
      keyword: item.keyword.trim(),
      aliases: item.aliases || [],
    })),
  };
}

function questionsFromText(text) {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((question, index) => ({
      question_id: `Q${String(index + 1).padStart(3, "0")}`,
      question,
    }));
}

async function startRun() {
  $("startRunButton").disabled = true;
  $("runLog").textContent = "正在启动...";
  try {
    await fetchJson("/api/run-now", { method: "POST" });
    startRunPolling();
  } catch (error) {
    $("runLog").textContent = `启动失败：${error.message}`;
    $("startRunButton").disabled = false;
  }
}

function startRunPolling() {
  if (state.runStatusTimer) clearInterval(state.runStatusTimer);
  refreshRunStatus();
  state.runStatusTimer = setInterval(refreshRunStatus, 1500);
}

async function refreshRunStatus() {
  const status = await fetchJson("/api/run-status");
  $("runStatusText").textContent = status.running ? "Running" : "Idle";
  $("runExitCode").textContent = status.return_code ?? "-";
  $("runLog").textContent = status.lines.length ? status.lines.join("\n") : "尚未启动运行。";
  $("startRunButton").disabled = status.running;
  if (!status.running && state.runStatusTimer) {
    clearInterval(state.runStatusTimer);
    state.runStatusTimer = null;
    await loadRuns();
  }
}

function renderAnswerFilters() {
  const platforms = [...new Set(state.payload.answers.map((item) => item.platform_id))].sort();
  $("platformFilter").innerHTML = '<option value="">全部平台</option>' + platforms.map((p) => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join("");
}

function renderAnswers() {
  const platform = $("platformFilter").value;
  const status = $("statusFilter").value;
  const answers = state.payload.answers.filter((item) => (!platform || item.platform_id === platform) && (!status || item.status === status));

  $("answerList").innerHTML = answers
    .map((answer, index) => {
      const analysis = findAnalysis(answer);
      const appeared = analysis.keyword_analysis.filter((item) => item.appeared).map((item) => `${item.keyword}${item.rank ? ` #${item.rank}` : ""}`);
      return `
        <div class="answer-card">
          <div>
            <div class="badge-row">
              <span class="badge ${escapeHtml(answer.status)}">${escapeHtml(answer.status)}</span>
              <span class="badge">${escapeHtml(answer.platform_id)}</span>
              <span class="badge">${escapeHtml(answer.question_id)}</span>
            </div>
            <strong>${escapeHtml(answer.question)}</strong>
            <p>${escapeHtml(answer.error_message || truncate(answer.answer_text || "No answer text", 180))}</p>
            <p>${appeared.length ? `Appeared: ${escapeHtml(appeared.join(", "))}` : "No monitored keyword appeared"}</p>
          </div>
          <button data-answer-index="${index}">查看详情</button>
        </div>
      `;
    })
    .join("");

  document.querySelectorAll("[data-answer-index]").forEach((button) => {
    button.addEventListener("click", () => openAnswer(answers[Number(button.dataset.answerIndex)]));
  });
}

function openAnswer(answer) {
  const analysis = findAnalysis(answer);
  $("dialogMeta").textContent = `${answer.platform_id} · ${answer.question_id} · ${answer.status}`;
  $("dialogTitle").textContent = answer.question;
  $("dialogAnswer").textContent = answer.answer_text || answer.error_message || "No answer text saved.";
  $("dialogKeywords").innerHTML = analysis.keyword_analysis
    .map((item) => {
      const cls = item.appeared ? "success" : "";
      const label = item.appeared ? `${item.keyword} · rank ${item.rank || "-"}` : `${item.keyword} · missed`;
      return `<span class="badge ${cls}">${escapeHtml(label)}</span>`;
    })
    .join("");
  if (answer.screenshot_path) {
    const src = `/runs/${encodeURIComponent(state.currentRunId)}/${answer.screenshot_path}`;
    $("dialogScreenshot").innerHTML = `<a class="screenshot-link" href="${src}" target="_blank" rel="noreferrer">打开截图原图</a><img src="${src}" alt="Answer screenshot" />`;
  } else {
    $("dialogScreenshot").innerHTML = '<span class="eyebrow">No screenshot saved</span>';
  }
  $("answerDialog").showModal();
}

function findAnalysis(answer) {
  return (
    state.payload.analyses.find((item) => item.platform_id === answer.platform_id && item.question_id === answer.question_id) || {
      keyword_analysis: [],
    }
  );
}

function setTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".tab").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.add("hidden"));
  $(`tab-${tab}`).classList.remove("hidden");
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const message = await response.text();
    throw new Error(message.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim() || `Request failed: ${response.status}`);
  }
  return response.json();
}

function splitAliases(text) {
  const seen = new Set();
  const aliases = [];
  for (const alias of text.split(",")) {
    const value = alias.trim();
    const key = value.toLowerCase();
    if (value && !seen.has(key)) {
      seen.add(key);
      aliases.push(value);
    }
  }
  return aliases;
}

function numeric(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function formatPercent(value) {
  return `${Math.round(value * 1000) / 10}%`;
}

function truncate(text, length) {
  if (text.length <= length) return text;
  return `${text.slice(0, length - 1)}…`;
}

function uniqueBy(items, keyFn) {
  const seen = new Set();
  const result = [];
  for (const item of items) {
    const key = keyFn(item);
    if (!seen.has(key)) {
      seen.add(key);
      result.push(item);
    }
  }
  return result;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

init().catch((error) => {
  console.error(error);
  $("emptyState").classList.remove("hidden");
  $("emptyState").innerHTML = `<h3>加载失败</h3><p>${escapeHtml(error.message)}</p>`;
});

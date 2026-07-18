/**
 * DemoCursor Agent 前端逻辑
 *
 * 与后端的两个连接：
 *   POST /api/chat    发送消息，同步等待回复
 *   GET  /api/events  SSE 订阅 Agent Loop 事件流（右侧面板实时渲染）
 *   GET  /api/status  轮询后端状态（顶栏徽标 + 连接指示灯）
 */
"use strict";

const API = "http://127.0.0.1:8000";

// ---------- DOM ----------
const chatList = document.getElementById("chat-list");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const btnSend = document.getElementById("btn-send");
const loopList = document.getElementById("loop-list");
const connDot = document.getElementById("conn-dot");

// ---------- 工具 ----------
function escapeHtml(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function scrollToBottom(el) { el.scrollTop = el.scrollHeight; }

function clearPlaceholder(container, cls) {
  const ph = container.querySelector("." + cls);
  if (ph) ph.remove();
}

// ---------- 对话区 ----------
function addMessage(role, text) {
  clearPlaceholder(chatList, "chat-empty");
  const div = document.createElement("div");
  div.className = "msg msg-" + role;
  div.textContent = text;
  chatList.appendChild(div);
  scrollToBottom(chatList);
  return div;
}

let thinkingEl = null;
function showThinking() {
  thinkingEl = document.createElement("div");
  thinkingEl.className = "msg msg-thinking";
  thinkingEl.textContent = "思考中…（右侧可观察执行步骤）";
  chatList.appendChild(thinkingEl);
  scrollToBottom(chatList);
}
function hideThinking() { if (thinkingEl) { thinkingEl.remove(); thinkingEl = null; } }

async function sendChat(text) {
  btnSend.disabled = true;
  showThinking();
  try {
    const resp = await fetch(API + "/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, chat_id: "web", user_id: "web-user" }),
    });
    const data = await resp.json();
    hideThinking();
    if (data.error) addMessage("error", "出错了：" + data.error);
    else addMessage("ai", data.reply || "（空回复）");
  } catch (e) {
    hideThinking();
    addMessage("error", "无法连接后端：" + e.message + "\n请先运行 python main.py");
  } finally {
    btnSend.disabled = false;
    chatInput.focus();
  }
}

chatForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = chatInput.value.trim();
  if (!text) return;
  chatInput.value = "";
  addMessage("user", text);
  sendChat(text);
});

document.getElementById("btn-new").addEventListener("click", async () => {
  try {
    await fetch(API + "/api/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: "web", user_id: "web-user" }),
    });
    addMessage("ai", "已新开会话，之前的上下文已清空。");
  } catch (e) {
    addMessage("error", "重开会话失败：" + e.message);
  }
});

// ---------- Loop 可视化 ----------
const STEP_META = {
  turn_start:       { icon: "▶", cls: "",      title: "开始一轮对话" },
  llm_request:      { icon: "↑", cls: "",      title: "调用大模型" },
  llm_response:     { icon: "↓", cls: "",      title: "模型返回" },
  tool_call_start:  { icon: "🔧", cls: "",     title: "调用工具" },
  tool_call_result: { icon: "✔", cls: "step-ok",  title: "工具结果" },
  turn_end:         { icon: "🏁", cls: "step-ok", title: "本轮完成" },
  turn_error:       { icon: "⚠", cls: "step-err", title: "本轮出错" },
  outbound_reply:   { icon: "📤", cls: "",      title: "回复已投递" },
};

// turn_id → <details> 元素，事件按轮次分组
const turnCards = new Map();

function getTurnCard(turnId, firstEvent) {
  if (turnCards.has(turnId)) return turnCards.get(turnId);
  clearPlaceholder(loopList, "loop-empty");
  const card = document.createElement("details");
  card.className = "turn-card";
  card.open = true; // 进行中的轮次默认展开
  const summary = document.createElement("summary");
  const time = new Date((firstEvent.created_at || Date.now() / 1000) * 1000)
    .toLocaleTimeString("zh-CN", { hour12: false });
  summary.innerHTML = `<span>轮次 ${escapeHtml(turnId.slice(0, 6))}</span><span>${time}</span>`;
  const steps = document.createElement("div");
  steps.className = "turn-steps";
  card.appendChild(summary);
  card.appendChild(steps);
  loopList.appendChild(card);
  turnCards.set(turnId, card);
  // 最多保留 30 轮，防止长会话撑爆 DOM
  if (turnCards.size > 30) {
    const oldest = turnCards.keys().next().value;
    turnCards.get(oldest).remove();
    turnCards.delete(oldest);
  }
  return card;
}

function renderStep(event) {
  const meta = STEP_META[event.type] || { icon: "•", cls: "", title: event.type };
  const d = event.data || {};
  let detail = "";

  switch (event.type) {
    case "turn_start":
      detail = d.user_text || "";
      break;
    case "llm_request":
      detail = `第 ${d.iteration} 次迭代 · 上下文 ${d.message_count} 条消息 / ${d.total_chars} 字符`;
      break;
    case "llm_response": {
      const tcs = (d.tool_calls || []).map(
        (t) => `→ ${t.name}(${JSON.stringify(t.arguments)})`
      );
      detail = [d.text_preview, ...tcs].filter(Boolean).join("\n");
      break;
    }
    case "tool_call_start":
      detail = `${d.name}(${JSON.stringify(d.arguments)})`;
      break;
    case "tool_call_result":
      detail = d.ok
        ? `${d.result_preview || ""}  [${d.duration_ms}ms]`
        : `失败: ${d.error}`;
      break;
    case "turn_end":
      detail = `迭代 ${d.iterations} 次`;
      break;
    case "turn_error":
      detail = d.error || "";
      break;
    case "outbound_reply":
      detail = d.text_preview || "";
      break;
  }

  const step = document.createElement("div");
  step.className = "step " + (d.ok === false ? "step-err" : meta.cls);
  step.innerHTML =
    `<div class="step-icon">${meta.icon}</div>` +
    `<div class="step-body"><div class="step-title">${meta.title}</div>` +
    (detail ? `<div class="step-detail">${escapeHtml(detail)}</div>` : "") +
    `</div>`;

  const card = getTurnCard(event.turn_id || "misc", event);
  card.querySelector(".turn-steps").appendChild(step);
  if (event.type === "turn_end" || event.type === "turn_error") card.open = true;
  scrollToBottom(loopList);
}

// ---------- SSE 事件流 ----------
function connectEvents() {
  const es = new EventSource(API + "/api/events");
  es.onmessage = (e) => {
    try { renderStep(JSON.parse(e.data)); } catch { /* 忽略坏事件 */ }
  };
  es.onerror = () => { /* EventSource 自动重连，无需处理 */ };
}

document.getElementById("btn-clear-events").addEventListener("click", () => {
  loopList.innerHTML = "";
  turnCards.clear();
});

// ---------- 状态轮询 ----------
async function pollStatus() {
  try {
    const resp = await fetch(API + "/api/status");
    const s = await resp.json();
    connDot.className = "dot dot-on";
    connDot.title = "后端已连接";
    document.getElementById("model-badge").textContent = "model: " + (s.model || "-");
    document.getElementById("tools-badge").textContent = "tools: " + (s.tools || []).join(", ");
    document.getElementById("skills-badge").textContent = "skills: " + (s.skills || []).join(", ");
  } catch {
    connDot.className = "dot dot-off";
    connDot.title = "后端未连接（python main.py 启动）";
  }
}

connectEvents();
pollStatus();
setInterval(pollStatus, 5000);
chatInput.focus();

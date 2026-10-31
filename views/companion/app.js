/* 面板前端：只依赖宿主注入的 window.AstrBotPluginView，不引第三方库。 */
(function () {
  "use strict";

  var bridge = window.AstrBotPluginView || window.AstrBotPluginPage || null;
  var API = "";

  var state = {
    scope: "",
    tab: "facts",
    facts: [],
    picked: new Set(),
    pendingDelete: null,
    kind: "",
    logLevel: "",
  };

  var TRAITS = {
    intimacy: "亲密度",
    playfulness: "玩心",
    directness: "直率",
    initiative: "主动",
    warmth: "温度",
  };

  function $(id) { return document.getElementById(id); }

  function esc(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function api(path, params) {
    if (bridge && bridge.apiGet) return bridge.apiGet(API + path, params || {});
    var query = new URLSearchParams(params || {}).toString();
    return fetch(API + path + (query ? "?" + query : "")).then(function (r) { return r.json(); });
  }

  function post(path, body) {
    if (bridge && bridge.apiPost) return bridge.apiPost(API + path, body || {});
    return fetch(API + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (r) { return r.json(); });
  }

  function toast(text) {
    var box = $("toast");
    box.textContent = text;
    box.hidden = false;
    clearTimeout(box._timer);
    box._timer = setTimeout(function () { box.hidden = true; }, 2200);
  }

  function timeText(stamp) {
    if (!stamp) return "—";
    var date = new Date(stamp * 1000);
    var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
    return date.getMonth() + 1 + "/" + date.getDate() + " " +
      pad(date.getHours()) + ":" + pad(date.getMinutes());
  }

  // ---------- 会话 ----------

  function loadScopes() {
    return api("scopes").then(function (data) {
      var items = (data && data.items) || [];
      var select = $("scope");
      if (!items.length) {
        select.innerHTML = "<option>还没有会话</option>";
        $("hero-sub").textContent = "先和机器人聊几句，这里就有数据了";
        return;
      }
      select.innerHTML = items.map(function (item) {
        return '<option value="' + esc(item.scope) + '">' +
          esc(item.label) + "（" + item.total + "）</option>";
      }).join("");
      state.scope = state.scope || items[0].scope;
      select.value = state.scope;
      var current = items.filter(function (i) { return i.scope === state.scope; })[0];
      $("hero-sub").textContent = current
        ? current.label + " · " + current.total + " 条有效记忆"
        : "";
    });
  }

  function loadStats() {
    return api("overview", { scope: state.scope }).then(function (data) {
      var items = (data && data.items) || [];
      $("stats").innerHTML = items.map(function (item) {
        return '<div class="stat' + (item.warn ? " warn" : "") + '">' +
          "<b>" + esc(item.value) + "</b><span>" + esc(item.label) + "</span></div>";
      }).join("");
    });
  }

  // ---------- 记忆 ----------

  function loadFacts() {
    var params = { scope: state.scope, limit: 100 };
    var q = $("q").value.trim();
    if (q) params.q = q;
    if ($("status").value) params.status = $("status").value;
    if (state.kind) params.kind = state.kind;
    return api("facts", params).then(function (data) {
      state.facts = (data && data.items) || [];
      state.picked.clear();
      renderFacts();
    });
  }

  function renderKinds() {
    var kinds = [];
    state.facts.forEach(function (fact) {
      if (kinds.indexOf(fact.kind) < 0) kinds.push(fact.kind);
    });
    var labels = { preference: "偏好", profile: "身份", fact: "事实", event: "事件", task: "在办" };
    $("kind-chips").innerHTML = ['<button class="chip' + (state.kind ? "" : " on") +
      '" data-kind="">全部</button>'].concat(kinds.map(function (kind) {
      return '<button class="chip' + (state.kind === kind ? " on" : "") + '" data-kind="' +
        esc(kind) + '">' + esc(labels[kind] || kind) + "</button>";
    })).join("");
  }

  function renderFacts() {
    renderKinds();
    var list = $("facts");
    $("facts-empty").hidden = state.facts.length > 0;
    list.innerHTML = state.facts.map(function (fact) {
      var picked = state.picked.has(fact.id);
      return '<li class="fact' + (picked ? " picked" : "") + '" data-id="' + fact.id + '">' +
        '<input type="checkbox" ' + (picked ? "checked" : "") + ' data-pick="' + fact.id + '" />' +
        '<div class="fact-body">' +
          '<div class="fact-line"><span class="slot-chip">' + esc(fact.slot) + "</span>" +
          '<span class="content">' + esc(fact.content) + "</span></div>" +
          '<div class="fact-meta"><span>#' + fact.id + "</span><span>重要度 " +
          fact.importance + "</span><span>置信 " + fact.confidence.toFixed(2) +
          "</span><span>召回 " + fact.hits + " 次</span><span>" +
          timeText(fact.updated_at) + "</span>" +
          (fact.has_vector ? "<span>有向量</span>" : "<span>无向量</span>") + "</div>" +
        "</div>" +
        '<div class="fact-side"><span class="pill ' + esc(fact.status) + '">' +
        statusText(fact.status) + "</span>" +
        '<span class="row-actions">' +
          (fact.status === "suspect"
            ? '<button class="row-btn" data-act="confirm" data-id="' + fact.id + '">确认</button>'
            : "") +
          '<button class="row-btn danger" data-act="delete" data-id="' + fact.id + '">删除</button>' +
        "</span></div></li>";
    }).join("");
    syncBulk();
  }

  function statusText(status) {
    return { active: "生效中", suspect: "待确认", superseded: "已被取代" }[status] || status;
  }

  function syncBulk() {
    var count = state.picked.size;
    $("bulkbar").hidden = count === 0;
    $("bulk-count").textContent = "已选 " + count + " 条";
    document.querySelectorAll(".fact").forEach(function (row) {
      var id = Number(row.dataset.id);
      row.classList.toggle("picked", state.picked.has(id));
      var box = row.querySelector("input[data-pick]");
      if (box) box.checked = state.picked.has(id);
    });
  }

  function act(ids, kind) {
    if (!ids.length) return;
    var path = kind === "delete" ? "facts/delete" : "facts/confirm";
    post(path, { scope: state.scope, ids: ids }).then(function (data) {
      var payload = (data && data.data) || data || {};
      if (payload.ok === false) {
        toast(payload.error || "操作失败");
        return;
      }
      toast(kind === "delete"
        ? "删除 " + (payload.removed || 0) + " 条"
        : "确认 " + (payload.changed || 0) + " 条");
      state.picked.clear();
      refreshAll();
    });
  }

  // ---------- 人格 ----------

  function loadPersona() {
    return api("persona", { scope: state.scope }).then(function (data) {
      var info = (data && data.data) || data || {};
      var traits = info.traits || {};
      var baseline = info.baseline || {};
      $("persona-summary").textContent = info.version
        ? "今日已用位移 " + (info.day_used * 100).toFixed(1) + "% / " +
          (info.day_budget * 100).toFixed(0) + "%，上次演化 " + timeText(info.last_evolve_at)
        : "还没有演化过，人格仍是初始基线。";
      $("traits").innerHTML = Object.keys(TRAITS).map(function (key) {
        var value = traits[key] == null ? 0.5 : traits[key];
        var base = baseline[key] == null ? 0.5 : baseline[key];
        var diff = Math.round((value - base) * 100);
        return '<div class="trait"><div class="trait-head"><span>' + TRAITS[key] +
          "</span><b>" + Math.round(value * 100) + "</b></div>" +
          '<div class="meter"><i style="width:' + Math.round(value * 100) + '%"></i>' +
          '<u style="left:' + Math.round(base * 100) + '%"></u></div>' +
          "<small>基线 " + Math.round(base * 100) + " · 偏离 " +
          (diff >= 0 ? "+" : "") + diff + "</small></div>";
      }).join("");
      renderPersonaLog(info.log || []);
    });
  }

  function renderPersonaLog(rows) {
    if (!rows.length) {
      $("persona-log").innerHTML = '<li class="hint">还没有记录。</li>';
      return;
    }
    $("persona-log").innerHTML = rows.map(function (row) {
      var chips = Object.keys(row.deltas || {}).map(function (key) {
        var value = row.deltas[key];
        if (Math.abs(value) < 0.001) return "";
        return '<span class="delta' + (value < 0 ? " down" : "") + '">' +
          (TRAITS[key] || key) + " " + (value > 0 ? "+" : "") +
          Math.round(value * 100) + "</span>";
      }).join("");
      return "<li><div><b>v" + row.version + "</b> " + esc(row.reason) +
        '<span class="when">' + timeText(row.created_at) + "</span></div>" +
        '<div class="deltas">' + (chips || '<span class="delta">没有位移</span>') +
        "</div></li>";
    }).join("");
  }

  // ---------- 主动消息 ----------

  function loadCare() {
    return api("care", { scope: state.scope }).then(function (data) {
      var info = (data && data.data) || data || {};
      var ready = info.reason === "ready" || info.reason === "forced";
      var hero = $("care-hero");
      hero.className = "care-hero " + (ready ? "ready" : "wait");
      $("care-pill").textContent = ready ? "可以发" : "暂不发";
      $("care-reason").textContent = reasonText(info.reason);
      $("care-toggle").checked = !!info.enabled;
      $("care-grid").innerHTML = (info.gates || []).map(function (gate) {
        return '<div class="gate ' + (gate.ok ? "ok" : "no") + '"><span>' +
          (gate.ok ? "满足" : "未满足") + "</span>" + esc(gate.detail) + "</div>";
      }).join("") +
        '<div class="gate"><span>今日已发</span>' + info.sent_today + " 条</div>" +
        '<div class="gate"><span>未回应</span>' + info.unanswered + " 条</div>";
      $("care-last").textContent = info.last_text
        ? "上次主动：" + info.last_text
        : "还没主动说过话。";
    });
  }

  function reasonText(reason) {
    return {
      disabled: "总开关没开",
      muted: "这个会话关了",
      quiet_hours: "免打扰时段",
      outside_schedule: "不在允许时段",
      too_soon: "还不到时候",
      daily_cap: "今天发够了",
      unanswered_cap: "连发没回，等你先说话",
      probability: "这轮概率没抽中",
      no_prompt: "没攒出话题",
      forced: "手动触发",
      ready: "可以发",
    }[reason] || reason || "—";
  }

  // ---------- 日志 ----------

  function loadLogs() {
    var params = { limit: 80 };
    var q = $("log-q").value.trim();
    if (q) params.q = q;
    if (state.logLevel) params.level = state.logLevel;
    return api("logs", params).then(function (data) {
      var info = (data && data.data) || data || {};
      var items = info.items || [];
      var stats = info.stats || {};
      $("log-stats").innerHTML = [
        "共 " + (stats.total || 0) + " 条",
        "ERROR " + ((stats.counts || {}).ERROR || 0),
        "WARN " + ((stats.counts || {}).WARN || 0),
        "大小 " + Math.round((stats.bytes || 0) / 1024) + " KB",
        "保留 " + (stats.days || 7) + " 天",
      ].map(function (text) { return "<span>" + text + "</span>"; }).join("");
      $("log-empty").hidden = items.length > 0;
      $("log-list").innerHTML = items.map(function (row) {
        var extra = Object.keys(row).filter(function (key) {
          return ["ts", "time", "level", "event", "msg"].indexOf(key) < 0;
        }).map(function (key) {
          var value = row[key];
          if (value && typeof value === "object") value = JSON.stringify(value);
          return key + "=" + value;
        }).join(" ");
        return '<li class="log">' +
          '<span class="log-time">' + esc(String(row.time || "").slice(11)) + "</span>" +
          '<span class="log-level ' + esc(row.level) + '">' + esc(row.level) + "</span>" +
          '<span class="log-evt">' + esc(row.event) + "</span>" +
          '<span class="log-msg" title="' + esc(row.msg) + '">' + esc(row.msg) + "</span>" +
          '<span class="log-fields">' + esc(extra) + "</span></li>";
      }).join("");
    });
  }

  function renderLogLevels() {
    var levels = [["", "全部"], ["DEBUG", "DEBUG"], ["INFO", "INFO"], ["WARN", "WARN"], ["ERROR", "ERROR"]];
    $("log-levels").innerHTML = levels.map(function (pair) {
      return '<button class="chip' + (state.logLevel === pair[0] ? " on" : "") +
        '" data-level="' + pair[0] + '">' + pair[1] + "</button>";
    }).join("");
  }

  // ---------- 汇总 ----------

  function refreshAll() {
    if (!state.scope) return Promise.resolve();
    var jobs = [loadStats()];
    if (state.tab === "facts") jobs.push(loadFacts());
    if (state.tab === "persona") jobs.push(loadPersona());
    if (state.tab === "care") jobs.push(loadCare());
    if (state.tab === "logs") jobs.push(loadLogs());
    return Promise.all(jobs).catch(function (error) {
      toast("加载失败：" + error);
    });
  }

  function switchTab(tab) {
    state.tab = tab;
    document.querySelectorAll(".tab").forEach(function (button) {
      button.classList.toggle("on", button.dataset.tab === tab);
    });
    ["facts", "persona", "care", "logs"].forEach(function (name) {
      $("panel-" + name).hidden = name !== tab;
    });
    refreshAll();
  }

  // ---------- 事件 ----------

  function bind() {
    $("scope").addEventListener("change", function (event) {
      state.scope = event.target.value;
      state.picked.clear();
      loadScopes().then(refreshAll);
    });
    $("reload").addEventListener("click", function () { refreshAll(); toast("已刷新"); });
    $("tabs").addEventListener("click", function (event) {
      var tab = event.target.dataset && event.target.dataset.tab;
      if (tab) switchTab(tab);
    });
    $("refresh-facts").addEventListener("click", loadFacts);
    $("q").addEventListener("keydown", function (event) {
      if (event.key === "Enter") loadFacts();
    });
    $("status").addEventListener("change", loadFacts);
    $("kind-chips").addEventListener("click", function (event) {
      var kind = event.target.dataset && event.target.dataset.kind;
      if (kind === undefined) return;
      state.kind = kind;
      loadFacts();
    });

    $("facts").addEventListener("click", function (event) {
      var target = event.target;
      if (target.dataset && target.dataset.pick) {
        var id = Number(target.dataset.pick);
        if (target.checked) state.picked.add(id); else state.picked.delete(id);
        syncBulk();
        return;
      }
      var act_ = target.dataset && target.dataset.act;
      if (!act_) return;
      act([Number(target.dataset.id)], act_);
    });

    $("bulk-delete").addEventListener("click", function () {
      act(Array.from(state.picked), "delete");
    });
    $("bulk-confirm").addEventListener("click", function () {
      act(Array.from(state.picked), "confirm");
    });
    $("bulk-clear").addEventListener("click", function () {
      state.picked.clear();
      syncBulk();
    });

    $("care-toggle").addEventListener("change", function (event) {
      post("care", { scope: state.scope, enabled: event.target.checked }).then(function () {
        toast(event.target.checked ? "已打开这个会话的主动消息" : "已关闭");
        loadCare();
      });
    });

    $("log-q").addEventListener("keydown", function (event) {
      if (event.key === "Enter") loadLogs();
    });
    $("log-levels").addEventListener("click", function (event) {
      var level = event.target.dataset && event.target.dataset.level;
      if (level === undefined) return;
      state.logLevel = level;
      renderLogLevels();
      loadLogs();
    });
    $("log-clear").addEventListener("click", function () {
      post("logs/clear", {}).then(function () {
        toast("日志已清空");
        loadLogs();
      });
    });
  }

  function start() {
    bind();
    renderLogLevels();
    var ready = bridge && bridge.ready ? bridge.ready() : Promise.resolve({});
    ready.then(function (info) {
      if (info && info.isDark) document.documentElement.dataset.theme = "dark";
      if (info && info.pluginName) API = "";
      return loadScopes();
    }).then(function () {
      return refreshAll();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();

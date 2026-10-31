/* 面板前端：只依赖宿主注入的 window.AstrBotPluginView，不引第三方库。 */
window.__companionBuild = "0.1.10";

(function () {
  "use strict";

  // 桥脚本是宿主在 </body> 前追加的，执行顺序在本文件之后——所以这里不能提前
  // 取一次就定死。第一次拿不到就一直是 null，面板于是走 fetch 兜底，在受限 iframe
  // 里抛 "Failed to fetch"，页面全空。
  var bridgeRef = null;

  function bridge() {
    if (!bridgeRef) {
      bridgeRef = window.AstrBotPluginView || window.AstrBotPluginPage || null;
    }
    return bridgeRef;
  }

  function waitForBridge(timeoutMs) {
    return new Promise(function (resolve) {
      var deadline = Date.now() + (timeoutMs || 3000);
      (function poll() {
        if (bridge()) return resolve(bridge());
        if (Date.now() > deadline) return resolve(null);
        setTimeout(poll, 50);
      })();
    });
  }
  var API = "";
  var PLUGIN = "astrbot_plugin_companion";

  var state = {
    scope: "",
    tab: "facts",
    facts: [],
    picked: new Set(),
    pendingDelete: null,
    kind: "",
    logLevel: "",
    settings: null,
    draft: {},
    fieldErrors: {},
  };

  // 数值项的单位，让「90」不至于被当成 90 秒
  var UNIT = {
    top_k: "条",
    flush_turns: "轮",
    idle_seconds: "秒",
    max_buffer: "条",
    max_inject_chars: "字",
    max_item_chars: "字",
    max_per_slot: "条",
    llm_timeout: "秒",
    embedding_timeout: "秒",
    persona_max_chars: "字",
    care_min_idle_minutes: "分钟",
    care_daily_max: "条/天",
    care_max_unanswered: "条",
    care_max_chars: "字",
    care_split_max: "条",
    care_split_delay: "秒",
    log_days: "天",
    persona_half_life_days: "天",
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

  // 后端 handler 出错时会回 {status:"error", message}（不是 500，500 会让 bridge
  // reject 成一个没有文案的错误，页面就永远停在「正在读取…」）。这里统一把它
  // 抛成 Error，好让调用处能把原因写进 hero。
  function unwrap(data) {
    if (data && data.status === "error") {
      throw new Error(data.message || "接口返回错误");
    }
    return data;
  }

  function api(path, params) {
    var b = bridge();
    var call = b && b.apiGet
      ? b.apiGet(API + path, params || {})
      : (function () {
          var query = new URLSearchParams(params || {}).toString();
          var url = "/api/v1/plugins/extensions/" + PLUGIN + "/" + path;
          return fetch(url + (query ? "?" + query : "")).then(function (r) {
            return r.json();
          });
        })();
    return call.then(unwrap);
  }

  function post(path, body) {
    var b = bridge();
    var call = b && b.apiPost
      ? b.apiPost(API + path, body || {})
      : fetch("/api/v1/plugins/extensions/" + PLUGIN + "/" + path, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body || {}),
        }).then(function (r) { return r.json(); });
    return call.then(unwrap);
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
          '<div class="fact-line"><span class="slot-chip">' +
          esc(fact.kind === "task" ? "任务" : fact.slot) + "</span>" +
          '<span class="content">' + esc(fact.content) + "</span></div>" +
          '<div class="fact-meta"><span>#' + fact.id + "</span><span>重要度 " +
          fact.importance + "</span><span>置信 " + fact.confidence.toFixed(2) +
          "</span><span>召回 " + fact.hits + " 次</span><span>" +
          timeText(fact.status === "done" && fact.closed_at ? fact.closed_at : fact.updated_at) +
          "</span>" +
          (fact.has_vector ? "<span>有向量</span>" : "<span>无向量</span>") + "</div>" +
        "</div>" +
        '<div class="fact-side"><span class="pill ' + esc(fact.status) + '">' +
        statusText(fact.status) + "</span>" +
        '<span class="row-actions">' +
          (fact.status === "suspect"
            ? '<button class="row-btn" data-act="confirm" data-id="' + fact.id + '">确认</button>'
            : "") +
          (fact.kind === "task" && fact.status === "active"
            ? '<button class="row-btn" data-act="close" data-id="' + fact.id + '">完成</button>'
            : "") +
          (fact.kind === "task" && fact.status === "done"
            ? '<button class="row-btn" data-act="reopen" data-id="' + fact.id + '">重开</button>'
            : "") +
          '<button class="row-btn danger" data-act="delete" data-id="' + fact.id + '">删除</button>' +
        "</span></div></li>";
    }).join("");
    syncBulk();
  }

  function statusText(status) {
    return {
      active: "生效中", suspect: "待确认", superseded: "已被取代",
      done: "已完成", cancelled: "已取消",
    }[status] || status;
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

  var ACT_PATH = {
    delete: "facts/delete", confirm: "facts/confirm",
    close: "facts/close", reopen: "facts/close",
  };

  function act(ids, kind) {
    if (!ids.length) return;
    var path = ACT_PATH[kind] || "facts/confirm";
    var body = { scope: state.scope, ids: ids };
    if (kind === "close") body.state = "done";
    if (kind === "reopen") body.state = "active";
    post(path, body).then(function (data) {
      var payload = (data && data.data) || data || {};
      if (payload.ok === false) {
        toast(payload.error || "操作失败");
        return;
      }
      toast(kind === "delete"
        ? "删除 " + (payload.removed || 0) + " 条"
        : kind === "close"
          ? "完成 " + (payload.closed || 0) + " 条"
          : kind === "reopen"
            ? "重开 " + (payload.closed || 0) + " 条"
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
        if (Math.abs(value) < 0.005) return "";
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
          '<span class="log-time">' + esc(timeText(row.ts || row.time)) + "</span>" +
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


  // ---------- 设置 ----------

  function loadSettings() {
    return api("settings").then(function (data) {
      state.settings = data || { groups: [] };
      state.draft = {};
      state.fieldErrors = {};
      renderSettings();
      var diag = (data && data.diagnostics) || null;
      var box = $("set-diag");
      if (box) {
        box.textContent = diag
          ? "v" + String(diag.version).replace(/^v/, "") +
            " · 读到 " + diag.providers + " 个聊天提供商、" + diag.embeddings + " 个嵌入提供商"
          : "";
      }
    });
  }

  function valueOf(field) {
    if (Object.prototype.hasOwnProperty.call(state.draft, field.key)) {
      return state.draft[field.key];
    }
    return field.value;
  }

  function isDirty(field) {
    var now = valueOf(field);
    if (typeof field.value === "boolean") return now !== field.value;
    if (typeof field.value === "number") return Number(now) !== field.value;
    return String(now == null ? "" : now) !== String(field.value == null ? "" : field.value);
  }

  function fieldHtml(field, needle) {
    var dirty = isDirty(field);
    var bad = state.fieldErrors[field.key];
    var title = needle ? markMatch(field.title, needle) : esc(field.title);
    var hint = field.hint ? "<small>" + esc(field.hint) + "</small>" : "";
    var unit = UNIT[field.key] ? '<span class="unit">' + UNIT[field.key] + "</span>" : "";
    var control;
    if (field.options && field.options.length) {
      control = '<select class="pick" data-key="' + esc(field.key) + '">' +
        field.options.map(function (option) {
          return '<option value="' + esc(option.value) + '"' +
            (String(valueOf(field)) === String(option.value) ? " selected" : "") + ">" +
            esc(option.label) + "</option>";
        }).join("") + "</select>";
      return '<div class="set-field' + (dirty ? " is-dirty" : "") + (bad ? " has-error" : "") +
        '" data-field="' + esc(field.key) + '">' +
        '<label class="field-title">' + title + "</label>" + control + hint +
        (bad ? '<em class="field-error">' + esc(bad) + "</em>" : "") + "</div>";
    }
    if (field.type === "bool") {
      control = '<label class="switch"><input type="checkbox" data-key="' +
        esc(field.key) + '"' + (valueOf(field) ? " checked" : "") + " /><span></span></label>";
      return '<div class="set-field' + (dirty ? " is-dirty" : "") + (bad ? " has-error" : "") +
        '" data-field="' + esc(field.key) + '">' +
        '<div class="field-head"><span class="field-title">' + title + "</span>" +
        control + "</div>" + hint +
        (bad ? '<em class="field-error">' + esc(bad) + "</em>" : "") + "</div>";
    }
    if (field.type === "int" || field.type === "float") {
      control = '<input class="num" type="number" data-key="' + esc(field.key) + '" value="' +
        esc(valueOf(field)) + '"' + (field.type === "float" ? ' step="0.01"' : "") + " />" + unit;
    } else {
      control = '<input class="text" type="text" data-key="' + esc(field.key) + '" value="' +
        esc(valueOf(field)) + '" />';
    }
    return '<div class="set-field' + (dirty ? " is-dirty" : "") + (bad ? " has-error" : "") +
      '" data-field="' + esc(field.key) + '">' +
      '<label class="field-title">' + title + "</label>" + control + hint +
      (bad ? '<em class="field-error">' + esc(bad) + "</em>" : "") + "</div>";
  }

  function markMatch(text, needle) {
    var source = String(text == null ? "" : text);
    if (!needle) return esc(source);
    var at = source.toLowerCase().indexOf(String(needle).toLowerCase());
    if (at < 0) return esc(source);
    return esc(source.slice(0, at)) + "<mark>" +
      esc(source.slice(at, at + String(needle).length)) + "</mark>" +
      esc(source.slice(at + String(needle).length));
  }

  function renderSettings() {
    var data = state.settings || { groups: [] };
    var needle = ($("set-q").value || "").trim();
    var touched = Object.keys(state.draft).length;

    $("set-nav").innerHTML = data.groups.map(function (group) {
      return '<a class="set-nav-item" href="#group-' + esc(group.id) + '">' +
        esc(group.title) + "</a>";
    }).join("");

    var html = data.groups.map(function (group) {
      var fields = group.fields.filter(function (field) {
        if (!needle) return true;
        return (field.key + " " + field.title + " " + (field.hint || ""))
          .toLowerCase().indexOf(needle.toLowerCase()) >= 0;
      });
      if (!fields.length) return "";
      return '<section class="set-group" id="group-' + esc(group.id) + '">' +
        '<div class="set-group-head"><h3>' + esc(group.title) + "</h3>" +
        '<p class="hint">' + esc(group.desc || "") + "</p></div>" +
        '<div class="set-fields">' +
        fields.map(function (field) {
          return fieldHtml(field, needle) + "";
        }).join("") +
        "</div></section>";
    }).join("");

    $("set-groups").innerHTML = html || '<p class="empty">没有匹配的设置项。</p>';
    $("set-dirty").textContent = touched ? "已改 " + touched + " 项，待保存" : "没有改动";
    $("set-save").disabled = !touched;
    $("set-undo").disabled = !touched;
  }

  function onSettingsInput(event) {
    var key = event.target.dataset && event.target.dataset.key;
    if (!key) return;
    var raw = event.target.type === "checkbox" ? event.target.checked : event.target.value;
    state.draft[key] = raw;
    delete state.fieldErrors[key];
    renderSettings();
  }

  function undoSettings() {
    state.draft = {};
    state.fieldErrors = {};
    renderSettings();
  }

  function saveSettings() {
    var body = {};
    Object.keys(state.draft).forEach(function (key) { body[key] = state.draft[key]; });
    if (!Object.keys(body).length) return Promise.resolve();
    return post("settings", body).then(function (data) {
      if (data && data.ok === false) {
        state.fieldErrors = (data && data.errors) || {};
        if (state.fieldErrors._) toast(state.fieldErrors._);
        renderSettings();
        return;
      }
      state.draft = {};
      state.fieldErrors = {};
      state.settings = (data && data.settings) || state.settings;
      renderSettings();
      var changed = (data && data.changed) || [];
      toast("已保存 " + changed.length + " 项");
      if (data && data.restart && data.restart.length) {
        toast("这些项要重启插件才生效：" + data.restart.join("、"));
      }
    });
  }

  // ---------- 汇总 ----------

  function refreshAll() {
    if (!state.scope) return Promise.resolve();
    var jobs = [loadStats()];
    if (state.tab === "facts") jobs.push(loadFacts());
    if (state.tab === "persona") jobs.push(loadPersona());
    if (state.tab === "care") jobs.push(loadCare());
    if (state.tab === "logs") jobs.push(loadLogs());
    if (state.tab === "settings") jobs.push(loadSettings());
    return Promise.all(jobs).catch(function (error) {
      toast("加载失败：" + error);
    });
  }

  function switchTab(tab) {
    state.tab = tab;
    syncTabs();
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
    $("set-q").addEventListener("input", renderSettings);
    $("set-groups").addEventListener("input", onSettingsInput);
    $("set-groups").addEventListener("change", onSettingsInput);
    $("set-save").addEventListener("click", saveSettings);
    $("set-undo").addEventListener("click", undoSettings);
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

  function initialTab() {
    // 面板 iframe 的地址不带 query，所以线上永远是 facts；这条只给本地预览用。
    var match = /[?&]tab=([a-z]+)/.exec(window.location.search);
    var name = match ? match[1] : "facts";
    return ["facts", "persona", "care", "logs", "settings"].indexOf(name) >= 0
      ? name
      : "facts";
  }

  function syncTabs() {
    document.querySelectorAll(".tab").forEach(function (button) {
      button.classList.toggle("on", button.dataset.tab === state.tab);
    });
    ["facts", "persona", "care", "logs", "settings"].forEach(function (name) {
      $("panel-" + name).hidden = name !== state.tab;
    });
  }

  function start() {
    state.tab = initialTab();
    bind();
    renderLogLevels();
    syncTabs();
    // 顺序很重要：先等桥（宿主的 bridge-sdk.js 注入在 </body>，晚于本文件执行），
    // 再等 ready()。ready() 要等父窗口发 context 消息，那条消息也可能比监听器早到，
    // 所以它自己有 800ms 兜底；两个等待分开，桥没到就直说，不要再往下走。
    var ready = waitForBridge(3000).then(function (b) {
      if (!b) {
        $("hero-sub").textContent =
          "没拿到宿主 bridge：面板无法调用接口（bridge-sdk.js 没加载成功）";
        return {};
      }
      if (b.onContext) {
        b.onContext(function (info) {
          if (info && info.isDark) document.documentElement.dataset.theme = "dark";
        });
      }
      var wait = b.ready ? b.ready() : Promise.resolve({});
      var guard = new Promise(function (resolve) {
        setTimeout(function () { resolve({}); }, 800);
      });
      return Promise.race([wait, guard]);
    });
    ready.then(function (info) {
      if (info && info.isDark) document.documentElement.dataset.theme = "dark";
      return loadScopes();
    }).then(function () {
      return refreshAll();
    }).catch(function (error) {
      $("hero-sub").textContent = "面板出错：" + ((error && error.message) || error);
      toast("面板出错：" + ((error && error.message) || error));
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();

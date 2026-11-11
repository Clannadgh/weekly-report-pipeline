/* 顶栏「模型」入口：供应商列表 + 增改删 + 拉模型列表 + 测试连接。
   所有页面共用，配置读写 /api/llm 系列接口，密钥不回显。 */
(function () {
  "use strict";

  var PRESETS = [
    { label: "自定义", base: "", model: "" },
    { label: "智谱 GLM", base: "https://open.bigmodel.cn/api/paas/v4", model: "glm-5.3-flash" },
    { label: "DeepSeek", base: "https://api.deepseek.com/v1", model: "deepseek-chat" },
    { label: "通义千问", base: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus" }
  ];

  var TEMPLATE = [
    '<dialog class="model-dialog" id="modelCfgDialog" aria-labelledby="modelCfgTitle">',
    '  <div class="model-card">',
    '    <header class="model-cfg-head">',
    '      <div>',
    '        <div class="model-cfg-kicker">模型配置</div>',
    '        <h2 id="modelCfgTitle">模型供应商</h2>',
    '        <p class="model-cfg-sub" id="modelCfgStatus">读取中…</p>',
    '      </div>',
    '      <button class="model-close" type="button" id="modelCfgClose" aria-label="关闭">×</button>',
    '    </header>',
    '    <div class="model-body">',
    '      <aside class="model-list-wrap">',
    '        <div class="model-list-head"><span>已保存</span><button class="model-btn sm" type="button" id="modelCfgNew">＋ 新增</button></div>',
    '        <div class="model-list" id="modelCfgList"></div>',
    '      </aside>',
    '      <form class="model-form" id="modelCfgForm" novalidate>',
    '        <div class="model-form-title" id="modelCfgFormTitle">新增供应商</div>',
    '        <label class="model-field">常用服务',
    '          <select id="modelCfgPreset"></select>',
    '        </label>',
    '        <label class="model-field">名称（可不填）',
    '          <input id="modelCfgName" type="text" autocomplete="off" placeholder="例如：智谱 GLM">',
    '        </label>',
    '        <label class="model-field">Base URL',
    '          <input id="modelCfgBase" type="text" autocomplete="off" placeholder="https://open.bigmodel.cn/api/paas/v4">',
    '        </label>',
    '        <label class="model-field">API Key',
    '          <input id="modelCfgKey" type="password" autocomplete="new-password" placeholder="填写 API Key">',
    '        </label>',
    '        <label class="model-field">模型',
    '          <span class="model-row">',
    '            <input id="modelCfgModel" type="text" autocomplete="off" placeholder="例如：glm-5.3-flash">',
    '            <button class="model-btn" type="button" id="modelCfgFetch">获取模型</button>',
    '          </span>',
    '        </label>',
    '        <div class="model-options" id="modelCfgOptions" hidden></div>',
    '        <div class="model-form-actions">',
    '          <button class="model-btn" type="button" id="modelCfgTest">测试连接</button>',
    '          <button class="model-btn primary" type="submit" id="modelCfgSave">保存并启用</button>',
    '        </div>',
    '        <p class="model-msg" id="modelCfgMsg" role="status"></p>',
    '      </form>',
    '    </div>',
    '  </div>',
    '</dialog>'
  ].join("\n");

  var status = null;
  var editId = null;

  function $(id) { return document.getElementById(id); }

  function api(path, body) {
    var opts = body === undefined ? {} : {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    };
    return fetch(path, opts).then(function (resp) {
      return resp.json().catch(function () {
        throw new Error("服务器返回不是 JSON（HTTP " + resp.status + "）");
      }).then(function (data) {
        if (!resp.ok || data.ok === false) throw new Error(data.error || ("HTTP " + resp.status));
        return data;
      });
    });
  }

  function setMsg(text, kind) {
    var el = $("modelCfgMsg");
    if (!el) return;
    el.className = "model-msg" + (kind ? " " + kind : "");
    el.textContent = text || "";
  }

  function setBusy(button, busy, busyText, idleText) {
    if (!button) return;
    button.disabled = busy;
    button.textContent = busy ? busyText : idleText;
  }

  function currentModelName() {
    return status && status.configured ? String(status.model || "") : "";
  }

  function updateBadge() {
    var btn = $("modelCfgBtn");
    var label = $("modelCfgLabel");
    if (!btn || !label) return;
    var configured = !!(status && status.configured);
    btn.classList.toggle("is-off", !configured);
    label.textContent = configured ? "模型 · " + currentModelName() : "模型 · 未配置";
    btn.title = configured
      ? (status.name || "") + " · " + currentModelName() + " · " + (status.base_url || "")
      : "点这里配置模型供应商";
    ["llmModel", "engineName", "modelName"].forEach(function (id) {
      var el = $(id);
      if (el) el.textContent = configured ? currentModelName() : "未配置";
    });
  }

  function renderStatus() {
    var el = $("modelCfgStatus");
    if (!el) return;
    if (status && status.configured) {
      el.textContent = "当前生效：" + (status.name || "未命名") + " · " + status.model +
        " · " + status.base_url + (status.source === "env" ? "（来自 .env）" : "");
    } else {
      el.textContent = "当前未配置模型；保存一个供应商后立即生效。";
    }
  }

  function providerItem(provider) {
    var item = document.createElement("div");
    var isCurrent = !!(status && status.source === "file" && provider.active);
    item.className = "model-item" + (isCurrent ? " is-active" : "");
    item.dataset.id = provider.id || "";

    var name = document.createElement("div");
    name.className = "model-item-name";
    name.appendChild(document.createTextNode(provider.name || "未命名"));
    if (isCurrent) {
      var tag = document.createElement("span");
      tag.className = "model-item-tag";
      tag.textContent = "当前";
      name.appendChild(tag);
    }
    item.appendChild(name);

    var meta = document.createElement("div");
    meta.className = "model-item-meta";
    meta.textContent = (provider.model || "") + "\n" + (provider.base_url || "");
    item.appendChild(meta);

    var actions = document.createElement("div");
    actions.className = "model-item-actions";
    if (!isCurrent) {
      actions.appendChild(actionButton("启用", "enable", false));
    }
    actions.appendChild(actionButton("编辑", "edit", false));
    actions.appendChild(actionButton("删除", "delete", false));
    item.appendChild(actions);
    return item;
  }

  function actionButton(label, act, primary) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "model-btn sm" + (primary ? " primary" : "");
    btn.dataset.act = act;
    btn.textContent = label;
    return btn;
  }

  function envItem() {
    var item = document.createElement("div");
    item.className = "model-item is-active";

    var name = document.createElement("div");
    name.className = "model-item-name";
    name.appendChild(document.createTextNode("环境变量 / .env"));
    var tag = document.createElement("span");
    tag.className = "model-item-tag";
    tag.textContent = "当前";
    name.appendChild(tag);
    item.appendChild(name);

    var meta = document.createElement("div");
    meta.className = "model-item-meta";
    meta.textContent = (status.model || "") + "\n" + (status.base_url || "");
    item.appendChild(meta);

    var actions = document.createElement("div");
    actions.className = "model-item-actions";
    actions.appendChild(actionButton("存为供应商", "env", false));
    item.appendChild(actions);
    return item;
  }

  function renderList() {
    var list = $("modelCfgList");
    if (!list) return;
    list.textContent = "";
    var providers = (status && status.providers) || [];
    if (status && status.source === "env") list.appendChild(envItem());
    providers.forEach(function (provider) { list.appendChild(providerItem(provider)); });
    if (!providers.length) {
      var empty = document.createElement("div");
      empty.className = "model-empty";
      if (status && status.env && status.env.configured) {
        var line = document.createElement("span");
        line.appendChild(document.createTextNode("当前使用 .env："));
        var model = document.createElement("b");
        model.textContent = status.env.model || "";
        line.appendChild(model);
        empty.appendChild(line);
        var base = document.createElement("span");
        base.className = "model-empty-base";
        base.textContent = status.env.base_url || "";
        empty.appendChild(base);
        var hint = document.createElement("span");
        hint.className = "model-empty-hint";
        hint.textContent = "新增供应商并保存后，以列表里启用的那份为准。";
        empty.appendChild(hint);
      } else {
        empty.textContent = "还没有供应商，先新增一个。";
      }
      list.appendChild(empty);
    }
  }

  function fillPresetOptions() {
    var select = $("modelCfgPreset");
    if (!select) return;
    PRESETS.forEach(function (preset, index) {
      var option = document.createElement("option");
      option.value = String(index);
      option.textContent = preset.label;
      select.appendChild(option);
    });
  }

  function clearForm() {
    editId = null;
    ["modelCfgName", "modelCfgBase", "modelCfgKey", "modelCfgModel"].forEach(function (id) {
      var el = $(id);
      if (el) el.value = "";
    });
    $("modelCfgPreset").value = "0";
    $("modelCfgKey").placeholder = "填写 API Key";
    $("modelCfgFormTitle").textContent = "新增供应商";
    hideOptions();
    setMsg("");
  }

  function fillForm(provider) {
    editId = provider.id || null;
    $("modelCfgName").value = provider.name || "";
    $("modelCfgBase").value = provider.base_url || "";
    $("modelCfgKey").value = "";
    $("modelCfgKey").placeholder = provider.has_key ? "已保存，留空不修改" : "填写 API Key";
    $("modelCfgModel").value = provider.model || "";
    $("modelCfgPreset").value = "0";
    $("modelCfgFormTitle").textContent = editId ? "编辑供应商" : "新增供应商";
    hideOptions();
    setMsg("");
  }

  function startNew() {
    clearForm();
    if (status && status.env && status.env.configured) {
      $("modelCfgBase").value = status.env.base_url || "";
      $("modelCfgModel").value = status.env.model || "";
      $("modelCfgKey").placeholder = "留空沿用 .env 中的密钥";
      setMsg("已带入 .env 的地址和模型；保存后会固化为供应商配置。");
    }
    $("modelCfgName").focus();
  }

  function hideOptions() {
    var box = $("modelCfgOptions");
    if (!box) return;
    box.hidden = true;
    box.textContent = "";
  }

  function renderOptions(models) {
    var box = $("modelCfgOptions");
    if (!box) return;
    box.textContent = "";
    if (!models.length) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    models.forEach(function (model) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "model-option" + (model.id === $("modelCfgModel").value ? " is-picked" : "");
      btn.textContent = model.id;
      btn.title = model.owned_by ? model.id + " · " + model.owned_by : model.id;
      btn.addEventListener("click", function () {
        $("modelCfgModel").value = model.id;
        Array.prototype.forEach.call(box.children, function (child) {
          child.classList.toggle("is-picked", child === btn);
        });
        setMsg("已选模型：" + model.id, "is-ok");
      });
      box.appendChild(btn);
    });
    if (box.scrollIntoView) box.scrollIntoView({ block: "nearest" });
  }

  function formPayload() {
    var payload = {};
    if (editId) payload.id = editId;
    var name = $("modelCfgName").value.trim();
    var base = $("modelCfgBase").value.trim();
    var key = $("modelCfgKey").value.trim();
    var model = $("modelCfgModel").value.trim();
    if (name) payload.name = name;
    if (base) payload.base_url = base;
    if (key) payload.api_key = key;
    if (model) payload.model = model;
    return payload;
  }

  async function loadState() {
    try {
      var resp = await fetch("/api/llm");
      status = await resp.json();
    } catch (err) {
      status = null;
      var line = $("modelCfgStatus");
      if (line) line.textContent = "读取模型配置失败：" + err.message;
    }
    updateBadge();
    renderStatus();
    renderList();
  }

  async function refresh(prefill) {
    await loadState();
    if (prefill) {
      var active = status && status.configured && status.source === "file" ? status : null;
      if (active) fillForm(active);
      else startNew();
    }
  }

  async function save(event) {
    event.preventDefault();
    var payload = formPayload();
    if (!payload.base_url) { setMsg("请填写 Base URL", "is-bad"); return; }
    if (!payload.model) { setMsg("请填写模型名", "is-bad"); return; }
    payload.activate = true;
    var btn = $("modelCfgSave");
    setBusy(btn, true, "保存中…", "保存并启用");
    try {
      var data = await api("/api/llm/providers", payload);
      await loadState();
      var savedId = (data.saved && data.saved.id) || editId;
      var provider = ((status && status.providers) || []).find(function (p) { return p.id === savedId; });
      if (provider) fillForm(provider);
      setMsg("已保存并启用：" + ((provider && provider.name) || (data.saved && data.saved.name) || ""), "is-ok");
    } catch (err) {
      setMsg(err.message, "is-bad");
    } finally {
      setBusy(btn, false, "保存中…", "保存并启用");
    }
  }

  async function testConnection() {
    var btn = $("modelCfgTest");
    setBusy(btn, true, "测试中…", "测试连接");
    setMsg("正在请求模型接口…");
    try {
      var data = await api("/api/llm/test", formPayload());
      setMsg("连接正常：回复「" + (data.reply || "") + "」，" + (data.elapsed_ms || 0) +
        " ms，模型 " + (data.model || ""), "is-ok");
    } catch (err) {
      setMsg("连接失败：" + err.message, "is-bad");
    } finally {
      setBusy(btn, false, "测试中…", "测试连接");
    }
  }

  async function fetchModels() {
    var payload = formPayload();
    if (!payload.base_url) { setMsg("先填 Base URL，再获取模型", "is-bad"); return; }
    var btn = $("modelCfgFetch");
    setBusy(btn, true, "获取中…", "获取模型");
    setMsg("正在请求模型列表…");
    try {
      var data = await api("/api/llm/models", payload);
      renderOptions(data.models || []);
      setMsg("拿到 " + data.models.length + " 个模型，点一个填入。", "is-ok");
    } catch (err) {
      hideOptions();
      setMsg(err.message, "is-bad");
    } finally {
      setBusy(btn, false, "获取中…", "获取模型");
    }
  }

  async function handleListClick(event) {
    var btn = event.target.closest("button[data-act]");
    if (!btn) return;
    var act = btn.dataset.act;
    if (act === "env") {
      startNew();
      setMsg("把 .env 的地址和模型带进了表单；命名后点「保存并启用」。");
      return;
    }
    var item = btn.closest("[data-id]");
    var providerId = item && item.dataset.id;
    var provider = ((status && status.providers) || []).find(function (p) { return p.id === providerId; });
    if (!provider) return;
    if (act === "edit") {
      fillForm(provider);
      $("modelCfgName").focus();
      return;
    }
    if (act === "enable") {
      try {
        await api("/api/llm/activate", { id: providerId });
        await loadState();
        setMsg("已启用：" + provider.name, "is-ok");
      } catch (err) {
        setMsg(err.message, "is-bad");
      }
      return;
    }
    if (act === "delete") {
      if (!window.confirm("删除供应商「" + provider.name + "」？")) return;
      try {
        await api("/api/llm/delete", { id: providerId });
        var wasEditing = editId === providerId;
        await loadState();
        if (wasEditing) startNew();
        setMsg("已删除：" + provider.name, "is-ok");
      } catch (err) {
        setMsg(err.message, "is-bad");
      }
    }
  }

  function bindDialog() {
    var dialog = $("modelCfgDialog");
    $("modelCfgClose").addEventListener("click", function () { dialog.close(); });
    $("modelCfgNew").addEventListener("click", startNew);
    $("modelCfgForm").addEventListener("submit", save);
    $("modelCfgTest").addEventListener("click", testConnection);
    $("modelCfgFetch").addEventListener("click", fetchModels);
    $("modelCfgList").addEventListener("click", handleListClick);
    $("modelCfgPreset").addEventListener("change", function () {
      var preset = PRESETS[Number(this.value)] || PRESETS[0];
      if (!preset.base) return;
      $("modelCfgBase").value = preset.base;
      $("modelCfgModel").value = preset.model;
      if (!$("modelCfgName").value.trim()) $("modelCfgName").value = preset.label;
      hideOptions();
      setMsg("已填入 " + preset.label + " 的默认地址和模型，可以再改。");
    });
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) dialog.close();
    });
  }

  function mount() {
    var header = document.querySelector("header.topbar");
    if (!header || $("modelCfgBtn")) return;
    var host = header.querySelector(".top-actions") || header.querySelector(".headlinks") ||
      header.querySelector(".headlink") || header;
    var btn = document.createElement("button");
    btn.type = "button";
    btn.id = "modelCfgBtn";
    btn.className = "model-btn model-config-btn";
    btn.innerHTML = '<span class="model-dot" aria-hidden="true"></span><span id="modelCfgLabel">模型</span>';
    host.appendChild(btn);

    document.body.insertAdjacentHTML("beforeend", TEMPLATE);
    fillPresetOptions();
    bindDialog();
    btn.addEventListener("click", function () { refresh(true); $("modelCfgDialog").showModal(); });
    loadState();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();

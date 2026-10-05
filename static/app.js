"use strict";
const $ = (id) => document.getElementById(id);
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const percent = (value) => Number((value * 100).toFixed(1));
const media = (file, thumb = false) => `/media/${file ? file.replace(/\.png$/, thumb ? "_thumb.png" : ".png") : ""}`;
const statusText = {queued:"等待处理", running:"处理中", ready:"已去背景", done:"已完成", error:"处理失败", partial:"部分失败", interrupted:"已中断"};
const jobTitles = {remove:"批量抠图", compose:"合成预览", generate:"ChatGPT 光影处理", restore:"还原商品细节"};
let state = {products:[], templates:[], composites:[], results:[], jobs:[], sizes:{}};
let view = "workspace", fingerprint = "", firstLoad = true, refreshing = false, submitting = false;
let selectedProducts = new Set(), selectedComposites = new Set(), selectedResults = new Set();
let inspector = null;
let confirmResolve = null;

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...(options.body instanceof FormData ? {} : {"Content-Type":"application/json"}), ...options.headers } });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.rejected?.map(r=>`${r.name}：${r.error}`).join("\n") || "请求失败，请重试。");
  return data;
}
function toast(message, error = false) {
  const element = document.createElement("div");
  element.className = `toast${error ? " error" : ""}`;
  const close = document.createElement("button");
  close.textContent = "×"; close.setAttribute("aria-label", "关闭提示"); close.onclick = () => element.remove();
  element.append(close, document.createTextNode(message));
  $("toast-container").append(element);
  setTimeout(() => element.remove(), error ? 15000 : 6000);
}
function prune(set, identifiers) { for (const id of set) if (!identifiers.has(id)) set.delete(id); }
function activeComposites() { return state.composites.filter(c => c.active); }
function busyCompositeIds() {
  return new Set(state.jobs.filter(j=>j.kind==="generate").flatMap(j=>j.items.filter(i=>["queued","running"].includes(i.status)).map(i=>i.ref_id)));
}
function visibleResults() {
  const product = $("result-product").value, query = $("result-search").value.toLowerCase().trim();
  return state.results.filter(r=>(product==="all" || r.product_id===product) &&
    (!query || `${r.product_name} ${r.template_name}`.toLowerCase().includes(query)));
}
async function refresh(force = false) {
  if (refreshing) return;
  refreshing = true;
  try {
    const data = await api("/api/state");
    $("connection").textContent = "本地工作空间";
    const signature = JSON.stringify(data);
    if (force || signature !== fingerprint) {
      const previous = new Set(state.products.map(p=>p.id));
      state = data; fingerprint = signature;
      for (const product of state.products) if (firstLoad || !previous.has(product.id)) selectedProducts.add(product.id);
      firstLoad = false;
      prune(selectedProducts, new Set(state.products.map(p=>p.id)));
      prune(selectedComposites, new Set(activeComposites().map(c=>c.id)));
      prune(selectedResults, new Set(state.results.map(r=>r.id)));
      render();
    }
  } catch (error) {
    $("connection").textContent = "连接已断开";
    if (force || firstLoad) toast(error.message, true);
  } finally { refreshing = false; }
}
function render() {
  $("nav-products").textContent = state.products.length;
  $("nav-templates").textContent = state.templates.length;
  $("nav-results").textContent = state.results.length;
  $("product-count").textContent = state.products.length;
  $("template-count").textContent = state.templates.length;
  $("job-count").textContent = state.jobs.filter(j=>["queued","running"].includes(j.status)).length;
  const oldFilter = $("result-product").value;
  $("result-product").innerHTML = `<option value="all">所有商品</option>${state.products.map(p=>`<option value="${p.id}">${escapeHTML(p.name)}</option>`).join("")}`;
  $("result-product").value = state.products.some(p=>p.id===oldFilter) ? oldFilter : "all";
  renderProducts(); renderComposites(); renderTemplates(); renderResults(); renderDock(); renderJobs();
  if ($("inspector").open && inspector) renderInspector();
}
function recutButtons(product, className = "product-recut") {
  if (!["ready", "error"].includes(product.status)) return "";
  return ["poof", "photoroom"].map(provider => {
    const name = provider === "photoroom" ? "Photoroom" : "Poof";
    return `<button class="${className}" data-recut-product="${product.id}" data-recut-provider="${provider}" title="从保存的原始照片调用 ${name} API 抠图">用 ${name} 重新抠图</button>`;
  }).join("");
}
function renderProducts() {
  $("product-empty").hidden = !!state.products.length;
  $("product-grid").innerHTML = state.products.map(p=> {
    const pending = ["queued","running"].includes(p.status);
    return `<article class="product-card ${selectedProducts.has(p.id) ? "selected" : ""}">
      <button class="select-mark" data-select-product="${p.id}" aria-label="选择 ${escapeHTML(p.name)}" aria-pressed="${selectedProducts.has(p.id)}">${icon("check")}</button>
      <button class="product-remove" data-delete-product="${p.id}" aria-label="移除 ${escapeHTML(p.name)}">${icon("trash")}</button>
      <button class="product-image ${p.cutout ? "checker" : ""}" data-inspect-product="${p.id}" title="查看图片流程"><img loading="lazy" src="${media(p.cutout || p.original, true)}" alt="${escapeHTML(p.name)}"></button>
      <div class="product-meta"><strong title="${escapeHTML(p.name)}">${escapeHTML(p.name)}</strong><div class="product-footer"><select data-size-product="${p.id}" aria-label="${escapeHTML(p.name)} 的尺寸">${Object.keys(state.sizes).map(s=>`<option ${s===p.size ? "selected" : ""}>${s}</option>`).join("")}</select>
      ${p.status==="error" ? `<button class="status-label error" data-retry-product="${p.id}" title="${escapeHTML(p.error)}">失败 · 重试</button>` : `<span class="status-label ${pending ? "pending" : ""}">${pending ? '<i class="spinner"></i>' : ""}${p.status==="ready" ? (p.placement_confirmed ? "摆放已确认" : "待确认摆放") : statusText[p.status]}</span>`}</div>
      ${p.status==="ready" ? `<button class="product-align ${p.placement_confirmed ? "confirmed" : ""}" data-align-product="${p.id}">${p.placement_confirmed ? "调整摆放" : "预览并调整摆放"} ${icon("arrow")}</button>` : ""}${recutButtons(p)}</div></article>`;
  }).join("");
  $("size-toolbar").hidden = !selectedProducts.size;
  $("selected-product-count").textContent = selectedProducts.size;
  const sizes = new Set(state.products.filter(p=>selectedProducts.has(p.id)).map(p=>p.size));
  $("batch-sizes").innerHTML = Object.keys(state.sizes).map(s=>`<button class="${sizes.size===1 && sizes.has(s) ? "active" : ""}" data-batch-size="${s}" title="画布宽度的 ${percent(state.sizes[s])}%">${s}</button>`).join("");
  $("download-cutouts").disabled = !state.products.filter(p=>selectedProducts.has(p.id)).every(p=>p.cutout);
  $("align-next").disabled = !state.products.some(p=>p.status==="ready" && selectedProducts.has(p.id));
  $("select-products").textContent = state.products.length && selectedProducts.size===state.products.length ? "取消全选" : "全选商品";
}
function empty(element, title, description, action = "", imageIcon = "image") {
  element.innerHTML = `<span class="empty-icon">${icon(imageIcon)}</span><h3>${title}</h3><p>${description}</p>${action}`;
}
function renderComposites() { renderGroupedComposites(); }
function renderTemplates() {
  $("template-empty").hidden = !!state.templates.length;
  $("template-grid").innerHTML = state.templates.map(t=>`<article class="image-card"><span class="card-badge">4:5</span><button class="image-button" data-inspect-template="${t.id}"><img loading="lazy" src="${media(t.file,true)}" alt="${escapeHTML(t.name)}"><span class="image-open">查看模板 ↗</span></button><div class="card-meta"><strong title="${escapeHTML(t.name)}">${escapeHTML(t.name)}</strong><div class="meta-row"><span>1080 × 1350</span></div></div><button class="template-delete" data-delete-template="${t.id}" aria-label="移除 ${escapeHTML(t.name)}">${icon("trash")}</button></article>`).join("");
}
function renderResults() {
  const results = visibleResults();
  $("result-total").textContent = `共 ${results.length} 张成品`;
  $("result-grid").innerHTML = results.map(r=>`<article class="image-card ${selectedResults.has(r.id) ? "selected" : ""}"><button class="select-mark" data-select-result="${r.id}" aria-label="选择 ${escapeHTML(r.product_name)} 成品 ${r.variant}" aria-pressed="${selectedResults.has(r.id)}">${icon("check")}</button><span class="card-badge">结果 ${r.variant} · ${r.size}</span><button class="image-button" data-inspect-result="${r.id}"><img loading="lazy" src="${media(r.file,true)}" alt="${escapeHTML(r.product_name)} · 结果 ${r.variant}"><span class="image-open">查看图片流程 ↗</span></button><div class="card-meta"><strong title="${escapeHTML(r.product_name)}">${escapeHTML(r.product_name)}</strong><div class="meta-row"><span title="${escapeHTML(r.template_name)}">${escapeHTML(r.template_name)}</span><span>${r.width} × ${r.height}</span></div></div></article>`).join("");
  $("result-empty").hidden = !!results.length;
  empty($("result-empty"), state.results.length ? "没有符合条件的成品" : "你的下一张好照片，从这里开始", state.results.length ? "试试其他商品或搜索词。" : "在批量工作台选择合成预览，点击 ChatGPT 处理。生成完成后，所有结果会自动保存到这里。", state.results.length ? "" : `<button class="secondary small" data-view="workspace">前往批量工作台 ${icon("arrow")}</button>`, "spark");
  $("select-results").disabled = !results.length;
  renderDock();
}
function renderDock() {
  $("generation-dock").hidden = view !== "workspace";
  $("results-dock").hidden = view !== "results";
  $("selected-composite-count").textContent = selectedComposites.size;
  const n = Number($("generation-n").value || 3);
  $("generation-estimate").textContent = selectedComposites.size ? `${selectedComposites.size} 个组合 × ${n} 个结果 = ${selectedComposites.size*n} 张成品 · 通过 OpenAI API 生成` : "选中喜欢的组合，开始自然光影处理";
  const eligible = [...selectedComposites].filter(id=>!busyCompositeIds().has(id));
  $("generate-button").disabled = !eligible.length || submitting || !state.key_ready;
  $("generate-button").title = state.key_ready ? "" : "请在根目录放入 openai_key";
  $("download-composites").disabled = !selectedComposites.size;
  $("selected-result-count").textContent = selectedResults.size;
  $("download-results").disabled = !selectedResults.size;
  if (typeof renderCombinationSelection === "function") renderCombinationSelection();
}
function switchView(next) {
  view = next;
  for (const name of ["workspace","templates","results"]) $("view-"+name).hidden = name!==view;
  document.querySelectorAll(".nav-item").forEach(b=>b.classList.toggle("active",b.dataset.view===view));
  $("breadcrumb-current").textContent = {workspace:"批量工作台",templates:"背景模板",results:"成品图库"}[view];
  renderDock(); window.scrollTo({top:0,behavior:"smooth"});
}
function toggle(set, id) { set.has(id) ? set.delete(id) : set.add(id); }
function selectVisible(set, entries) { for (const item of entries) set.add(item.id); }

async function uploadFiles(kind, files) {
  if (!files.length) return;
  const form = new FormData();
  for (const file of files) form.append("files",file);
  document.querySelectorAll(`[data-upload="${kind}"]`).forEach(b=>b.disabled=true);
  toast(`正在上传 ${files.length} 张${kind==="products" ? "商品照片" : "背景模板"}…`);
  try {
    const result = await api(`/api/${kind}`,{method:"POST",body:form});
    toast(`已上传 ${result.accepted.length} 张${kind==="products" ? "照片，Poof 正在抠图" : "背景模板，已统一为 1080 × 1350"}。`);
    if (result.rejected.length) toast(result.rejected.map(r=>`${r.name}：${r.error}`).join("\n"),true);
    await refresh(true);
  } catch(error) { toast(error.message,true); }
  finally { document.querySelectorAll(`[data-upload="${kind}"]`).forEach(b=>b.disabled=false); $(kind+"-input").value=""; }
}
async function setSizes(identifiers, size) {
  if (!identifiers.length) return;
  try {
    await api("/api/products/size",{method:"PATCH",body:JSON.stringify({ids:identifiers,size})});
    toast(`已设为「${size}」，请在单模板预览中确认摆放。`); await refresh(true);
  } catch(error) { toast(error.message,true); await refresh(true); }
}
async function downloadBatch(kind, identifiers, button) {
  if (!identifiers.length) return;
  if (button) button.disabled=true;
  toast("正在准备下载文件…");
  try {
    const response=await fetch("/api/download",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({kind,ids:identifiers})});
    if (!response.ok) throw new Error((await response.json()).error);
    const url=URL.createObjectURL(await response.blob());
    const a=document.createElement("a"); a.href=url; a.download=`LE_${kind}.zip`; document.body.append(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(url),60000);
  } catch(error) { toast(error.message,true); }
  finally { if (button) button.disabled=false; renderDock(); }
}
function askConfirm(title, description) {
  $("confirm-title").textContent=title; $("confirm-description").textContent=description;
  $("confirm-dialog").showModal();
  return new Promise(resolve=>confirmResolve=resolve);
}
function finishConfirm(value) { $("confirm-dialog").close(); if(confirmResolve) confirmResolve(value); confirmResolve=null; }
function openInspector(productId, stage="cutout", compositeId=null, resultId=null) {
  inspector={productId,stage,compositeId,resultId,showRaw:false};
  renderInspector(); $("inspector").showModal();
}
function renderInspector() {
  if (!inspector) return;
  const p=state.products.find(p=>p.id===inspector.productId);
  if (!p) { $("inspector").close(); return; }
  const composites=state.composites.filter(c=>c.product_id===p.id);
  let c=composites.find(c=>c.id===inspector.compositeId) || composites.find(c=>c.active) || composites[0];
  if(c) inspector.compositeId=c.id;
  const results=state.results.filter(r=>r.product_id===p.id && (!c || r.composite_id===c.id));
  let r=results.find(r=>r.id===inspector.resultId) || results[0];
  if(r) inspector.resultId=r.id;
  $("inspector-title").textContent=p.name;
  const stage=inspector.stage;
  $("inspector-stages").innerHTML=[['original','01','原始照片'],['cutout','02','透明底商品'],['composite','03','合成预览'],['result','04','AI 成品']].map(([s,num,label])=>`<button data-stage="${s}" class="stage-tab ${stage===s ? "active" : ""}"><span>${num}</span>${label}</button>`).join("");
  let file=null, download=null, details="", variants="";
  if (stage==="original") {
    file=p.original; download=`/api/download/original/${p.id}`;
    details=`<div class="detail-label">原始照片</div><div class="detail-value">${escapeHTML(p.name)}</div><p class="muted">上传原图已在本地保存（修正方向后存为 PNG）。重新抠图始终读取这张原图。</p><div class="detail-label">当前包包尺寸</div><div class="detail-value">${p.size} · ${percent(state.sizes[p.size])}% 画布宽度</div>`;
  } else if (stage==="cutout") {
    file=p.cutout; download=file ? `/api/download/cutout/${p.id}` : null;
    details=`<div class="detail-label">去背景状态</div><div class="detail-value">${statusText[p.status]} · ${({poof:"Poof", photoroom:"Photoroom"}[p.cutout_provider] || "已有透明图")}</div><div class="detail-label">格式</div><div class="detail-value">透明 PNG</div>${p.status==="ready" ? `<button class="primary small" data-align-product="${p.id}">预览并调整摆放</button>` : ""}${recutButtons(p, "quiet small")}${p.error ? `<div class="detail-error">${escapeHTML(p.error)}</div><button class="secondary small" data-retry-product="${p.id}">重新去背景</button>` : ""}`;
  } else if (stage==="composite" || stage==="result") {
    const list=stage==="composite" ? composites : results;
    const showRaw=stage==="result" && inspector.showRaw && r?.raw_file;
    file=stage==="composite" ? c?.file : showRaw ? r.raw_file : r?.file;
    download=file ? `/api/download/${stage==="composite" ? "composite" : showRaw ? "raw-result" : "result"}/${stage==="composite" ? c.id : r.id}` : null;
    const place=c ? JSON.parse(c.placement) : null;
    details=`<div class="detail-label">背景模板</div><div class="detail-value">${escapeHTML(c?.template_name || "尚未合成")}</div><div class="detail-label">本次合成的尺寸</div><div class="detail-value">${c?.size || p.size}${c && !c.active ? " · 历史版本" : ""}</div>${place ? `<div class="detail-label">商品位置 / 尺寸</div><div class="detail-value">左上角 ${place.x}, ${place.y}<br>包包 ${place.width} × ${place.height} px<br>旋转 ${place.angle || 0}°</div>` : ""}${stage==="composite" && p.status==="ready" ? `<button class="secondary small" data-align-product="${p.id}">调整摆放</button>` : ""}`;
    if(stage==="result" && r) {
      if(r.raw_file) details+=`<div class="detail-label">商品细节保护</div><div class="size-segments"><button data-result-display="final" class="${showRaw ? "" : "active"}">商品还原成品</button><button data-result-display="raw" class="${showRaw ? "active" : ""}">AI 原始结果</button></div>`;
      details+=`<div class="detail-label">结果 ${r.variant} · ${new Date(r.created).toLocaleString("zh-CN")}</div><div class="detail-value">${showRaw ? r.raw_width : r.width} × ${showRaw ? r.raw_height : r.height} · PNG<br>${showRaw ? "AI 原始结果，仅供比较" : r.product_restored ? "已按原位置覆盖原商品" : "等待还原商品细节"}<br>生成任务 ${r.job_id.slice(0,8)}</div>`;
    }
    if(stage==="result" && !r) details+=`<div class="detail-label">还没有这个组合的成品</div><div class="detail-value">在工作台选择合成图并开始 ChatGPT 处理。</div>`;
    variants=list.map(item=>`<button class="variant ${(stage==="composite" ? c?.id : r?.id)===item.id ? "active" : ""}" data-inspector-${stage}="${item.id}" title="${escapeHTML(stage==="composite" ? item.template_name + ' · ' + item.size : '结果 '+item.variant+' · '+item.job_id.slice(0,8))}"><img loading="lazy" src="${media(item.file,true)}" alt="${stage==="composite" ? escapeHTML(item.template_name) : '结果 '+item.variant}"><span>${stage==="composite" ? `${item.size} · ${item.active ? "当前" : "历史"}` : `结果 ${item.variant}`}</span></button>`).join("");
  }
  $("inspector-details").innerHTML=details; $("inspector-variants").innerHTML=variants;
  $("inspector-image").hidden=!file; $("inspector-no-image").hidden=!!file;
  $("zoom-button").hidden=!file; $("inspector-download").hidden=!download;
  if(file) $("inspector-image").src=media(file);
  $("inspector-image").alt=`${p.name} · ${stage}`;
  if(download) $("inspector-download").href=download;
}
function renderJobs() {
  $("jobs-list").innerHTML=state.jobs.length ? state.jobs.map(j=> {
    const complete=j.items.filter(i=>i.status==="done").length, failures=j.items.filter(i=>["error","interrupted"].includes(i.status));
    return `<article class="job"><div class="job-heading"><strong>${jobTitles[j.kind]} ${j.kind==="generate" ? `· 每张 ${j.n} 个结果` : ""}</strong><span class="status-label ${failures.length ? "error" : ""}">${["queued","running"].includes(j.status)?'<i class="spinner"></i> ':""}${statusText[j.status]}</span></div><div class="job-meta">${new Date(j.created).toLocaleString("zh-CN")} · ${complete} / ${j.items.length} 已完成</div><div class="job-progress"><span style="width:${(complete+failures.length)/j.items.length*100}%"></span></div>${failures.map(i=> {
      const ref=j.kind==="generate" ? state.composites.find(c=>c.id===i.ref_id) : state.products.find(p=>p.id===i.ref_id);
      const name=ref ? (ref.name || `${ref.product_name} / ${ref.template_name}`) : i.ref_id.slice(0,8);
      return `<div class="job-error"><span class="job-item-label">${escapeHTML(name)}</span>${escapeHTML(i.error)}</div>`;
    }).join("")}${failures.length ? `<button class="secondary small job-retry" data-retry-job="${j.id}">重试失败项目 ${j.kind==="generate" ? '· 会重新调用 API' : ''}</button>` : ""}</article>`;
  }).join("") : `<p class="muted">任务会在这里记录。上传第一张商品照片开始吧。</p>`;
}
function showSettings() {
  $("settings-content").innerHTML=`<p class="muted">已保留你的 Playground 配方，只有每张生成数量在工作台动态选择。</p><table class="settings-table"><tbody>${Object.entries(state.settings || {}).map(([k,v])=>`<tr><td>${escapeHTML(k)}</td><td>${escapeHTML(v)}</td></tr>`).join("")}<tr><td>n</td><td>1–10，默认 3</td></tr></tbody></table><div class="detail-label">你的原始 Prompt</div><div class="prompt-text">${escapeHTML(state.prompt)}</div><div class="detail-label">五档尺寸 · 画布宽度</div><div class="settings-sizes">${Object.entries(state.sizes).map(([k,v])=>`<span>${k} ${percent(v)}%</span>`).join("")}</div><p class="muted">默认商品最底部距离背景底部约 12.5%（1/8），水平居中。单模板预览支持拖动、缩放、旋转和精确位置调整；确认后套用所有模板。五档按可见商品宽度计算；过高商品默认等比缩小。AI 返回后，先将背景统一为 1080 × 1350，再按生成前的原位置、原尺寸覆盖原商品。AI 原始结果保留在单图流程中供比较。</p><p class="muted">抠图使用 Poof Background Removal API。${state.removal_ready ? "已读取本地 Poof key。" : "请在根目录放入 poof.bg_key。"}商品卡片和单图流程中都可选择 Poof 或 Photoroom 重新抠图。${state.photoroom_ready ? "已读取本地 Photoroom key。" : "使用 Photoroom 需在根目录放入 photoroom_key。"}重新抠图使用保存的原始照片。</p><p class="muted">${state.key_ready ? "已读取本地 OpenAI key。" : "请把 OpenAI key 放在项目根目录的 openai_key 文件。"}生成调用在本地服务端完成。</p>`;
  $("settings-dialog").showModal();
}

document.addEventListener("click", async (event)=> {
  const button=event.target.closest("button, a[data-view]"); if(!button) return;
  const d=button.dataset;
  try {
    if(d.view) switchView(d.view);
    else if(d.upload) $(d.upload+"-input").click();
    else if(d.close) $(d.close).close();
    else if(d.selectProduct) { toggle(selectedProducts,d.selectProduct); renderProducts(); renderComposites(); }
    else if(d.selectComposite) { toggle(selectedComposites,d.selectComposite); renderComposites(); }
    else if(d.selectResult) { toggle(selectedResults,d.selectResult); renderResults(); }
    else if(d.batchSize) await setSizes([...selectedProducts],d.batchSize);
    else if(d.alignProduct) { if($("inspector").open) $("inspector").close(); await openPlacementEditor(d.alignProduct); }
    else if(d.recutProduct) {
      button.disabled=true;
      await api("/api/products/remove-background",{method:"POST",body:JSON.stringify({ids:[d.recutProduct],provider:d.recutProvider || "poof"})});
      toast(`已提交 ${d.recutProvider === "photoroom" ? "Photoroom" : "Poof"} 抠图，完成后请重新确认摆放。`); await refresh(true);
    }
    else if(d.inspectProduct) openInspector(d.inspectProduct);
    else if(d.inspectComposite) { const c=state.composites.find(c=>c.id===d.inspectComposite); openInspector(c.product_id,"composite",c.id); }
    else if(d.inspectResult) { const r=state.results.find(r=>r.id===d.inspectResult); openInspector(r.product_id,"result",r.composite_id,r.id); }
    else if(d.inspectTemplate) { const t=state.templates.find(t=>t.id===d.inspectTemplate); $("lightbox-image").src=media(t.file); $("lightbox").showModal(); }
    else if(d.stage) { inspector.stage=d.stage; renderInspector(); }
    else if(d.inspectorComposite) { inspector.compositeId=d.inspectorComposite; inspector.resultId=null; renderInspector(); }
    else if(d.inspectorResult) { inspector.resultId=d.inspectorResult; renderInspector(); }
    else if(d.resultDisplay) { inspector.showRaw=d.resultDisplay==="raw"; renderInspector(); }
    else if(d.deleteTemplate || d.deleteProduct) {
      const kind=d.deleteTemplate ? "templates" : "products", id=d.deleteTemplate || d.deleteProduct;
      if(await askConfirm(kind==="templates" ? "移除这个背景模板？" : "移除这个商品？",kind==="templates" ? "模板将从库中移除，已有合成记录和成品会保留。" : "商品及其图片将从工作台和图库隐藏，本地文件仍然保留。")) {
        await api(`/api/${kind}/${id}`,{method:"DELETE"}); await refresh(true); toast("已移除。");
      }
    } else if(d.retryProduct) {
      button.disabled=true; await api("/api/products/retry",{method:"POST",body:JSON.stringify({ids:[d.retryProduct]})}); await refresh(true); toast("已加入去背景队列。");
    } else if(d.retryJob) {
      button.disabled=true; const response=await api(`/api/jobs/${d.retryJob}/retry`,{method:"POST",body:"{}"}); await refresh(true); toast(response.job_id ? "失败项目已加入队列。" : "这些项目已在队列中或已移除。");
    } else if(d.openJobs!==undefined) $("jobs-dialog").showModal();
  } catch(error) { button.disabled=false; toast(error.message,true); }
});
document.addEventListener("change",(event)=> {
  if(event.target.dataset.sizeProduct) setSizes([event.target.dataset.sizeProduct],event.target.value);
});
$("products-input").onchange=(e)=>uploadFiles("products",[...e.target.files]);
$("templates-input").onchange=(e)=>uploadFiles("templates",[...e.target.files]);
for(const element of document.querySelectorAll("[data-drop]")) {
  element.addEventListener("dragover",e=> { e.preventDefault(); element.classList.add("dragging"); });
  element.addEventListener("dragleave",()=>element.classList.remove("dragging"));
  element.addEventListener("drop",e=> { e.preventDefault(); element.classList.remove("dragging"); uploadFiles(element.dataset.drop,[...e.dataTransfer.files]); });
}
// Dropping onto the library/workbench also works after the empty upload box disappears.
document.addEventListener("dragover",e=>e.preventDefault());
document.addEventListener("drop",e=> {
  e.preventDefault(); if(!e.target.closest("[data-drop]") && e.dataTransfer.files.length) uploadFiles(view==="templates" ? "templates" : "products",[...e.dataTransfer.files]);
});
$("select-products").onclick=()=> { if(selectedProducts.size===state.products.length) selectedProducts.clear(); else selectVisible(selectedProducts,state.products); renderProducts(); renderComposites(); };
$("clear-composites").onclick=()=> { selectedComposites.clear(); renderComposites(); };
$("select-results").onclick=()=> { selectVisible(selectedResults,visibleResults()); renderResults(); };
$("clear-results").onclick=()=> { selectedResults.clear(); renderResults(); };
$("preview-filter").onchange=renderComposites; $("preview-search").oninput=renderComposites;
$("result-product").onchange=renderResults; $("result-search").oninput=renderResults;
$("generation-n").innerHTML=Array.from({length:10},(_,i)=>`<option value="${i+1}">${i+1}</option>`).join("");
$("generation-n").value="3";
$("generation-n").onchange=renderDock;
async function submitGeneration(requested) {
  if(submitting) return;
  const busy=busyCompositeIds(), identifiers=[...requested].filter(id=>!busy.has(id));
  if(!identifiers.length) return;
  submitting=true; renderDock();
  try {
    const response=await api("/api/generate",{method:"POST",body:JSON.stringify({ids:identifiers,n:Number($("generation-n").value)})});
    if(response.job_id) { identifiers.forEach(id=>selectedComposites.delete(id)); toast(`${identifiers.length} 张合成图已加入光影处理队列，可在成品图库查看结果。`); }
    else toast("这些合成图已在处理队列中。");
    await refresh(true);
  } catch(error) { toast(error.message,true); }
  finally { submitting=false; renderDock(); }
}
$("generate-button").onclick=()=>submitGeneration([...selectedComposites]);
$("download-results").onclick=()=>downloadBatch("results",[...selectedResults],$("download-results"));
$("download-composites").onclick=()=>downloadBatch("composites",[...selectedComposites],$("download-composites"));
$("download-cutouts").onclick=()=>downloadBatch("cutouts",[...selectedProducts],$("download-cutouts"));
$("jobs-button").onclick=()=>$("jobs-dialog").showModal();
$("settings-button").onclick=showSettings;
$("zoom-button").onclick=()=> { $("lightbox-image").src=$("inspector-image").src; $("lightbox").showModal(); };
$("confirm-ok").onclick=()=>finishConfirm(true); $("confirm-cancel").onclick=()=>finishConfirm(false);
$("confirm-dialog").addEventListener("cancel",e=> { e.preventDefault(); finishConfirm(false); });
for(const dialog of document.querySelectorAll("dialog")) dialog.addEventListener("click",e=> { if(e.target===dialog) { const r=dialog.getBoundingClientRect(); if(e.clientX<r.left || e.clientX>r.right || e.clientY<r.top || e.clientY>r.bottom) dialog=== $("confirm-dialog") ? finishConfirm(false) : dialog.close(); } });
refresh(true);
setInterval(()=>refresh(),2500);

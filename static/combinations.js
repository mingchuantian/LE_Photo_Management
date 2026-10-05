"use strict";
let combinationsProduct = null, combinationsSignature = "";
function combinationsFor(productId, filter="all", query="") {
  const done = new Set(state.results.map(r=>r.composite_id));
  return activeComposites().filter(c=>c.product_id===productId &&
    (filter==="all" || (filter==="new" ? !done.has(c.id) : done.has(c.id))) &&
    (!query || c.template_name.toLowerCase().includes(query)));
}
function currentCombinations() {
  return combinationsFor(combinationsProduct,$("combinations-filter").value,$("combinations-search").value.toLowerCase().trim());
}
function confirmedCombinationProducts() { return state.products.filter(p=>p.placement_confirmed && p.status==="ready"); }
function productComposing(productId) {
  return state.jobs.some(j=>j.kind==="compose" && j.items.some(i=>i.ref_id===productId && ["queued","running"].includes(i.status)));
}
function renderGroupedComposites() {
  const filter=$("preview-filter").value, query=$("preview-search").value.toLowerCase().trim(), busy=busyCompositeIds();
  const products=confirmedCombinationProducts().filter(p=>(!selectedProducts.size || selectedProducts.has(p.id)) &&
    (!query || p.name.toLowerCase().includes(query)) && (filter==="all" || combinationsFor(p.id,filter).length));
  const total=products.reduce((sum,p)=>sum+combinationsFor(p.id).length,0);
  $("preview-count").textContent=products.length;
  $("scope-label").textContent=`${products.length} 个商品 · ${total} 个组合`;
  $("composite-grid").innerHTML=products.map(p=> {
    const all=combinationsFor(p.id), selected=all.filter(c=>selectedComposites.has(c.id)).length;
    const processing=all.filter(c=>busy.has(c.id)).length, making=productComposing(p.id);
    const cover=all.find(c=>c.template_id===p.preview_template_id)||all[0];
    return `<article class="combination-group ${selected ? "has-selection" : ""}">
      <button class="combination-cover ${cover ? "" : "checker"}" data-open-combinations="${p.id}" aria-label="查看 ${escapeHTML(p.name)} 的背景组合"><img loading="lazy" src="${media(cover?.file || p.cutout,true)}" alt="${escapeHTML(p.name)}"><span>${selected ? `已选 ${selected} 张` : "挑选背景组合"}</span></button>
      <div class="combination-group-info"><strong title="${escapeHTML(p.name)}">${escapeHTML(p.name)}</strong><p>${all.length} / ${state.templates.length} 个模板${making ? ` · <i class="spinner"></i> 合成中` : processing ? ` · ${processing} 张光影处理中` : ""}</p>
      <button class="${selected ? "primary" : "secondary"} small" data-open-combinations="${p.id}">选择组合 ${selected ? `· 已选 ${selected}` : icon("arrow")}</button></div></article>`;
  }).join("");
  $("composite-empty").hidden=!!products.length;
  if(!products.length) {
    const unconfirmed=state.products.find(p=>p.status==="ready" && !p.placement_confirmed && (!selectedProducts.size || selectedProducts.has(p.id)));
    if(!state.templates.length) empty($("composite-empty"),"先上传一个背景模板","用一个模板确认摆放后，就可以打开商品的全部背景组合。",`<button class="secondary small" data-upload="templates">上传背景模板</button>`);
    else if(unconfirmed) empty($("composite-empty"),"先确认摆放，再挑选背景","确认后，这里会为每个商品显示一个背景组合入口。",`<button class="primary" data-align-product="${unconfirmed.id}">开始调整摆放 ${icon("arrow")}</button>`);
    else empty($("composite-empty"),"暂时没有符合条件的商品","上传商品并确认摆放，或更改商品选择、搜索与筛选条件。");
  }
  if($("combinations-dialog").open) renderCombinationDialog();
  renderDock();
}
function openCombinations(productId) {
  combinationsProduct=productId; combinationsSignature="";
  $("combinations-filter").value=$("preview-filter").value; $("combinations-search").value="";
  renderCombinationDialog(); if(!$("combinations-dialog").open) $("combinations-dialog").showModal();
}
function renderCombinationDialog() {
  const p=state.products.find(p=>p.id===combinationsProduct && p.placement_confirmed && p.status==="ready");
  if(!p) { if($("combinations-dialog").open) $("combinations-dialog").close(); return; }
  const products=confirmedCombinationProducts(), entries=currentCombinations(), all=combinationsFor(p.id), busy=busyCompositeIds();
  const options=products.map(p=>`<option value="${p.id}">${escapeHTML(p.name)}</option>`).join("");
  if($("combinations-product").innerHTML!==options) $("combinations-product").innerHTML=options;
  $("combinations-product").value=p.id; $("combinations-title").textContent=p.name;
  $("combinations-progress").textContent=`已准备 ${all.length} / ${state.templates.length} 个背景组合${productComposing(p.id) ? " · 正在并行合成，可先选择已完成的图片" : " · 点击图片查看完整流程"}`;
  // Preserve scrolling/focus while background jobs update counts elsewhere.
  const signature=JSON.stringify(entries.map(c=>[c.id,selectedComposites.has(c.id),busy.has(c.id),state.results.filter(r=>r.composite_id===c.id).length]));
  if(signature!==combinationsSignature) {
    combinationsSignature=signature;
    $("combinations-grid").innerHTML=entries.map(c=> {
      const results=state.results.filter(r=>r.composite_id===c.id);
      return `<article class="image-card ${selectedComposites.has(c.id) ? "selected" : ""}">
        <button class="select-mark" data-select-composite="${c.id}" ${busy.has(c.id) ? "disabled" : ""} aria-label="选择 ${escapeHTML(c.template_name)} 合成图" aria-pressed="${selectedComposites.has(c.id)}">${icon("check")}</button>
        <span class="card-badge ${busy.has(c.id) ? "processing" : ""}">${busy.has(c.id) ? "光影处理中" : results.length ? `${results.length} 个成品` : "已确认摆放"}</span>
        <button class="image-button" data-inspect-composite="${c.id}"><img loading="lazy" src="${media(c.file,true)}" alt="${escapeHTML(c.product_name)} · ${escapeHTML(c.template_name)}"><span class="image-open">查看图片流程 ↗</span></button>
        <div class="card-meta"><strong title="${escapeHTML(c.template_name)}">${escapeHTML(c.template_name)}</strong><div class="meta-row"><span>1080 × 1350</span><span>4:5</span></div></div></article>`;
    }).join("");
  }
  $("combinations-empty").hidden=!!entries.length;
  if(!entries.length) empty($("combinations-empty"),productComposing(p.id) ? "正在准备背景组合" : "没有符合条件的组合",productComposing(p.id) ? "预览将分批出现，完成后可直接勾选。" : "试试其他筛选条件或背景关键词。");
  $("combinations-select-all").disabled=!entries.some(c=>!busy.has(c.id));
  renderCombinationSelection();
}
function renderCombinationSelection() {
  if(!combinationsProduct) return;
  const all=combinationsFor(combinationsProduct), selected=all.filter(c=>selectedComposites.has(c.id)), busy=busyCompositeIds();
  $("combinations-selected").textContent=`本商品已选 ${selected.length} 张`;
  $("combinations-total").textContent=`全部商品共选 ${selectedComposites.size} 张 · 选择会保留`;
  $("combinations-n").value=$("generation-n").value;
  $("combinations-generate").disabled=submitting || !state.key_ready || !selected.some(c=>!busy.has(c.id));
}
document.addEventListener("click",event=> {
  const button=event.target.closest("[data-open-combinations]");
  if(button) openCombinations(button.dataset.openCombinations);
});
$("combinations-product").onchange=()=> {combinationsProduct=$("combinations-product").value;combinationsSignature="";$("combinations-search").value="";renderCombinationDialog();$("combinations-grid").parentElement.scrollTop=0;};
$("combinations-filter").onchange=renderCombinationDialog; $("combinations-search").oninput=renderCombinationDialog;
$("combinations-select-all").onclick=()=> {const busy=busyCompositeIds();selectVisible(selectedComposites,currentCombinations().filter(c=>!busy.has(c.id)));renderComposites();};
$("combinations-clear").onclick=()=> {combinationsFor(combinationsProduct).forEach(c=>selectedComposites.delete(c.id));renderComposites();};
$("combinations-n").innerHTML=Array.from({length:10},(_,i)=>`<option value="${i+1}">${i+1}</option>`).join("");
$("combinations-n").value="3";
$("combinations-n").onchange=()=> {$("generation-n").value=$("combinations-n").value;renderDock();};
$("combinations-generate").onclick=()=>submitGeneration(combinationsFor(combinationsProduct).filter(c=>selectedComposites.has(c.id)).map(c=>c.id));

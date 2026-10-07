"use strict";
let resultsProduct = null, resultsSignature = "", choosingBest = false;
const comparedResults = new Set();
function resultsFor(productId) { return state.results.filter(r=>r.product_id===productId); }
function resultProducts() { return state.products.filter(p=>resultsFor(p.id).length); }
function bestResultIds() { return state.products.map(p=>p.best_result_id).filter(id=>state.results.some(r=>r.id===id)); }
function renderGroupedResults() {
  const visible = visibleResults(), ids = new Set(visible.map(r=>r.product_id));
  const products = resultProducts().filter(p=>ids.has(p.id));
  $("result-total").textContent=`${products.length} 个商品 · ${visible.length} 张成品`;
  $("result-grid").innerHTML=products.map(p=> {
    const all=resultsFor(p.id), best=all.find(r=>r.id===p.best_result_id), cover=best||all[0];
    const selected=all.filter(r=>selectedResults.has(r.id)).length;
    return `<article class="combination-group ${best ? "has-selection" : ""}"><button class="combination-cover" data-open-results="${p.id}" aria-label="比较 ${escapeHTML(p.name)} 的全部成品"><img loading="lazy" src="${media(cover.file,true)}" alt="${escapeHTML(p.name)}"><span>${best ? "已选最佳" : "待挑选最佳"}</span></button><div class="combination-group-info"><strong title="${escapeHTML(p.name)}">${escapeHTML(p.name)}</strong><p>${all.length} 张成品 · ${new Set(all.map(r=>r.template_id)).size} 个背景${selected ? ` · 已勾选 ${selected}` : ""}</p><button class="${best ? "primary" : "secondary"} small" data-open-results="${p.id}">比较全部结果 ${icon("arrow")}</button></div></article>`;
  }).join("");
  $("result-empty").hidden=!!products.length;
  if(!products.length) empty($("result-empty"),state.results.length ? "没有符合条件的商品" : "等待第一批成品",state.results.length ? "试试其他商品或搜索词。" : "生成完成后，每个商品会在这里拥有自己的成品分组。",`<button class="secondary small" data-view="workspace">前往批量工作台 ${icon("arrow")}</button>`,"spark");
  $("select-results").textContent="选择所有最佳";
  $("select-results").disabled=!bestResultIds().length;
  if($("product-results-dialog").open) renderProductResults();
  if($("results-compare-dialog").open) renderResultsComparison();
  renderDock();
}
function openProductResults(productId) {
  resultsProduct=productId; resultsSignature=""; comparedResults.clear();
  $("results-template-search").value="";
  renderProductResults();
  if(!$("product-results-dialog").open) $("product-results-dialog").showModal();
}
function renderProductResults() {
  const p=state.products.find(p=>p.id===resultsProduct);
  if(!p) { $("product-results-dialog").close(); return; }
  const all=resultsFor(p.id), query=$("results-template-search").value.toLowerCase().trim();
  const entries=all.filter(r=>!query || r.template_name.toLowerCase().includes(query));
  const products=resultProducts(), index=products.findIndex(p=>p.id===resultsProduct);
  const options=products.map(p=>`<option value="${p.id}">${escapeHTML(p.name)}</option>`).join("");
  if($("results-current-product").innerHTML!==options) $("results-current-product").innerHTML=options;
  $("results-current-product").value=p.id; $("product-results-title").textContent=p.name;
  $("result-original-thumb").src=media(p.original,true);
  const composites=new Set(state.composites.filter(c=>c.product_id===p.id).map(c=>c.id));
  const processing=state.jobs.filter(j=>j.kind==="generate").flatMap(j=>j.items).filter(i=>composites.has(i.ref_id)&&["queued","running"].includes(i.status)).length;
  $("product-results-progress").textContent=`第 ${index+1} / ${products.length} 个商品 · 共 ${all.length} 张成品${processing ? ` · ${processing} 个组合正在处理，新成品会自动出现` : " · 点击图片查看流程，可选两张并排对比"}`;
  $("results-previous-product").disabled=index<=0; $("results-next-product").disabled=index>=products.length-1;
  prune(comparedResults,new Set(all.map(r=>r.id)));
  const signature=JSON.stringify([p.best_result_id,entries.map(r=>[r.id,selectedResults.has(r.id),comparedResults.has(r.id)])]);
  if(signature!==resultsSignature) {
    resultsSignature=signature;
    $("product-results-grid").innerHTML=entries.map(r=> {
      const best=r.id===p.best_result_id;
      return `<article class="image-card result-choice ${best ? "best-choice" : ""} ${selectedResults.has(r.id) ? "selected" : ""}"><button class="select-mark" data-select-result="${r.id}" aria-label="勾选 ${escapeHTML(r.template_name)} 结果 ${r.variant}" aria-pressed="${selectedResults.has(r.id)}">${icon("check")}</button><span class="card-badge">${best ? "本商品最佳" : `结果 ${r.variant} · ${r.size}`}</span><button class="image-button" data-inspect-result="${r.id}"><img loading="lazy" src="${media(r.file,true)}" alt="${escapeHTML(r.template_name)} · 结果 ${r.variant}"><span class="image-open">查看图片流程 ↗</span></button><div class="card-meta"><strong title="${escapeHTML(r.template_name)}">${escapeHTML(r.template_name)}</strong><div class="meta-row"><span>结果 ${r.variant}</span><span>${r.width} × ${r.height}</span></div><button class="best-button ${best ? "active" : ""}" data-best-result="${r.id}" aria-pressed="${best}" ${choosingBest ? "disabled" : ""}>${best ? "✓ 本商品最佳" : "设为本商品最佳"}</button><button class="compare-toggle ${comparedResults.has(r.id) ? "active" : ""}" data-compare-result="${r.id}" aria-pressed="${comparedResults.has(r.id)}">${comparedResults.has(r.id) ? "取消对比" : "加入对比"}</button></div></article>`;
    }).join("");
  }
  $("product-results-empty").hidden=!!entries.length;
  if(!entries.length) empty($("product-results-empty"),"没有符合条件的成品","试试其他背景关键词。");
  const best=all.find(r=>r.id===p.best_result_id);
  $("product-best-label").textContent=best ? `最佳：${best.template_name} · 结果 ${best.variant}` : "尚未选择最佳";
  $("clear-product-best").disabled=!best||choosingBest;
  $("results-compare").textContent=`放大对比（${comparedResults.size} / 2）`;
  $("results-compare").disabled=comparedResults.size!==2;
}
async function chooseBest(productId, resultId) {
  if(choosingBest) return;
  choosingBest=true; resultsSignature=""; renderProductResults();
  try {
    await api(`/api/products/${productId}/best-result`,{method:"PATCH",body:JSON.stringify({result_id:resultId})});
    await refresh(true);
  } catch(error) { toast(error.message,true); }
  finally { choosingBest=false; resultsSignature=""; renderResults(); }
}
function renderResultsComparison() {
  $("results-compare-content").innerHTML=[...comparedResults].map(id=>state.results.find(r=>r.id===id)).filter(Boolean).map(r=> {
    const best=state.products.find(p=>p.id===r.product_id)?.best_result_id===r.id;
    return `<article><button class="compare-large-image" data-zoom-result="${r.id}" aria-label="放大 ${escapeHTML(r.template_name)} 结果 ${r.variant}"><img src="${media(r.file)}" alt="${escapeHTML(r.template_name)} · 结果 ${r.variant}"></button><p>${escapeHTML(r.template_name)} · 结果 ${r.variant}</p><button class="best-button ${best ? "active" : ""}" data-best-result="${r.id}" ${choosingBest ? "disabled" : ""}>${best ? "✓ 本商品最佳" : "设为本商品最佳"}</button></article>`;
  }).join("");
}
document.addEventListener("click",event=> {
  const button=event.target.closest("button"); if(!button) return;
  const d=button.dataset;
  if(d.openResults) openProductResults(d.openResults);
  else if(d.bestResult) { const r=state.results.find(r=>r.id===d.bestResult); if(r) chooseBest(r.product_id,r.id); }
  else if(d.compareResult) {
    if(!comparedResults.has(d.compareResult) && comparedResults.size===2) {toast("先取消一张对比图，再选择新图。");return;}
    toggle(comparedResults,d.compareResult); renderProductResults();
  } else if(d.zoomResult) {
    const r=state.results.find(r=>r.id===d.zoomResult); if(r) {$("lightbox-image").src=media(r.file);$("lightbox").showModal();}
  }
});
$("results-current-product").onchange=()=>openProductResults($("results-current-product").value);
$("results-template-search").oninput=renderProductResults;
function stepResultsProduct(delta) {const products=resultProducts(), index=products.findIndex(p=>p.id===resultsProduct);if(products[index+delta])openProductResults(products[index+delta].id);}
$("results-previous-product").onclick=()=>stepResultsProduct(-1);
$("results-next-product").onclick=()=>stepResultsProduct(1);
$("clear-product-best").onclick=()=>chooseBest(resultsProduct,null);
$("results-compare").onclick=()=> {renderResultsComparison();$("results-compare-dialog").showModal();};
$("download-best-results").onclick=()=>downloadBatch("results",bestResultIds(),$("download-best-results"));
$("select-results").onclick=()=> {bestResultIds().forEach(id=>selectedResults.add(id));renderResults();};

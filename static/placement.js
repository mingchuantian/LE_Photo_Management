"use strict";
let placementEditor = null, placementRequest = 0, placementTimer = null, placementController = null;
const placementCanvas = $("placement-canvas"), placementContext = placementCanvas.getContext("2d");
function loadPlacementImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image(); image.onload = () => resolve(image); image.onerror = () => reject(new Error("图片加载失败，请重试。")); image.src = url;
  });
}
async function openPlacementEditor(productId) {
  const product = state.products.find(p => p.id === productId);
  if (!product || product.status !== "ready") { toast("请等待商品抠图完成。", true); return; }
  if (!state.templates.length) { switchView("templates"); toast("先上传一个背景模板，再调整包包摆放。"); return; }
  stopPlacementRequests();
  const session = {productId, saving:false, ready:false}; placementEditor = session;
  $("placement-title").textContent = product.name;
  $("placement-error").hidden = true; $("placement-status").textContent = "正在加载预览…";
  $("placement-confirm").disabled = true; $("placement-next").disabled = true;
  placementContext.clearRect(0,0,1080,1350);
  if (!$("placement-dialog").open) $("placement-dialog").showModal();
  try {
    const data = await api(`/api/products/${productId}/alignment`);
    if (placementEditor !== session) return;
    Object.assign(session, data, {draft:{...data.alignment}});
    $("placement-template").innerHTML = state.templates.map(t=>`<option value="${t.id}">${escapeHTML(t.name)}</option>`).join("");
    $("placement-template").value = session.template_id;
    $("placement-template").disabled = false;
    document.querySelector(".placement-controls").scrollTop = 0;
    $("placement-sizes").innerHTML = Object.entries(state.sizes).map(([s,w])=>`<button data-placement-size="${s}" title="背景宽度 ${percent(w)}%">${s}</button>`).join("");
    $("placement-footer-note").textContent = "这一步只预览摆放，确认后才生成全部模板组合。";
    $("placement-confirm").textContent = `确认并套用 ${state.templates.length} 个模板`;
    const template = state.templates.find(t=>t.id===session.template_id);
    const images = await Promise.all([loadPlacementImage(media(session.sprite_file)), loadPlacementImage(media(template.file))]);
    if (placementEditor !== session) return;
    [session.sprite,session.background] = images; session.ready = true;
    updatePlacement(false); renderExactPlacement();
  } catch (error) { if (placementEditor === session) placementError(error.message); }
}
function stopPlacementRequests() {
  placementRequest++; clearTimeout(placementTimer); placementController?.abort(); placementController = null;
}
function placementError(message) {
  $("placement-error").textContent = message; $("placement-error").hidden = false;
  $("placement-status").textContent = "请调整后再确认"; $("placement-confirm").disabled = true; $("placement-next").disabled = true;
}
function placementBounds() {
  const e = placementEditor, a = e.draft;
  const w = Math.round(a.width * 1080), h = Math.round(w * e.source_height / e.source_width);
  const radians = a.angle * Math.PI / 180;
  return {w,h, bw:Math.abs(w*Math.cos(radians))+Math.abs(h*Math.sin(radians)), bh:Math.abs(w*Math.sin(radians))+Math.abs(h*Math.cos(radians))};
}
function constrainPlacement() {
  const e = placementEditor, a = e.draft;
  a.width = Math.max(.05,Math.min(1,a.width)); a.angle = Math.max(-180,Math.min(180,a.angle));
  let b = placementBounds();
  // Leave 2px for rotation/resampling rounding at the image edges.
  const fit = Math.min(1,1078/b.bw,1348/b.bh);
  if (fit < 1) a.width = Math.max(.05,a.width * fit);
  b = placementBounds();
  a.cx = Math.max((b.bw/2+1)/1080,Math.min(1-(b.bw/2+1)/1080,a.cx));
  a.cy = Math.max((b.bh/2+1)/1350,Math.min(1-(b.bh/2+1)/1350,a.cy));
}
function drawPlacementOutline() {
  const e = placementEditor, a = e.draft, b = placementBounds();
  placementContext.save(); placementContext.translate(a.cx*1080,a.cy*1350); placementContext.rotate(a.angle*Math.PI/180);
  placementContext.strokeStyle = "#6d9667"; placementContext.lineWidth = 3; placementContext.setLineDash([12,9]);
  placementContext.strokeRect(-b.w/2-4,-b.h/2-4,b.w+8,b.h+8); placementContext.restore();
}
function updatePlacement(clamp = true) {
  const e = placementEditor; if (!e?.ready) return;
  if (clamp) constrainPlacement();
  const a = e.draft, b = placementBounds();
  placementContext.clearRect(0,0,1080,1350); placementContext.drawImage(e.background,0,0,1080,1350);
  placementContext.save(); placementContext.translate(a.cx*1080,a.cy*1350); placementContext.rotate(a.angle*Math.PI/180);
  placementContext.drawImage(e.sprite,-b.w/2,-b.h/2,b.w,b.h); placementContext.restore(); drawPlacementOutline();
  for (const [id,value] of [["width",a.width*100],["angle",a.angle]]) {
    $("placement-"+id).value=value; $("placement-"+id+"-number").value=Number(value.toFixed(1));
  }
  $("placement-x").value=Number((a.cx*100).toFixed(2)); $("placement-y").value=Number((a.cy*100).toFixed(2));
  document.querySelectorAll("[data-placement-size]").forEach(b=>b.classList.toggle("active", Math.abs(state.sizes[b.dataset.placementSize]-a.width)<.001));
  $("placement-error").hidden=true; $("placement-status").textContent="正在更新精确预览…";
  $("placement-confirm").disabled=true; $("placement-next").disabled=true;
  stopPlacementRequests(); placementTimer=setTimeout(renderExactPlacement,180);
}
function placementBody() {
  const e=placementEditor;
  return {revision:e.revision,template_id:e.template_id,alignment:{...e.draft}};
}
async function renderExactPlacement() {
  clearTimeout(placementTimer);
  const e=placementEditor; if (!e?.ready || !$("placement-dialog").open) return;
  const version=++placementRequest; placementController?.abort(); placementController=new AbortController();
  let url;
  try {
    const response=await fetch(`/api/products/${e.productId}/preview`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(placementBody()),signal:placementController.signal});
    if (!response.ok) throw new Error((await response.json()).error);
    url=URL.createObjectURL(await response.blob()); const image=await loadPlacementImage(url);
    if (placementEditor!==e || version!==placementRequest || !$("placement-dialog").open) return;
    placementContext.drawImage(image,0,0); drawPlacementOutline();
    $("placement-status").textContent="已更新 · 这就是确认后的摆放效果";
    $("placement-confirm").disabled=e.saving; $("placement-next").disabled=e.saving;
  } catch(error) { if (error.name!=="AbortError" && placementEditor===e && version===placementRequest) placementError(error.message); }
  finally { if(url) URL.revokeObjectURL(url); }
}
async function confirmPlacement(next=false) {
  const e=placementEditor; if (!e?.ready || e.saving || $("placement-confirm").disabled) return;
  e.saving=true; $("placement-confirm").disabled=true; $("placement-next").disabled=true;
  try {
    await api(`/api/products/${e.productId}/confirm-placement`,{method:"POST",body:JSON.stringify(placementBody())});
    $("placement-dialog").close(); await refresh(true);
    toast("摆放已确认，正在生成所有模板组合。");
    if (next) {
      const remaining=state.products.find(p=>p.status==="ready" && !p.placement_confirmed && (!selectedProducts.size || selectedProducts.has(p.id)));
      if (remaining) await openPlacementEditor(remaining.id); else toast("所选商品的摆放都已确认。");
    }
  } catch(error) { placementError(error.message); }
  finally { e.saving=false; }
}
let placementDrag=null;
placementCanvas.addEventListener("pointerdown",event=> {
  const e=placementEditor; if (!e?.ready || e.saving) return;
  const r=placementCanvas.getBoundingClientRect(), x=(event.clientX-r.left)*1080/r.width,y=(event.clientY-r.top)*1350/r.height;
  const a=e.draft, angle=-a.angle*Math.PI/180, dx=x-a.cx*1080,dy=y-a.cy*1350,b=placementBounds();
  if (Math.abs(dx*Math.cos(angle)-dy*Math.sin(angle))>b.w/2 || Math.abs(dx*Math.sin(angle)+dy*Math.cos(angle))>b.h/2) return;
  placementCanvas.focus(); placementCanvas.setPointerCapture(event.pointerId);
  placementDrag={x:event.clientX,y:event.clientY,cx:a.cx,cy:a.cy};
});
placementCanvas.addEventListener("pointermove",event=> {
  if (!placementDrag || !placementEditor?.ready || placementEditor.saving) return;
  const r=placementCanvas.getBoundingClientRect();
  placementEditor.draft.cx=placementDrag.cx+(event.clientX-placementDrag.x)/r.width;
  placementEditor.draft.cy=placementDrag.cy+(event.clientY-placementDrag.y)/r.height; updatePlacement();
});
for(const type of ["pointerup","pointercancel","lostpointercapture"]) placementCanvas.addEventListener(type,()=>{placementDrag=null;});
placementCanvas.addEventListener("keydown",event=> {
  if (!placementEditor?.ready || placementEditor.saving || !["ArrowLeft","ArrowRight","ArrowUp","ArrowDown"].includes(event.key)) return;
  event.preventDefault(); const step=event.shiftKey?10:1,a=placementEditor.draft;
  if (event.key==="ArrowLeft") a.cx-=step/1080;
  if (event.key==="ArrowRight") a.cx+=step/1080;
  if (event.key==="ArrowUp") a.cy-=step/1350;
  if (event.key==="ArrowDown") a.cy+=step/1350; updatePlacement();
});
for (const [id,key,divisor] of [["width","width",100],["width-number","width",100],["angle","angle",1],["angle-number","angle",1],["x","cx",100],["y","cy",100]]) {
  $("placement-"+id).addEventListener(id.includes("number") || id==="x" || id==="y" ? "change" : "input",event=> {
    if (!placementEditor?.ready || placementEditor.saving || event.target.value==="" || !Number.isFinite(Number(event.target.value))) return;
    placementEditor.draft[key]=Number(event.target.value)/divisor; updatePlacement();
  });
}
document.addEventListener("click",event=> {
  const button=event.target.closest("[data-placement-size]"); if (!button || !placementEditor?.ready || placementEditor.saving) return;
  placementEditor.draft.width=state.sizes[button.dataset.placementSize]; updatePlacement();
});
$("placement-template").onchange=async()=> {
  const e=placementEditor; if(!e?.ready || e.saving) return;
  stopPlacementRequests(); e.ready=false; $("placement-template").disabled=true; $("placement-confirm").disabled=true; $("placement-next").disabled=true;
  const template=state.templates.find(t=>t.id===$("placement-template").value);
  try { const background=await loadPlacementImage(media(template.file)); if(placementEditor!==e) return; e.template_id=template.id;e.background=background;e.ready=true;updatePlacement(false); }
  catch(error) { if(placementEditor===e) { e.ready=true;$("placement-template").value=e.template_id;placementError(error.message); } }
  finally { if(placementEditor===e) $("placement-template").disabled=false; }
};
$("placement-reset").onclick=()=> { if(placementEditor?.ready && !placementEditor.saving) {placementEditor.draft={...placementEditor.defaults};updatePlacement(false);} };
$("placement-rotate-left").onclick=()=>rotatePlacement(-5);
$("placement-rotate-right").onclick=()=>rotatePlacement(5);
$("placement-straight").onclick=()=> { if(placementEditor?.ready && !placementEditor.saving) {placementEditor.draft.angle=0;updatePlacement();} };
function rotatePlacement(delta) { if(placementEditor?.ready && !placementEditor.saving) {placementEditor.draft.angle+=delta;updatePlacement();} }
$("placement-confirm").onclick=()=>confirmPlacement(); $("placement-next").onclick=()=>confirmPlacement(true);
$("placement-dialog").addEventListener("close",()=> {stopPlacementRequests();placementEditor=null;placementDrag=null;});
$("align-next").onclick=()=> {
  const ready=state.products.filter(p=>p.status==="ready" && (!selectedProducts.size || selectedProducts.has(p.id)));
  const product=ready.find(p=>!p.placement_confirmed)||ready[0];
  if (product) openPlacementEditor(product.id); else toast("请等待抠图完成后再调整摆放。");
};

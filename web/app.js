const $ = (id) => document.getElementById(id);
const money = (micro) => `¥${(Number(micro || 0) / 1e6).toFixed(3)}`;
const datetime = (ts) => ts ? new Date(ts * 1000).toLocaleString('zh-CN', {hour12:false}) : '-';
const escapeHTML = (value) => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
let refreshTimer = null;
let orderTimer = null;
let dashboardLoading = false;
const pages = {jobs:1, ledger:1, orders:1};
const pageSize = 10;

async function api(path, options = {}) {
  const response = await fetch(path, {credentials:'same-origin', headers:{'Content-Type':'application/json', ...(options.headers || {})}, ...options});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body?.error?.message || `HTTP ${response.status}`);
  return body.data ?? body;
}
function tag(status) {
  const labels = {queued:'排队中',processing:'处理中',success:'成功',failed:'失败',0:'待支付',1:'已支付',2:'已取消'};
  const cls = {0:'processing',1:'paid',2:'canceled'}[status] || status;
  return `<span class="tag ${escapeHTML(cls)}">${escapeHTML(labels[status] ?? status)}</span>`;
}
function showLogin() {
  $('login').classList.remove('hidden'); $('dashboard').classList.add('hidden');
  clearInterval(refreshTimer); refreshTimer = null;
}
function showDashboard() {
  $('login').classList.add('hidden'); $('dashboard').classList.remove('hidden');
  loadDashboard(); clearInterval(refreshTimer); refreshTimer = setInterval(loadDashboard, 3000);
}
async function loadDashboard() {
  if (dashboardLoading) return;
  dashboardLoading = true;
  try {
    const params = new URLSearchParams({jobs_page:pages.jobs,ledger_page:pages.ledger,orders_page:pages.orders,page_size:pageSize});
    const data = await api(`/api/dashboard?${params}`);
    const a = data.account, s = data.stats;
    $('balance').textContent = money(a.balance_micro); $('reserved').textContent = money(a.reserved_micro);
    $('total-topup').textContent = money(a.total_topup_micro); $('total-spend').textContent = money(a.total_spend_micro);
    $('today-success').textContent = s.today_success || 0; $('today-failed').textContent = s.today_failed || 0;
    $('queue-count').textContent = (s.queued || 0) + (s.processing || 0);
    $('success-rate').textContent = `${Number(s.success_rate || 0).toFixed(1)}%`;
    $('today-spend').textContent = money(s.today_spend_micro);
    $('avg-process').textContent = `${(Number(s.avg_process_ms || 0) / 1000).toFixed(1)}s`;
    $('health-dot').textContent = '服务在线'; $('health-dot').classList.add('ok');
    $('last-sync').textContent = `更新于 ${new Date().toLocaleTimeString('zh-CN', {hour12:false})}`;
    $('jobs').innerHTML = data.jobs.map(j => `<tr><td class="mono" title="${escapeHTML(j.request_id)}">${escapeHTML(j.request_id.slice(0,22))}</td><td>${j.target_width}×${j.target_height}</td><td>${j.tier} / ${money(j.price_micro)}</td><td>${escapeHTML(j.operation || '-')}</td><td>${j.route === 'singapore' ? '新加坡' : '国内'}</td><td>${j.queue_ms} / ${j.process_ms} ms</td><td>${tag(j.status)}</td><td>${datetime(j.created_at)}</td></tr>`).join('') || '<tr><td colspan="8" class="muted">暂无调度任务</td></tr>';
    $('ledger').innerHTML = data.ledger.map(x => `<tr><td>${x.kind === 'topup' ? '充值' : '消费'}</td><td>${x.amount_micro > 0 ? '+' : ''}${money(x.amount_micro)}</td><td>${money(x.balance_after_micro)}</td><td>${escapeHTML(x.remark)}</td><td>${datetime(x.created_at)}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">暂无余额流水</td></tr>';
    $('orders').innerHTML = data.orders.map(o => `<tr><td class="mono">${escapeHTML(o.order_no)}</td><td>${o.provider === 'alipay' ? '支付宝' : '微信'}</td><td>${money(Number(o.pay_amount_cents || 0) * 10_000)}</td><td>${tag(o.status)}</td><td>${datetime(o.created_at)}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">暂无充值订单</td></tr>';
    renderPagination('jobs', data.pagination.jobs, data.pagination.page_size);
    renderPagination('ledger', data.pagination.ledger, data.pagination.page_size);
    renderPagination('orders', data.pagination.orders, data.pagination.page_size);
    renderCallsChart(data.charts.trend || []);
    renderLatencyChart(data.charts.trend || []);
    renderTierChart(data.charts.tiers || []);
  } catch (error) {
    if (/登录|unauthorized/i.test(error.message)) showLogin();
    else {$('health-dot').textContent = '同步失败'; $('health-dot').classList.remove('ok');}
  } finally {dashboardLoading = false;}
}

function renderPagination(kind, page, size) {
  const totalPages = Math.max(1, Math.ceil(Number(page.total || 0) / size));
  if (pages[kind] > totalPages) pages[kind] = totalPages;
  $(`${kind}-pagination`).innerHTML = `<span>共 <b>${page.total || 0}</b> 条</span><button data-page-kind="${kind}" data-page="${pages[kind]-1}" ${pages[kind] <= 1 ? 'disabled' : ''}>上一页</button><span>${pages[kind]} / ${totalPages}</span><button data-page-kind="${kind}" data-page="${pages[kind]+1}" ${pages[kind] >= totalPages ? 'disabled' : ''}>下一页</button>`;
}

document.addEventListener('click', event => {
  const button = event.target.closest('[data-page-kind]');
  if (!button || button.disabled) return;
  pages[button.dataset.pageKind] = Number(button.dataset.page);
  loadDashboard();
});

function chartContext(id) {
  const canvas = $(id), dpr = window.devicePixelRatio || 1;
  const width = Math.max(280, canvas.clientWidth), height = 190;
  canvas.style.height = `${height}px`;
  canvas.width = width * dpr; canvas.height = height * dpr;
  const ctx = canvas.getContext('2d'); ctx.scale(dpr,dpr);
  return {ctx,width,height};
}

function renderCallsChart(rows) {
  const {ctx,width,height}=chartContext('calls-chart'), pad={l:30,r:10,t:12,b:28};
  const max=Math.max(1,...rows.flatMap(x=>[Number(x.success||0),Number(x.failed||0)]));
  ctx.font='11px Avenir Next, sans-serif'; ctx.textAlign='center'; ctx.fillStyle='#738078';
  rows.forEach((row,i)=>{
    const slot=(width-pad.l-pad.r)/Math.max(1,rows.length), bar=Math.min(16,slot*.24), x=pad.l+i*slot+slot/2;
    const sh=(height-pad.t-pad.b)*Number(row.success||0)/max, fh=(height-pad.t-pad.b)*Number(row.failed||0)/max;
    ctx.fillStyle='#0f6a48'; ctx.fillRect(x-bar-1,height-pad.b-sh,bar,sh);
    ctx.fillStyle='#d98270'; ctx.fillRect(x+1,height-pad.b-fh,bar,fh);
    ctx.fillStyle='#738078'; ctx.fillText(String(row.day||'').slice(5).replace('-','/'),x,height-8);
  });
  ctx.strokeStyle='#d9dbd2'; ctx.beginPath(); ctx.moveTo(pad.l,height-pad.b+.5);ctx.lineTo(width-pad.r,height-pad.b+.5);ctx.stroke();
}

function renderLatencyChart(rows) {
  const {ctx,width,height}=chartContext('latency-chart'), pad={l:18,r:12,t:16,b:28};
  const values=rows.map(x=>Number(x.avg_process_ms||0)/1000), max=Math.max(1,...values);
  ctx.strokeStyle='#0f6a48';ctx.lineWidth=2.5;ctx.beginPath();
  values.forEach((v,i)=>{const x=pad.l+i*(width-pad.l-pad.r)/Math.max(1,values.length-1),y=height-pad.b-v*(height-pad.t-pad.b)/max;i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();
  ctx.font='10px Avenir Next, sans-serif';ctx.textAlign='center';ctx.fillStyle='#738078';
  rows.forEach((row,i)=>{const x=pad.l+i*(width-pad.l-pad.r)/Math.max(1,rows.length-1);ctx.fillText(String(row.day||'').slice(5).replace('-','/'),x,height-8);});
  ctx.fillStyle='#0f6a48';ctx.textAlign='left';ctx.fillText(`峰值 ${max.toFixed(1)}s`,pad.l,pad.t);
}

function renderTierChart(rows) {
  const colors={'1K':'#0f6a48','2K':'#bbdb62','4K':'#d98270'}, total=rows.reduce((n,x)=>n+Number(x.count||0),0);
  let cursor=0; const stops=[];
  rows.forEach(row=>{const next=cursor+(total?Number(row.count)*100/total:0);stops.push(`${colors[row.tier]||'#738078'} ${cursor}% ${next}%`);cursor=next;});
  $('tier-donut').style.background=total?`conic-gradient(${stops.join(',')})`:'conic-gradient(#dfe4dc 0 100%)';
  $('tier-total').textContent=total;
  $('tier-legend').innerHTML=rows.map(row=>`<div><i style="background:${colors[row.tier]||'#738078'}"></i>${escapeHTML(row.tier)} · ${row.count}</div>`).join('')||'<div>暂无成功任务</div>';
}
$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault(); $('login-error').textContent = '';
  try { await api('/api/login', {method:'POST', body:JSON.stringify({password:$('password').value})}); $('password').value=''; showDashboard(); }
  catch (error) {$('login-error').textContent = error.message;}
});
$('logout').onclick = async () => { await api('/api/logout', {method:'POST'}).catch(()=>{}); showLogin(); };
$('open-topup').onclick = () => {$('topup-dialog').showModal(); resetCheckout();};
$('close-topup').onclick = () => {$('topup-dialog').close(); clearInterval(orderTimer);};
$('topup-dialog').addEventListener('click', e => {if (e.target === $('topup-dialog')) $('topup-dialog').close();});
$('topup-dialog').addEventListener('close', () => {clearInterval(orderTimer); setOrderLoading('', false); setCheckoutLoading(false);});
document.querySelectorAll('[data-amount]').forEach(b => b.onclick = () => $('amount').value = b.dataset.amount);
document.querySelectorAll('[data-provider]').forEach(b => b.onclick = () => createOrder(b.dataset.provider));
$('new-order').onclick = resetCheckout;
const providerLabels = {alipay:'支付宝', wechat:'微信支付'};
function setOrderLoading(provider, active) {
  document.querySelectorAll('[data-provider]').forEach(button => {
    const selected = active && button.dataset.provider === provider;
    button.disabled = active;
    button.classList.toggle('is-loading', selected);
    button.setAttribute('aria-busy', String(selected));
    button.querySelector('.pay-button-label').textContent = selected ? '正在创建订单…' : providerLabels[button.dataset.provider];
  });
  $('topup-loading').classList.toggle('hidden', !active);
}
function setCheckoutLoading(active, text = '正在加载支付二维码…') {
  $('checkout-loading-text').textContent = text;
  $('checkout-loading').classList.toggle('hidden', !active);
  $('checkout-loading').classList.toggle('is-error', active && text.includes('失败'));
}
$('qr').addEventListener('load', () => {
  if (!$('qr').classList.contains('hidden')) setCheckoutLoading(false);
});
$('qr').addEventListener('error', () => {
  if (!$('qr').classList.contains('hidden')) setCheckoutLoading(true, '二维码加载失败，请返回后重试');
});
$('pay-frame').addEventListener('load', () => {
  if (!$('pay-frame').classList.contains('hidden') && $('pay-frame').src !== 'about:blank') setCheckoutLoading(false);
});
function resetCheckout() {$('topup-form-wrap').classList.remove('hidden'); $('checkout').classList.add('hidden'); $('topup-error').textContent=''; $('pay-frame').src='about:blank'; $('pay-frame').classList.add('hidden'); $('qr').removeAttribute('src'); $('qr').classList.remove('hidden'); setOrderLoading('', false); setCheckoutLoading(false); clearInterval(orderTimer);}
async function createOrder(provider) {
  const amount = Number($('amount').value); $('topup-error').textContent='';
  if (!Number.isFinite(amount) || amount < 1 || amount > 500) {$('topup-error').textContent='充值金额须在 ¥1 到 ¥500 之间'; return;}
  setOrderLoading(provider, true);
  try {
    const data = await api('/api/topup', {method:'POST',body:JSON.stringify({provider,amount_micro:Math.round(amount*1e6)})});
    $('topup-form-wrap').classList.add('hidden'); $('checkout').classList.remove('hidden');
    // 优先展示本地生成的二维码图片，只有支付宝未能提取二维码内容时才回落收银台 iframe。
    const useFrame = provider === 'alipay' && !data.qr_code && Boolean(data.pay_url);
    $('qr').classList.toggle('hidden', useFrame); $('pay-frame').classList.toggle('hidden', !useFrame);
    setCheckoutLoading(true);
    if (useFrame) $('pay-frame').src = data.pay_url; else $('qr').src = `/api/topup/qr/${encodeURIComponent(data.order_no)}`;
    const payAmount = `¥${(Number(data.pay_amount_cents || 0) / 100).toFixed(2)}`;
    $('checkout-title').textContent = `请使用${provider === 'alipay' ? '支付宝' : '微信'}支付 ${payAmount}`;
    $('checkout-order').textContent = data.order_no;
    $('pay-link').classList.toggle('hidden', !data.pay_url); if (data.pay_url) $('pay-link').href=data.pay_url;
    clearInterval(orderTimer); orderTimer=setInterval(()=>pollOrder(data.order_no),3000);
  } catch(error) {$('topup-error').textContent=error.message;}
  finally {setOrderLoading('', false);}
}
async function pollOrder(orderNo) {
  try {const order=await api(`/api/topup/orders/${encodeURIComponent(orderNo)}`); if(order.status===1){clearInterval(orderTimer);$('checkout-title').textContent='充值已到账';$('qr').classList.add('hidden');loadDashboard();setTimeout(()=>{$('topup-dialog').close();$('qr').classList.remove('hidden');},1200);}}
  catch (_) {}
}
api('/api/dashboard').then(showDashboard).catch(showLogin);

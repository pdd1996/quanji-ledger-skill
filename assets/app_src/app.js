/* ================= 数据 ================= */
const TX = D.tx.map((a, i) => ({i, m:a[0], d:a[1], cat:a[2], cls:a[3], amt:a[4], dir:a[5],
  merch:a[6], desc:a[7], pk:a[8], plat:a[9], st:a[10]}));
const MON = D.months, ICO = D.icons, DEF = D.default, ALLCATS = D.allcats, INS = D.insight;
const COL = ['#1a6dff','#0f9d58','#faad14','#e0534b','#722ed1','#13c2c2','#eb2f96','#fa8c16',
             '#8bc34a','#2f54eb','#9aa4b8','#ff7875'];
const WD = ['周日','周一','周二','周三','周四','周五','周六'];
const REVIEW = D.review;                       // 待确认交易索引

const S = { tab:'home', month:DEF, scope:'consume', dfilter:'all', openCat:null, q:'',
            sel:new Set(), fixed:{}, rules:{}, sheet:null, wiz:0, wizPlat:null, wizFile:null };

/* ================= 工具 ================= */
const money = n => '¥' + n.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
const k2 = n => n >= 10000 ? (n/10000).toFixed(1) + '万' : Math.round(n).toLocaleString('zh-CN');
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));
const iconOf = c => ICO[c] || ICO[String(c).split('·')[0]] || '⚪';
const catOf = t => S.fixed[t.i] || S.rules[t.merch] || t.cat;
const wd = ds => WD[new Date(ds.replace(/-/g,'/')).getDay()];
const pct = x => (x*100).toFixed(1) + '%';

function slice(mon, scope){
  const rs = TX.filter(t => t.m === mon);
  const exp = rs.filter(t => t.dir === 'e' && (scope === 'all' || t.cls === 'consume'))
                .map(t => ({...t, cat: catOf(t)}));
  const inc = rs.filter(t => t.dir === 'i');
  const neu = rs.filter(t => t.dir === 'n');
  const g = {};
  exp.forEach(t => { (g[t.cat] = g[t.cat] || {v:0,n:0,items:[]}); g[t.cat].v += t.amt; g[t.cat].n++; g[t.cat].items.push(t); });
  const cats = Object.keys(g).map(k => ({cat:k, v:g[k].v, n:g[k].n, items:g[k].items})).sort((a,b) => b.v - a.v);
  const sum = xs => xs.reduce((a,t) => a + t.amt, 0);
  return { rs, exp, inc, neu, cats, total: sum(exp),
    income: sum(inc),
    credit: exp.filter(t => t.pk === 'credit').reduce((a,t) => a+t.amt, 0),
    invest: neu.filter(t => t.cls === 'invest').reduce((a,t) => a+t.amt, 0),
    debt:   neu.filter(t => t.cls === 'debt').reduce((a,t) => a+t.amt, 0) };
}
const txOf = mon => TX.filter(t => t.m === mon);

/* ================= 页面：概览 ================= */
function homeHTML(){
  const s = slice(S.month, S.scope), pi = MON.indexOf(S.month), pm = pi > 0 ? MON[pi-1] : null;
  const sp = pm ? slice(pm, S.scope) : null;
  let delta = '';
  if (sp && sp.total > 0){
    const p = (s.total/sp.total - 1) * 100, up = p >= 0;
    delta = `<div class="delta ${up?'up':'dn'}">较 ${pm.slice(5)} 月 ${up?'↑':'↓'} ${Math.abs(p).toFixed(1)}%</div>`;
  }
  const revN = REVIEW.filter(i => !S.fixed[i]).length;
  const ins3 = INS.anomalies.slice(0,3);
  return `
  <div class="hd">
    <div class="bar">AI 记账 <em>${MON[0]} ~ ${MON[MON.length-1]}</em><b onclick="openSheet('info')">⋯</b></div>
    <div class="pills">${MON.map(m=>`<b class="${m===S.month?'on':''}" onclick="setMonth('${m}')">${m.slice(5)}月</b>`).join('')}</div>
    <div class="big"><div class="l">${S.scope==='consume'?'消费支出（已剔除转账）':'总支出（含转账）'}</div>
      <div class="v"><small>¥</small>${s.total.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})}</div>
      ${delta}</div>
    <div class="duo">
      <div><div class="l">收入</div><div class="v">${money(s.income)}</div></div>
      <div><div class="l">结余</div><div class="v">${money(s.income - s.total)}</div></div>
      <div><div class="l">笔数</div><div class="v">${s.exp.length}</div></div>
    </div>
  </div>
  <div class="seg">${['consume','all'].map(v=>`<b class="${S.scope===v?'on':''}" onclick="setScope('${v}')">${v==='consume'?'仅消费':'全部支出'}</b>`).join('')}</div>
  <div class="kpis">
    <div class="kpi cr"><div class="l">信用消费（花呗/信用购）</div><div class="v">${money(s.credit)}</div>
      <div class="n">占 ${s.total?pct(s.credit/s.total):'0%'} · 下月还款</div></div>
    <div class="kpi gr"><div class="l">推算现金流出</div><div class="v">${money(s.total - s.credit)}</div>
      <div class="n">真正从卡/余额出去的</div></div>
    <div class="kpi"><div class="l">投资买入/卖出</div><div class="v">${money(s.invest)}</div><div class="n">不计收支</div></div>
    <div class="kpi" onclick="go('insight')" style="cursor:pointer"><div class="l">待确认分类</div>
      <div class="v">${revN}<span style="font-size:13px;font-weight:500"> 笔</span></div><div class="n">点击去确认 ›</div></div>
  </div>
  <div class="card"><h3>双视角 · 消费发生额 ≠ 现金流出</h3>
    <div class="dualbar">
      <div style="width:${s.total?(s.total-s.credit)/s.total*100:100}%;background:#0f9d58">现金 ${money(s.total-s.credit)}</div>
      <div style="width:${s.total?s.credit/s.total*100:0}%;background:#e0534b">信用 ${money(s.credit)}</div></div>
    <div class="muted">本月消费发生额 <b style="color:#1a1d24">${money(s.total)}</b>，
      其中 <b style="color:#e0534b">${money(s.credit)}</b> 用花呗/信用购支付，
      所以实际现金流出约 <b style="color:#0f9d58">${money(s.total-s.credit)}</b>。</div>
  </div>
  <div class="card"><h3>本月 TOP 类目<u onclick="go('analysis')">看分析 ›</u></h3>
    <div class="pills" style="padding:0">${s.cats.slice(0,8).map((c,i)=>
      `<b onclick="setMonth('${S.month}');go('analysis')">${iconOf(c.cat)} ${esc(c.cat)} ${money(c.v)}</b>`).join('')}</div>
  </div>
  <div class="card"><h3>需要关注<u onclick="go('insight')">全部 ›</u></h3>
    ${ins3.length ? ins3.map(a=>`<div class="ins ${a.severity}"><div class="tt">${esc(a.title)}</div>
      <div class="dd">${esc(a.detail)}</div></div>`).join('') : '<div class="empty">本期无异常</div>'}
  </div>`;
}

/* ================= 页面：明细 ================= */
function filteredRows(){
  let rs = txOf(S.month);
  const q = S.q.trim();
  if (q) rs = rs.filter(t => (t.merch + t.desc + t.cat).toLowerCase().includes(q.toLowerCase()));
  const f = S.dfilter;
  if (f === 'consume')       rs = rs.filter(t => t.dir === 'e' && t.cls === 'consume');
  else if (f === 'transfer') rs = rs.filter(t => t.dir === 'e' && t.cls === 'transfer');
  else if (f === 'flow')     rs = rs.filter(t => t.dir === 'n');
  else if (f === 'income')   rs = rs.filter(t => t.dir === 'i');
  return rs;
}
function listHTML(){
  const rs = filteredRows();
  if (!rs.length) return '<div class="empty">没有匹配的记录</div>';
  const byDay = {};
  rs.forEach(t => (byDay[t.d.slice(0,10)] = byDay[t.d.slice(0,10)] || []).push(t));
  return Object.keys(byDay).sort().reverse().map(day => {
    const list = byDay[day];
    const out = list.filter(t => t.dir === 'e').reduce((a,t) => a+t.amt, 0);
    const inn = list.filter(t => t.dir === 'i').reduce((a,t) => a+t.amt, 0);
    return `<div class="dgroup">
      <div class="dhead"><b>${day.slice(5)} ${wd(day)}</b>
        <span>${out?'支出 '+money(out):''}${out&&inn?' · ':''}${inn?'收入 '+money(inn):''}</span></div>
      <div class="dlist">${list.map(t => {
        const isE = t.dir === 'e';
        const amt = t.dir === 'n'
          ? `<span style="color:#98a0b0">${money(t.amt)}</span>`
          : `<span class="amt ${isE?'':'in'}">${isE?'-':'+'}${t.amt.toFixed(2)}</span>`;
        return `<div class="li"><div class="ic">${iconOf(catOf(t))}</div>
          <div class="mid"><b>${esc(t.merch)}${t.pk==='credit'?'<span class="pill cr">信用</span>':''}</b>
            <i>${esc(t.d.slice(5))} · ${esc(catOf(t))} · ${esc(t.plat)}</i></div>${amt}</div>`;
      }).join('')}</div></div>`;
  }).join('');
}
function detailHTML(){
  const rs = filteredRows();
  const FILTERS = [['all','全部'],['consume','消费'],['transfer','转账'],['flow','资金流转'],['income','收入']];
  const out = rs.filter(t => t.dir === 'e').reduce((a,t) => a + t.amt, 0);
  const inn = rs.filter(t => t.dir === 'i').reduce((a,t) => a + t.amt, 0);
  return `<div class="hd"><div class="bar">明细<em>${rs.length} 笔</em></div>
    <div class="pills">${MON.map(m=>`<b class="${m===S.month?'on':''}" onclick="setMonth('${m}')">${m.slice(5)}月</b>`).join('')}</div>
    <div class="duo" style="margin-top:12px">
      <div><div class="l">支出</div><div class="v">${money(out)}</div></div>
      <div><div class="l">收入</div><div class="v">${money(inn)}</div></div>
      <div><div class="l">净额</div><div class="v">${money(inn-out)}</div></div></div></div>
  <div class="pills" style="background:#fff;margin:12px 0 0;padding:10px 14px;box-shadow:0 1px 5px rgba(20,30,60,.05)">
    ${FILTERS.map(([v,n])=>`<b class="${S.dfilter===v?'on':''}" onclick="setDFilter('${v}')">${n}</b>`).join('')}</div>
  <div class="search">🔍<input id="q" placeholder="搜索商户 / 说明 / 类目" value="${esc(S.q)}"
    oninput="S.q=this.value;document.getElementById('listwrap').innerHTML=listHTML()"></div>
  <div id="listwrap">${listHTML()}</div>`;
}
/* ================= 页面：分析 ================= */
function analysisHTML(){
  const s = slice(S.month, S.scope);
  const all = slice(S.month, 'all');
  const trans = all.cats.filter(c => c.cat === '转账·人际').reduce((a,c) => a+c.v, 0);
  const tp = all.total ? trans/all.total*100 : 0;
  const tot = s.total || 1;
  let off = 0, seg = '';
  s.cats.slice(0,11).forEach((c,i) => {
    const p = c.v/tot*100;
    seg += `<circle r="15.9155" cx="21" cy="21" fill="none" stroke="${COL[i%COL.length]}" stroke-width="6.2"
      stroke-dasharray="${p} ${100-p}" stroke-dashoffset="${-off}"></circle>`;
    off += p;
  });
  if (s.cats.length > 11) seg += `<circle r="15.9155" cx="21" cy="21" fill="none" stroke="#c8cfdb"
    stroke-width="6.2" stroke-dasharray="${Math.max(0,100-off)} 0"></circle>`;
  const cmax = s.cats.length ? s.cats[0].v : 1;
  const mv = Math.max(...MON.map(m => slice(m,S.scope).total), 1);
  const note = trans <= 0 ? '' : (S.scope === 'all'
    ? `<div class="warnbox">本期「全部支出」里含 <b>${money(trans)}</b> 转账/人际往来，占 <b>${tp.toFixed(1)}%</b>。
        它通常不是消费，所以默认口径「仅消费」已把它剔除。</div>`
    : `<div class="muted" style="margin:10px 16px 0">口径：已剔除 <b>转账·人际 ${money(trans)}</b>（占全部支出 ${tp.toFixed(1)}%）· 切到「全部支出」查看完整口径</div>`);
  return `<div class="hd"><div class="bar">收支分析</div>
      <div class="pills">${MON.map(m=>`<b class="${m===S.month?'on':''}" onclick="setMonth('${m}')">${m.slice(5)}月</b>`).join('')}</div>
      <div class="big" style="margin-top:14px"><div class="l">${S.scope==='consume'?'消费支出':'总支出（含转账）'}</div>
        <div class="v"><small>¥</small>${s.total.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})}</div></div>
      <div class="duo"><div><div class="l">收入</div><div class="v">${money(s.income)}</div></div>
        <div><div class="l">结余</div><div class="v">${money(s.income-s.total)}</div></div>
        <div><div class="l">笔数</div><div class="v">${s.exp.length}</div></div></div></div>
    <div class="seg">${['consume','all'].map(v=>`<b class="${S.scope===v?'on':''}" onclick="setScope('${v}')">${v==='consume'?'仅消费':'全部支出'}</b>`).join('')}</div>
    ${note}
    <div class="card"><h3>支出构成<em>${S.month} · ${s.exp.length} 笔</em></h3>
      <div class="donutwrap"><div class="donut">
        <svg viewBox="0 0 42 42">${seg}</svg>
        <div class="c"><b>${k2(s.total)}</b><i>合计</i></div></div>
        <div class="lg">${s.cats.slice(0,6).map((c,i)=>`<div><em style="background:${COL[i%COL.length]}"></em>
          <span class="n">${esc(c.cat)}</span><span class="p">${(c.v/tot*100).toFixed(1)}%</span></div>`).join('')}</div>
      </div></div>
    <div class="card"><h3>分类排行<em>点击展开明细</em></h3>
      ${s.cats.length ? s.cats.map((c,i) => `<div class="cat">
        <div class="crow" onclick="toggle('${esc(c.cat)}')">
          <div class="ic">${iconOf(c.cat)}</div>
          <div class="cmain"><div class="n">${esc(c.cat)}<em>${c.n} 笔</em></div>
            <div class="bar"><i style="width:${c.v/cmax*100}%;background:${COL[i%COL.length]}"></i></div></div>
          <div class="camt"><b>${money(c.v)}</b><i>${(c.v/tot*100).toFixed(1)}%</i></div></div>
        <div class="det ${S.openCat===c.cat?'on':''}">${c.items.map(t=>`<div class="r">
          <div class="l"><b>${esc(t.merch)}${t.pk==='credit'?'<span class="pill cr">信用</span>':''}</b>
            <i>${esc(t.d.slice(5))} · ${esc(t.desc)} · ${esc(t.plat)}</i></div>
          <div class="a">-${t.amt.toFixed(2)}</div></div>`).join('')}</div></div>`).join('')
        : '<div class="empty">本月无支出</div>'}</div>
    <div class="card"><h3>月度趋势<em>同口径</em></h3>
      <div class="trend">${MON.map(m => { const v = slice(m,S.scope).total;
        return `<div class="b ${m===S.month?'on':'dim'}" onclick="setMonth('${m}')">
          <div class="v">${k2(v)}</div>
          <i style="height:${Math.max(4, v/mv*88)}px"></i><div class="t">${m.slice(5)}月</div></div>`; }).join('')}</div>
      <div class="muted" style="margin-top:9px">首末月为不完整月份（${MON[0]} ~ ${MON[MON.length-1]}）</div></div>
    <div class="card"><h3>资金流水<em>不计收支</em></h3>
      <div class="srcline"><span>投资买入 / 卖出</span><b>${money(s.invest)}</b></div>
      <div class="srcline"><span>信用还款</span><b style="color:#0f9d58">${money(s.debt)}</b></div>
      <div class="srcline"><span>其中信用消费（已含在上面支出里）</span><b style="color:#e0534b">${money(s.credit)}</b></div>
      <div class="muted" style="margin-top:9px">投资买入与信用还款属于资金流转，不计入收支 —— 否则「花了多少」会严重失真。</div></div>`;
}

/* ================= 页面：洞察 ================= */
function insightHTML(){
  const rev = REVIEW.filter(i => !S.fixed[i]);
  const allSel = rev.length > 0 && rev.every(i => S.sel.has(i));
  return `<div class="hd"><div class="bar">账本洞察<em>${MON.length} 个月</em></div>
    <div class="duo" style="margin-top:14px">
      <div><div class="l">定投</div><div class="v">${money(INS.plans.reduce((a,p)=>a+p.total,0))}</div></div>
      <div><div class="l">周期扣费</div><div class="v">${INS.subs.length} 项</div></div>
      <div><div class="l">异常</div><div class="v">${INS.anomalies.length} 条</div></div></div></div>

    <div class="card"><h3>定投识别<em>重复小额·固定周期</em></h3>
      ${INS.plans.length ? INS.plans.map(p=>`<div class="ins info">
        <div class="tt">${esc(p.plan)}<span class="pill">${p.count} 笔 × ${money(p.single)}</span></div>
        <div class="dd">${p.first} ~ ${p.last}（${p.span} 天）累计 <b style="color:#0f9d58">${money(p.total)}</b></div></div>`).join('')
      : '<div class="empty">未识别到定投</div>'}
      ${INS.incomes.length ? `<div class="muted" style="margin-top:9px">同期收益类：` +
        INS.incomes.map(i=>`${esc(i.plan)} ${i.count} 笔 = ${money(i.total)}`).join('；') + `</div>` : ''}</div>

    <div class="card"><h3>订阅与周期扣费<em>固定商户+固定金额+固定周期</em></h3>
      ${INS.subs.length ? INS.subs.map(s=>`<div class="ins ${s.cycle.includes('疑似')?'warn':'info'}">
        <div class="tt">${esc(s.merchant)}<span class="pill ${s.cycle.includes('疑似')?'yl':'gr'}">${s.cycle}</span>
          <span class="pill">${esc(s.category)}</span></div>
        <div class="dd">${money(s.amount)} × ${s.count} 期 · ${s.first} ~ ${s.last} · 折合月均 <b>${money(s.monthly)}</b></div></div>`).join('')
      : '<div class="empty">未识别到周期扣费</div>'}</div>

    <div class="card"><h3>异常检测<em>单笔偏高 / 重复扣款 / 环比</em></h3>
      ${INS.anomalies.length ? INS.anomalies.map(a=>`<div class="ins ${a.severity}">
        <div class="tt">${esc(a.title)}</div><div class="dd">${esc(a.detail)}</div>
        ${(a.evidence||[]).map(e=>`<div class="ev">${esc(e)}</div>`).join('')}</div>`).join('')
      : '<div class="empty">未发现异常</div>'}</div>

    <div class="card"><h3>退款追踪</h3>
      <div class="srcline"><span>退款笔数</span><b>${INS.refunds.n} 笔</b></div>
      <div class="srcline"><span>退款合计</span><b style="color:#0f9d58">${money(INS.refunds.total)}</b></div>
      <div class="srcline"><span>进行中</span><b>${INS.refunds.pending} 笔 ${money(INS.refunds.pending_amt)}</b></div>
      ${INS.refund_items.slice(0,4).map(r=>`<div class="srcline"><span>${r.t} · ${esc(r.d.slice(0,18))}</span><b>${money(r.a)}</b></div>`).join('')}</div>

    <div class="card"><h3>待确认分类<em>${rev.length} 笔</em></h3>
      ${rev.length ? `
      <div class="rowline" style="margin-bottom:8px">
        <div class="chk ${allSel?'on':''}" onclick="selAll()">${allSel?'✓':''}</div>
        <span class="muted" style="flex:1">全选 &nbsp;·&nbsp; 已选 <b style="color:#1a6dff">${S.sel.size}</b> 笔</span></div>
      ${rev.map(i => { const t = TX[i], on = S.sel.has(i);
        return `<div class="rv"><div class="chk ${on?'on':''}" onclick="toggleSel(${i})">${on?'✓':''}</div>
          <div class="mid"><b>${esc(t.merch)}</b><i>${esc(t.d.slice(0,10))} · ${esc(t.desc)} · 暂定「${esc(catOf(t))}」</i></div>
          <div class="amt">${money(t.amt)}</div></div>`; }).join('')}
      <div class="rowline" style="margin-top:12px">
        <select id="pickcat">${ALLCATS.map(c=>`<option>${esc(c)}</option>`).join('')}</select>
        <button class="btn pri" style="width:auto;padding:12px 20px;white-space:nowrap" onclick="applyReview()">确认</button></div>
      <label class="rowline" style="margin-top:10px;font-size:12.5px;color:#8b93a5">
        <input type="checkbox" id="remember" checked style="width:16px;height:16px;flex:0 0 auto">
        记住这些商户，下次自动归类（数据飞轮）</label>`
      : '<div class="empty">全部已确认 ✓</div>'}</div>`;
}

/* ================= 页面：我的 ================= */
function mineHTML(){
  const rules = Object.keys(S.rules);
  const nW = TX.filter(t => t.plat === '微信').length, nA = TX.filter(t => t.plat === '支付宝').length;
  return `<div class="hd"><div class="bar">我的</div>
    <div class="duo" style="margin-top:14px">
      <div><div class="l">账本总笔数</div><div class="v">${TX.length}</div></div>
      <div><div class="l">已学规则</div><div class="v">${rules.length} 条</div></div>
      <div><div class="l">待确认</div><div class="v">${REVIEW.filter(i=>!S.fixed[i]).length} 笔</div></div></div></div>

    <div class="card"><h3>数据源<em>本地解析</em></h3>
      <div class="srcline"><span>💬 微信支付账单</span><b>${nW} 笔 · 已导入</b></div>
      <div class="srcline"><span>🅰️ 支付宝交易明细</span><b>${nA} 笔 · 已导入</b></div>
      <div class="muted" style="margin-top:9px">账单区间 ${MON[0]} ~ ${MON[MON.length-1]}，共 ${MON.length} 个月</div></div>

    <div style="margin:12px 14px"><button class="btn pri" onclick="openSheet('wizard')">📥 导入账单</button></div>

    <div class="card"><h3>已学规则<em>${rules.length} 条</em></h3>
      ${rules.length ? rules.map(m=>`<div class="srcline"><span>${esc(m)}</span><b>${esc(S.rules[m])}</b></div>`).join('')
        : '<div class="empty">还没有学习到规则<br><span style="font-size:11.5px">在「洞察 → 待确认分类」里确认几笔，就会自动记住</span></div>'}</div>

    <div class="card"><h3>口径说明</h3>
      <div class="muted">· <b>消费</b>：真实花掉的钱（默认口径，已剔除转账/红包/代付）<br>
        · <b>转账·人际</b>：转账、红包、群收款 —— 不计入消费<br>
        · <b>投资</b>：基金买入/卖出、收益 —— 资金流转，不计收支<br>
        · <b>信用还款</b>：花呗/信用购还款 —— 不计收支<br>
        · <b>信用消费</b>：用花呗/信用购支付的消费，计入消费但现金流出在下月<br><br>
        支付宝官方统计口径存在不一致（笔数含交易关闭、金额不含；基金买入退款在桶内冲减），
        本应用以<b>明细为准</b>重新计算。</div></div>

    <div class="card"><h3>隐私</h3>
      <div class="muted">· 账单压缩包在本地解密，密码不出设备、不落盘<br>
        · 姓名 / 账户 / 昵称等字段在导入时已剥离<br>
        · 页面为纯本地文件，无任何网络请求</div></div>

    <div class="muted center" style="margin:18px 16px 0">AI 记账 · 原型演示<br>数据来自微信支付账单 + 支付宝交易明细</div>`;
}

/* ================= 导入向导 ================= */
const PLATS = [
  {id:'wechat', name:'微信支付账单', sub:'导出为 .xlsx，压缩包加密', color:'#07c160', icon:'💬',
   path:'微信 → 我 → 服务 → 钱包 → 账单 → 常见问题 → 下载账单 → 用于个人对账 → 选月份 → 填邮箱'},
  {id:'alipay', name:'支付宝交易明细', sub:'导出为 .csv，压缩包加密', color:'#1677ff', icon:'🅰️',
   path:'支付宝 → 我的 → 账单 → 右上角 → 开具交易流水证明 → 选时间范围 → 填邮箱'},
];
const WLOG = [
  '读取文件 … <文件大小> bytes',
  '识别加密方式 … ZipCrypto / DEFLATE',
  '校验解压密码 … OK',
  '解压 … 账单明细文件.xlsx (<解压后大小> bytes)',
  '识别平台 … wechat，表头位于第 17 行',
  '解析交易 … N 笔',
  '资金分流 … 主账本 N · 不计收支 N · 交易关闭 N · 零金额 N',
  '商户归一与分类 … 知识库命中 N · 待确认 N',
  '与现有账本对账 … 新增 N · 去重合并 N',
  '完成',
];
function wizHTML(){
  const st = S.wiz;
  let body = '';
  if (st === 0) body = `<h4>从哪个平台导入？</h4><p>微信和支付宝的账单都需要先在 App 内导出，再导入这里。导出的压缩包是加密的，需要输入解压密码。</p>
    ${PLATS.map(p=>`<div class="pick ${S.wizPlat===p.id?'on':''}" onclick="pickPlat('${p.id}')">
      <div class="lg2" style="background:${p.color}">${p.icon}</div>
      <div class="t"><b>${p.name}</b><i>${p.sub}</i></div></div>`).join('')}
    ${S.wizPlat?`<div class="card" style="margin:14px 0 0;background:#f5f9ff"><div class="muted">📌 ${PLATS.find(p=>p.id===S.wizPlat).path}</div></div>`:''}`;
  else if (st === 1) body = `<h4>选择账单文件</h4><p>支持 .zip（加密压缩包）、.csv、.xlsx</p>
    <input type="file" id="fi" accept=".zip,.csv,.xlsx" onchange="pickFile(this)">
    <label for="fi" class="filebox"><div class="t">${S.wizFile?'📄 '+esc(S.wizFile.name):'点击选择文件'}</div>
      <div class="s">${S.wizFile?(S.wizFile.size.toLocaleString()+' bytes · '+S.wizFile.kind):'也可以把邮件里的附件「分享」到本应用'}</div></label>`;
  else if (st === 2) body = `<h4>输入解压密码</h4>
    <p>${S.wizPlat==='wechat'?'微信支付公众号会给你发一条消息，里面有解压密码 —— <b>每次导出都不一样</b>。':'密码在支付宝的账单申请页面上显示 —— <b>每次导出都不一样</b>。'}</p>
    <div class="rowline"><input id="pwd" placeholder="6 位数字" inputmode="numeric"
      style="flex:1;font-family:inherit;font-size:16px;letter-spacing:3px;padding:13px;border-radius:11px;
      border:1.5px solid #e2e6ee;outline:0">
      <button class="btn" style="width:auto;padding:13px 14px;white-space:nowrap;font-size:13px"
        onclick="pastePwd()">📋 粘贴</button></div>
    <div class="card" style="background:#f5f9ff;margin:14px 0 0"><div class="muted">
      🔒 密码只在你的设备上使用，<b>不会上传到任何服务器</b>。压缩包也在本地解密。</div></div>`;
  else if (st === 3) body = `<h4>正在解析…</h4><p>本地解密 + 解析 + 分类 + 对账</p>
    <div class="log" id="wlog"></div>`;
  else body = `<div class="bigres"><div class="v">N</div><div class="l">新增账目</div></div>
    <div class="card" style="margin:0 0 14px"><h3>导入结果</h3>
      <div class="srcline"><span>解析交易</span><b>N 笔</b></div>
      <div class="srcline"><span>去重合并</span><b>N 笔</b></div>
      <div class="srcline"><span>自动分类</span><b>N 笔</b></div>
      <div class="srcline"><span>待确认</span><b style="color:#b8860b">N 笔</b></div>
      <div class="srcline"><span>耗时</span><b>N.Ns</b></div></div>
    <div class="muted" style="margin:0 0 14px">💡 这次导入的共享单车记录（商户名多为平台马甲公司），
      已按本地商户库归入「交通·共享单车」。</div>
    <button class="btn pri" onclick="closeSheet();go('insight')">去确认分类</button>`;
  return `<div class="panel"><div class="phead">导入账单<b onclick="closeSheet()">×</b></div>
    <div class="steps">${[0,1,2,3,4].map(i=>`<i class="${i<=st?'on':''}"></i>`).join('')}</div>
    <div class="wz">${body}</div>
    ${st<3?`<div style="padding:22px 18px 0"><button class="btn pri" ${(st===0&&!S.wizPlat)||(st===1&&!S.wizFile)?'disabled':''}
      onclick="${st===2?'startParse()':'wizNext()'}">${st===2?'开始解析':'下一步'}</button></div>`:''}</div>`;
}
function infoHTML(){
  return `<div class="panel"><div class="phead">口径说明<b onclick="closeSheet()">×</b></div>
    <div class="wz"><p>记账最容易错的地方不是算错数，而是<b>口径混淆</b>。本应用把每一笔拆成四类：</p>
      <div class="card" style="margin:0 0 12px"><div class="muted">
        <b>消费</b> —— 真实花掉的钱。默认统计口径（已剔除转账）<br><br>
        <b>转账 · 人际</b> —— 转账、红包、群收款、代付。它只是钱换了位置，不是消费。<br>
        在一份实测账单里它曾占全部支出的 <b>接近一半</b>，如果混进来，「钱花哪了」会被严重带偏。<br><br>
        <b>投资</b> —— 基金买入/卖出、理财收益。<br><br>
        <b>信用还款</b> —— 花呗/信用购还款。还款不是消费，消费发生在更早的那个月。<br><br>
        <b>信用消费</b> —— 用花呗付的消费。按权责发生制计入<b>消费发生月</b>，
        但现金流出其实在下个月的还款日。
      </div></div>
      <div class="muted">另外：支付宝官方统计口径本身存在不一致 —— 官方「支出」的<b>笔数</b>含交易关闭，
        但<b>金额</b>不含；不计收支里基金「买入退款」在桶内冲减。本应用一律<b>以明细为准</b>重新计算。</div>
      <div style="padding-top:18px"><button class="btn pri" onclick="closeSheet()">知道了</button></div></div></div>`;
}

/* ================= 交互 ================= */
function render(){
  document.getElementById('page').innerHTML =
    S.tab === 'home' ? homeHTML() : S.tab === 'detail' ? detailHTML() :
    S.tab === 'analysis' ? analysisHTML() : S.tab === 'insight' ? insightHTML() : mineHTML();
  const revN = REVIEW.filter(i => !S.fixed[i]).length;
  const TABS = [['home','🏠','概览'],['detail','📋','明细'],['analysis','📊','分析'],
                ['insight','✨','洞察'],['mine','👤','我的']];
  document.getElementById('tabbar').innerHTML = TABS.map(([id,ic,nm]) =>
    `<div class="${S.tab===id?'on':''}" onclick="go('${id}')"><i>${ic}</i>${nm}` +
    (id==='insight'&&revN?`<span class="badge">${revN}</span>`:'') + `</div>`).join('');
  const sh = document.getElementById('sheet');
  if (S.sheet === 'wizard'){ sh.classList.add('on'); document.getElementById('panel').outerHTML = wizHTML().replace('class="panel"','id="panel" class="panel"'); }
  else if (S.sheet === 'info'){ sh.classList.add('on'); document.getElementById('panel').outerHTML = infoHTML().replace('class="panel"','id="panel" class="panel"'); }
  else { sh.classList.remove('on'); }
  window.scrollTo(0,0);
}
function go(t){ S.tab = t; render(); }
function setMonth(m){ S.month = m; S.openCat = null; render(); }
function setScope(s){ S.scope = s; S.openCat = null; render(); }
function setDFilter(v){ S.dfilter = v; render(); }
function toggle(c){ S.openCat = (S.openCat === c ? null : c); render(); }
function toggleSel(i){ S.sel.has(i) ? S.sel.delete(i) : S.sel.add(i); render(); }
function selAll(){ const rev = REVIEW.filter(i => !S.fixed[i]);
  if (rev.every(i => S.sel.has(i))) S.sel.clear(); else rev.forEach(i => S.sel.add(i)); render(); }
function applyReview(){
  const cat = document.getElementById('pickcat').value, rem = document.getElementById('remember').checked;
  S.sel.forEach(i => { S.fixed[i] = cat; if (rem) S.rules[TX[i].merch] = cat; });
  S.sel.clear(); render();
}
function openSheet(v){ S.sheet = v; S.wiz = 0; render(); }
function closeSheet(){ S.sheet = null; render(); }
function pickPlat(id){ S.wizPlat = id; render(); }
function pickFile(el){
  const f = el.files[0]; if (!f) return;
  const ext = (f.name.split('.').pop() || '').toLowerCase();
  S.wizFile = {name: f.name, size: f.size,
    kind: ext === 'zip' ? '加密压缩包 · 需密码' : ext === 'csv' ? 'CSV 明文件' : 'XLSX 明文件'};
  render();
}
async function pastePwd(){
  const el = document.getElementById('pwd'); if (!el) return;
  try { el.value = (await navigator.clipboard.readText()).trim(); }
  catch (e) { el.focus(); el.placeholder = '请手动粘贴（浏览器未授权剪贴板）'; }
}
function wizNext(){ S.wiz++; render(); }
function startParse(){
  S.wiz = 3; render();
  let k = 0; const el = document.getElementById('wlog');
  const tick = () => {
    if (!el) return;
    if (k < WLOG.length){ el.textContent += '✓ ' + WLOG[k++] + '\n'; setTimeout(tick, 260); }
    else setTimeout(() => { S.wiz = 4; render(); }, 420);
  };
  tick();
}
render();